# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""SQL journal for optional AI refresh work.

These operations participate in the caller's transaction. They never commit,
contact a provider, enqueue a job or use Redis as the source of truth. The
caller starts each one in a fresh transaction and passes the run's session
name, so the first statement is the lock on that Session row; an existing run
then locks its Run and Attempt, in that order. Admission locks the Session,
then (user refreshes only) the Control row with NOWAIT, and takes over an
expired run of its session in the same transaction, so recovery does not wait
for the scheduler. A caller commits a
calling attempt before sending HTTP, then commits its result and outcome
together, adding the reported usage to the session counter in the same
transaction. That counter is #72's log-and-continue (D2): a statement-level
failure of it is logged and the outcome still commits, so the run's
``tokens_reported`` is the source for reconciling the session's total.
"""

import hashlib

import frappe

from optimus import ai_fix, safe_call

RUN = "Optimus AI Refresh Run"
ATTEMPT = "Optimus AI Refresh Attempt"
CONTROL = "Optimus AI Refresh Control"
ACTIVE = frozenset({"queued", "running"})
TERMINAL = frozenset({"complete", "stopped", "cancelled", "interrupted"})
# Slices in a row that could not begin any item before the run stops with ``no_progress``.
IDLE_SLICE_LIMIT = 3
# Failures about one prompt, which say nothing about the next finding's: the prompt did not fit
# the model's context window, or moderation refused it. Recorded under their own kind, and
# neither stop the run at once (they are not ``ai_fix.AI_FATAL_KINDS``) nor count toward the
# three-in-a-row stop.
PER_PROMPT_KINDS = frozenset({"context", "refused"})
# Seconds before a slice's pending dispatch intent may be sent to RQ again.
DISPATCH_INTERVAL = 30


class JournalUnavailable(RuntimeError):
	"""Fail closed when a deployment lacks the authoritative journal/mutex."""


# Driver codes of a conflict that ended the statement or the whole transaction, so a short
# journal transaction can start again from a rollback: MariaDB deadlock 1213, lock wait
# timeout 1205 (also a NOWAIT conflict) and 1020 "Record has changed since last read"
# (innodb_snapshot_isolation, on by default since MariaDB 11.8); Postgres serialization
# failure 40001, deadlock 40P01 and lock not available 55P03.
_RETRY_CODES = frozenset({1213, 1205, 1020, "40001", "40P01", "55P03"})
_LOCK_UNAVAILABLE_CODES = frozenset({1205, "55P03"})
_DUPLICATE_CODES = frozenset({1062, "23505"})


def _driver_codes(exc):
	"""The driver error codes on ``exc`` and on the driver errors Frappe wrapped it around.

	``Database.sql`` raises ``QueryDeadlockError(e)`` / ``QueryTimeoutError(e)`` from ``e``
	and ``Document.insert`` raises ``DuplicateEntryError(doctype, name, e)``, so the driver
	error is one of the args (and the cause). PyMySQL keeps the code in ``args[0]``,
	psycopg2 in ``pgcode``."""
	codes, seen, stack = set(), set(), [exc]
	while stack and len(seen) < 8:
		layer = stack.pop()
		if not isinstance(layer, BaseException) or id(layer) in seen:
			continue
		seen.add(id(layer))
		pgcode = getattr(layer, "pgcode", None)
		if isinstance(pgcode, str) and pgcode:
			codes.add(pgcode)
		args = getattr(layer, "args", ())
		if args and type(args[0]) is int:
			codes.add(args[0])
		stack.extend(arg for arg in args if isinstance(arg, BaseException))
		stack.append(layer.__cause__)
	return codes


def _frappe_error(exc, name):
	cls = getattr(frappe, name, None)
	return isinstance(cls, type) and isinstance(exc, cls)


def is_retryable_conflict(exc) -> bool:
	"""True when a short journal transaction may be rolled back and tried again: a
	deadlock, a lock wait, a snapshot or serialization conflict, raw or as Frappe wraps it.

	``frappe.QueryDeadlockError`` always is (the database ended the whole transaction, as
	``analyze._whole_transaction_rolled_back`` treats it). ``frappe.QueryTimeoutError`` is
	when it wraps a lock conflict, or no driver error at all; a Postgres statement timeout
	(57014) is not a conflict. Never a reason to repeat a provider call."""
	codes = _driver_codes(exc)
	if _frappe_error(exc, "QueryDeadlockError") or codes & _RETRY_CODES:
		return True
	return _frappe_error(exc, "QueryTimeoutError") and not codes


def is_lock_unavailable(exc) -> bool:
	"""True when a lock was not granted: a NOWAIT conflict or a lock wait timeout (MariaDB
	1205, Postgres 55P03), raw or wrapped by Frappe as ``QueryTimeoutError``."""
	codes = _driver_codes(exc)
	if codes & _LOCK_UNAVAILABLE_CODES:
		return True
	return _frappe_error(exc, "QueryTimeoutError") and not codes


def is_duplicate_key(exc) -> bool:
	"""True when an INSERT hit an existing key (MariaDB 1062, Postgres 23505), raw or as
	``Document.insert`` raises it (``DuplicateEntryError``, ``UniqueValidationError``)."""
	codes = _driver_codes(exc)
	if codes & _DUPLICATE_CODES:
		return True
	return not codes and (
		_frappe_error(exc, "DuplicateEntryError") or _frappe_error(exc, "UniqueValidationError")
	)


def _timestamp(value):
	# The selection cursor must also be representable by datetime in the worker.
	return type(value) in (int, float) and 0 <= value < 253402214400


def _read(table, name, *, lock=False, wait=True):
	return frappe.db.get_value(table, name, "*", as_dict=True, for_update=lock, wait=wait)


def _update(table, row, **values):
	if table == RUN:
		values["seq"] = (row.get("seq") or 0) + 1
	frappe.db.set_value(table, row["name"], values, update_modified=False)
	row.update(values)


def _insert(table, name, **values):
	row = {"doctype": table, "name": name, **values}
	frappe.get_doc(row).insert(ignore_permissions=True)
	return row


def _insert_new(table, name, **values):
	"""Insert a row whose key must be new, or return None when a unique key already holds it.

	The unique key is the check: there is no locking read of the key first, because a locking
	read of a key that does not exist takes a gap lock on MariaDB (REPEATABLE READ), and two
	transactions holding gap locks then deadlock on each other's INSERT. On Postgres the
	duplicate ends the transaction; the caller writes nothing more and commits (a rollback).
	Any other failure is raised; an RQ job timeout leaves as a fresh instance."""
	guard = safe_call.InterruptGuard()
	try:
		with guard:
			return _insert(table, name, **values)
	except Exception as exc:
		if not is_duplicate_key(exc):
			raise
	if guard.pending():
		raise guard.interrupt()
	return None


def _signal(signals, signal, reason, **context):
	"""Record a row for the caller to log once its transaction has ended: a release-blocking
	signal (``discarded``, ``duplicate_blocked``) or a call settled as ``uncertain``.
	``signals`` is an optional list the caller owns and clears before each try: a try that is
	rolled back and repeated must not leave rows behind."""
	if signals is not None:
		signals.append({"signal": signal, "reason": reason, **context})


def lock_run(run_id, docname, *, wait=True, force=False):
	"""Lock the run's session row, then the run row, as the transaction's first statements.

	``docname`` is the run's session, resolved by the caller before this transaction (the
	reference is immutable), so no plain read precedes the locks. A read view opened before
	the session lock would turn it into a 1020 on MariaDB 11.8 (snapshot isolation) once the
	lock holder commits a change to that row. Every path locks session, run and attempt in
	that order. A run of another session is reported as missing, so a caller bound to one
	session can never act on another session's run.

	``wait=False`` takes both rows with NOWAIT: a row another transaction holds raises a
	lock-unavailable error at once. A reservation naming another session raises, unless
	``force`` (cancel and recovery, which release it: ``_release``).

	A ``run_id`` that is not a non-empty string of at most 140 characters names no run, and
	the Run table is not read: a filter in its place would be a locking read by filter."""
	if not isinstance(docname, str) or not docname:
		return None, None
	parent = _read("Optimus Session", docname, lock=True, wait=wait)
	if not isinstance(run_id, str) or not run_id or len(run_id) > 140:
		return parent, None
	run = _read(RUN, run_id, lock=True, wait=wait)
	if not run or run.get("session_name") != docname:
		return parent, None
	if not force and run.get("active_session") not in (None, "", docname):
		raise JournalUnavailable("Refresh journal reservation is inconsistent")
	return parent, run


def expired(run, now):
	"""True when an active run can no longer progress: its lease (a worker's, or the queued
	delivery's) or its total deadline has passed. A late worker is already fenced; the run is
	ended as interrupted by its session's next admission or by the recovery sweep."""
	return bool(
		run
		and run.get("state") in ACTIVE
		and ((run.get("lease_until") or 0) <= now or (run.get("deadline") or 0) <= now)
	)


def expiry_reason(run, now):
	return "lease_expired" if (run.get("lease_until") or 0) <= now else "deadline"


def admit(
	*,
	run_id,
	docname,
	session_uuid,
	requested_by,
	scope,
	now,
	max_seconds,
	site_cap,
	user_cap,
	cap,
	include_fixes,
	include_steps,
	regenerate_all,
	resume_from=None,
	requested_at_epoch=None,
	retry_uncertain=False,
	gated=0,
	excluded=0,
):
	"""Reserve a session and, for a user refresh, enforce the site/user caps.

	Caller checks permissions, provider configuration, Phase 2 and workers before admission,
	and starts a fresh transaction (``ai_jobs._retry_sql``). No queue call happens here.

	Lock order (Q4): the session row first, then, only for ``scope == "all"`` (the only scope
	with caps), the site mutex with NOWAIT; a held mutex is a ``busy`` refusal, never a wait
	and never an error. An admission waiting on its own session therefore never holds the
	mutex. Analyze-time ``fixes_missing`` runs never take it.

	Every read of other runs is a plain read after those locks, so its read view opens after
	every admission that could change the count has committed: admissions with caps serialize
	on the mutex, and on Postgres an older snapshot fails on the mutex row the winner changed
	(40001), which the caller retries. No FOR UPDATE by filter: next-key locks on the run
	indexes deadlocked other sessions' workers. The unique ``active_session`` key is the
	authority for one active run per session: an INSERT that hits it is ``already_running``
	(with ``run`` None when this transaction could not see that run).

	The caps count live runs only: active, with lease and deadline not passed. A run that is
	``expired`` can neither claim a slice nor begin a call, so it holds no slot, even while it
	still holds its own session's reservation until that session's next admission or the
	sweep ends it. Every run that can still call a provider is counted.

	Lazy takeover (A2): a run that holds the reservation but whose lease or deadline has
	passed (``expired``) is ended in this transaction, as the recovery sweep would end it:
	interrupted, its Calling attempt uncertain (never sent again), its reservation released
	(``_release``; an inconsistent row is force-released). The admission then goes on, so a
	crashed worker never needs the scheduler or a manual cancel. The taken-over run comes back as
	``taken_over`` with every answer, a refusal included, since the takeover commits with it.

	``gated`` and ``excluded`` are the selection's counts (``AiSelection``), taken by the caller
	at admission and kept on the run for the page's toasts.

	Refresh count (Q1): a user refresh (``scope == "all"``, a resume included) adds one to the
	session's ``ai_refresh_count`` here, once the reservation is inserted, and nowhere else.
	The finish of a run never counts, whatever its ending. Analyze-time ``fixes_missing`` runs
	are not a click and count nothing.
	"""
	if scope not in {"all", "fixes_missing"} or (regenerate_all and scope != "all"):
		raise ValueError("Invalid refresh scope")
	if not _timestamp(now) or (requested_at_epoch is not None and not _timestamp(requested_at_epoch)):
		raise ValueError("Invalid refresh timestamp")
	if (
		any(type(n) is not int or n < 1 for n in (site_cap, user_cap))
		or type(max_seconds) is not int
		or not 60 <= max_seconds <= 86400
		or type(cap) is not int
		or not 0 <= cap < 2**31
		or any(type(n) is not int or not 0 <= n < 2**31 for n in (gated, excluded))
		or any(
			type(flag) is not bool for flag in (include_fixes, include_steps, regenerate_all, retry_uncertain)
		)
	):
		raise ValueError("Invalid refresh limits")
	if any(
		not isinstance(value, str) or not value or len(value) > 140
		for value in (run_id, docname, session_uuid, requested_by)
	):
		raise ValueError("Invalid refresh identity")
	if resume_from and (not isinstance(resume_from, str) or len(resume_from) > 140):
		raise ValueError("Invalid refresh resume identity")
	parent, busy = _lock_or_busy(lambda: _read("Optimus Session", docname, lock=True))
	if busy:
		# Another writer held the session row for a whole lock wait timeout: the HTTP caller
		# answers busy instead of waiting a second timeout (F-5).
		return {"status": "refused", "reason": "busy"}
	if not parent or parent.get("session_uuid") != session_uuid or parent["status"] != "Ready":
		return {"status": "refused", "reason": "not_ready"}
	control = None
	if scope == "all":
		control, busy = _lock_site_mutex()
		if busy:
			return {"status": "refused", "reason": "busy"}
		if not control:
			raise JournalUnavailable(
				"AI refresh storage is not initialized; repair: bench --site <site> execute "
				"optimus.patches.v0_12_0.initialize_ai_refresh_store.execute"
			)
	# The run being resumed belongs to this session, so its row is locked after the session.
	previous = _read(RUN, resume_from, lock=True) if resume_from else None
	# Plain reads from here on: every lock above comes first (see the docstring).
	current = frappe.db.get_value(RUN, {"active_session": docname}, "*", as_dict=True)
	taken_over = None
	if current and expired(current, now):
		# Locked after the plain read, by primary key: every writer of this run locks this
		# session first, which this transaction holds, so the row cannot change in between
		# (no 1020 on MariaDB 11.8; an older Postgres snapshot fails with 40001 and retries).
		if previous and previous["name"] == current["name"]:
			locked = previous  # resuming the expired run itself: it must be ended first
		else:
			locked = _read(RUN, current["name"], lock=True)
		current = locked if locked and locked.get("active_session") == docname else None
		if current and expired(current, now):
			taken_over = _release(
				current, docname, state="interrupted", reason=expiry_reason(current, now), now=now
			)
			current = None
	if current:
		return {"status": "already_running", "run": current}
	retry_no = 0
	steps_state, render_pending = "not_requested", 0
	if resume_from:
		if not previous or previous["session_name"] != docname or previous["scope"] != scope:
			raise ValueError("Invalid refresh resume parent")
		if previous["state"] not in {"stopped", "cancelled", "interrupted"}:
			return _answer(taken_over, status="refused", reason="not_resumable")
		if frappe.db.get_value(RUN, {"resume_from": resume_from}, "name"):
			return _answer(taken_over, status="refused", reason="already_resumed")
		retry_no = previous.get("retry_no", 0)
		if type(retry_no) is not int or retry_no < 0:
			raise JournalUnavailable("Refresh journal retry count is inconsistent")
		retry_no += 1
		if retry_no > 3:
			return _answer(taken_over, status="refused", reason="retry_limit")
		requested_at_epoch = previous.get("requested_at_epoch")
		cap = previous.get("cap", cap)
		if (
			not _timestamp(requested_at_epoch)
			or type(cap) is not int
			or not 0 <= cap < 2**31
			or any(
				type(previous.get(field, 0)) not in (int, bool) or previous.get(field, 0) not in (0, 1)
				for field in ("include_fixes", "include_steps", "regenerate_all")
			)
			or (scope != "all" and previous.get("regenerate_all"))
		):
			raise JournalUnavailable("Refresh journal selection is inconsistent")
		include_fixes = bool(previous.get("include_fixes", include_fixes))
		include_steps = bool(previous.get("include_steps", include_steps))
		regenerate_all = bool(previous.get("regenerate_all", regenerate_all))
		if previous.get("steps_state") in {"updated", "carried"}:
			steps_state = "carried"
		render_pending = int(bool(previous.get("render_pending")))
	if scope == "all":
		active = frappe.db.get_values(
			RUN,
			# the complement of ``expired``: a dead run of another session holds no slot
			{
				"scope": "all",
				"state": ["in", sorted(ACTIVE)],
				"lease_until": [">", now],
				"deadline": [">", now],
			},
			["requested_by"],
			as_dict=True,
			limit=site_cap + 1,
		)
		if len(active) >= site_cap:
			return _answer(taken_over, status="refused", reason="site_cap")
		if sum(row["requested_by"] == requested_by for row in active) >= user_cap:
			return _answer(taken_over, status="refused", reason="user_cap")
	run = _insert_new(
		RUN,
		run_id,
		run_id=run_id,
		session_name=docname,
		session_uuid=session_uuid,
		requested_by=requested_by,
		active_session=docname,
		scope=scope,
		state="queued",
		slice_no=0,
		requested_at_epoch=now if requested_at_epoch is None else requested_at_epoch,
		deadline=now + max_seconds,
		lease_until=now + 1800,
		heartbeat=now,
		dispatch_pending=1,
		dispatched_at_epoch=0,
		seq=0,
		cap=cap,
		include_fixes=int(include_fixes),
		include_steps=int(include_steps),
		regenerate_all=int(regenerate_all),
		resume_from=resume_from,
		retry_no=retry_no,
		retry_uncertain=int(retry_uncertain),
		render_pending=render_pending,
		steps_state=steps_state,
		attempted=0,
		completed=0,
		failed=0,
		skipped=0,
		uncertain=0,
		consecutive_failures=0,
		tokens_reported=0,
		usage_incomplete=0,
		total_items=0,
		gated_count=gated,
		excluded_count=excluded,
	)
	if run is None:
		return _answer(taken_over, status="already_running", run=None)
	if scope == "all":
		# One bump per accepted user refresh, a resume included (Q1), in the transaction that
		# reserves the session: a refused admission never counts. #72's counter is
		# savepoint-wrapped and log-and-continue, like its Refresh click.
		from optimus.analyze import _bump_ai_refresh_count

		_bump_ai_refresh_count(docname)
	if control is not None:
		# PostgreSQL REPEATABLE READ does not refresh even a locking SELECT's snapshot.
		# Changing the mutex row makes an older admission fail serialization on it and
		# retry, instead of missing the new active reservation.
		_update(CONTROL, control, generation=(control.get("generation") or 0) + 1)
	return _answer(taken_over, status="queued", run=run)


def _answer(taken_over, **out):
	"""An admission's answer, with the run it took over (``taken_over``) when there was one."""
	if taken_over is not None:
		out["taken_over"] = taken_over
	return out


def _lock_site_mutex():
	"""``(control row, busy)``: the site mutex locked with NOWAIT, or ``busy`` when another
	admission holds it."""
	return _lock_or_busy(
		lambda: frappe.db.get_value(CONTROL, "site", "*", as_dict=True, for_update=True, wait=False)
	)


def _lock_or_busy(read):
	"""``(row, busy)``: the locking ``read()``, or ``busy`` when its lock was not granted (a
	NOWAIT conflict or a lock wait timeout: MariaDB 1205, Postgres 55P03, raw or as Frappe's
	QueryTimeoutError). Any other failure is raised; an RQ job timeout leaves as a fresh
	instance."""
	guard = safe_call.InterruptGuard()
	try:
		with guard:
			return read(), False
	except Exception as exc:
		if not is_lock_unavailable(exc):
			raise
	if guard.pending():
		raise guard.interrupt()
	return None, True


def owns(run, worker_token, now):
	"""True when ``worker_token`` is the live owner of the running ``run`` (public: the worker
	checks it between items)."""
	return bool(
		worker_token
		and run
		and run["state"] == "running"
		and run.get("active_session")
		and run.get("worker_token") == worker_token
		and (run.get("lease_until") or 0) > now
	)


def claim(run_id, *, docname, slice_no, worker_token, now, lease_seconds, signals=None):
	"""Claim a queued slice once. Replaying even the same token is a no-op.

	A run whose queued lease or deadline has passed (``expired``) is dead, whatever the queue
	delivers late: it is not claimed, so it cannot be revived beside a path that treated it as
	dead. The session's next admission or the recovery sweep ends it."""
	parent, run = lock_run(run_id, docname)
	if (
		run
		and run.get("active_session")
		and not expired(run, now)
		and slice_no <= (run["slice_no"] or 0)
		and (run["state"] != "queued" or run["slice_no"] != slice_no)
	):
		# A delivery of a slice that was already claimed (or is already over): the SQL claim
		# is the fence, so no second worker runs it, and this is the only trace of it.
		_signal(signals, "duplicate_blocked", "already_claimed", slice_no=slice_no)
	if (
		not parent
		or not run
		or not run.get("active_session")
		or run["state"] != "queued"
		or run["slice_no"] != slice_no
		or expired(run, now)
	):
		return None
	if not worker_token or lease_seconds <= 0 or run["deadline"] <= now or parent["status"] != "Ready":
		return None
	_update(
		RUN,
		run,
		state="running",
		worker_token=worker_token,
		lease_until=now + lease_seconds,
		dispatch_pending=0,
		heartbeat=now,
	)
	return run


def attempt_name(run_id, kind, target):
	# Stable identity for one item in one run. An interrupted delivery cannot
	# create another billed attempt by choosing a different worker token.
	return hashlib.sha256(f"{run_id}\0{kind}\0{target}".encode()).hexdigest()[:40]


def begin_attempt(run_id, *, docname, worker_token, kind, target, input_hash, now, signals=None):
	"""Persist the send intent before HTTP. Returning None forbids a new call."""
	if kind not in {"fix", "steps"} or not isinstance(target, str) or not target:
		raise ValueError("Invalid refresh target")
	parent, run = lock_run(run_id, docname)
	if not parent or parent["status"] != "Ready" or not owns(run, worker_token, now):
		return None
	if run["deadline"] <= now or run.get("active_attempt") or run.get("breaker_kind"):
		return None
	name = attempt_name(run_id, kind, target)
	attempt = _insert_new(
		ATTEMPT,
		name,
		attempt_id=name,
		run_id=run_id,
		kind=kind,
		session_name=run["session_name"],
		target_name=target,
		input_hash=input_hash,
		worker_token=worker_token,
		state="calling",
		started_at_epoch=now,
		tokens_reported=0,
		usage_complete=0,
		settled=0,
	)
	if attempt is None:
		# this target was already attempted in this run: the unique key blocked a second send
		_signal(signals, "duplicate_blocked", "already_attempted", attempt=name, kind=kind)
		return None
	_update(RUN, run, active_attempt=name, attempted=run["attempted"] + 1, heartbeat=now)
	return attempt


def attempt_settled(attempt_id):
	"""The attempt's ``settled`` bit as committed (a plain read; the caller starts fresh)."""
	return bool(frappe.db.get_value(ATTEMPT, attempt_id, "settled"))


def settle_attempt(
	run_id,
	attempt_id,
	*,
	docname,
	worker_token,
	outcome,
	tokens,
	usage_complete,
	now,
	persist=None,
	error_kind="",
	signals=None,
):
	"""Save an outcome and its result, at most once, and add its usage to the session.

	The original worker may reconcile a known late outcome after cancellation
	or expiry. It may count its reported usage, but cannot write a stale answer.
	``persist`` is a database-only result write, never a provider call or commit.
	The usage goes to ``analyze._add_ai_spend`` in this transaction: a failure that
	ends the whole transaction rolls everything back (the caller retries), while a
	statement-level counter failure is logged by the counter and the outcome commits
	without it (#72, D2). ``tokens_reported`` always records the tokens.
	"""
	if (
		outcome not in {"succeeded", "failed", "skipped", "uncertain"}
		or type(tokens) is not int
		or tokens < 0
		or type(usage_complete) is not bool
	):
		raise ValueError("Invalid refresh outcome")
	answer = outcome == "succeeded"
	parent, run = lock_run(run_id, docname)
	if not run:
		if answer:
			# no attempt row to settle, so no idempotency key: the tokens are not charged (a lost
			# COMMIT acknowledgement would let the fallback charge them again); this row is their
			# only record
			_signal(signals, "discarded", "fenced", attempt=attempt_id, tokens=tokens)
		return False
	attempt = _read(ATTEMPT, attempt_id, lock=True)
	if not attempt or attempt.get("run_id") != run_id or attempt.get("worker_token") != worker_token:
		if answer:
			# no attempt row to settle, so no idempotency key: the tokens are not charged (a lost
			# COMMIT acknowledgement would let the fallback charge them again); this row is their
			# only record
			_signal(signals, "discarded", "fenced", attempt=attempt_id, tokens=tokens)
		return False
	if attempt.get("settled") or attempt["state"] not in {"calling", "uncertain"}:
		if answer:
			_signal(signals, "discarded", "settled", attempt=attempt_id, kind=attempt["kind"])
		return False
	session_ready = bool(parent and parent["status"] == "Ready")
	owner = bool(run["deadline"] > now and owns(run, worker_token, now))
	can_write = session_ready and owner
	final = "discarded" if answer and not can_write else outcome
	reason = "fenced" if answer and not owner else "session_changed"
	if answer and can_write and persist is not None:
		if persist() is False:
			final, reason = "discarded", "input_changed"
	if final == "discarded":
		_signal(signals, "discarded", reason, attempt=attempt_id, kind=attempt["kind"])
	if tokens and parent:
		from optimus.analyze import _add_ai_spend

		_add_ai_spend(parent["name"], tokens)
	was_uncertain = attempt["state"] == "uncertain"
	if final == "uncertain" and not was_uncertain:
		# its outcome is unknown and it is never sent again: one row, so the operator can check
		# the provider's usage (a cancel or a takeover that made it uncertain wrote its own)
		_signal(signals, "uncertain", error_kind, attempt=attempt_id)
	incomplete = not usage_complete
	_update(
		ATTEMPT,
		attempt,
		state=final,
		settled=1,
		finished_at_epoch=now,
		tokens_reported=tokens,
		usage_complete=int(not incomplete),
		error_kind=error_kind,
	)
	counter = {
		"succeeded": "completed",
		"failed": "failed",
		"skipped": "skipped",
		"discarded": "skipped",
		"uncertain": "uncertain",
	}[final]
	counts = {field: run.get(field, 0) or 0 for field in ("completed", "failed", "skipped", "uncertain")}
	counts["uncertain"] -= int(was_uncertain)
	counts[counter] += 1
	streak = run.get("consecutive_failures") or 0
	if final == "failed" and error_kind not in PER_PROMPT_KINDS:
		streak += 1
	elif final == "succeeded":
		streak = 0
	breaker = run.get("breaker_kind") or ""
	# Q2, #72's rule: stop at once only when the operator must act (``AI_FATAL_KINDS``);
	# any other failure, a rate limit included, counts toward three in a row.
	if final == "failed" and (error_kind in ai_fix.AI_FATAL_KINDS or streak >= 3):
		breaker = error_kind or "internal"
	_update(
		RUN,
		run,
		**counts,
		steps_state=(
			{"succeeded": "updated", "discarded": "kept"}.get(final, final)
			if attempt["kind"] == "steps"
			else run.get("steps_state")
		),
		consecutive_failures=streak,
		breaker_kind=breaker,
		render_pending=int(bool(run.get("render_pending")) or final == "succeeded"),
		tokens_reported=(run.get("tokens_reported") or 0) + tokens,
		usage_incomplete=max(0, (run.get("usage_incomplete") or 0) - int(was_uncertain)) + int(incomplete),
		active_attempt=None if run.get("active_attempt") == attempt_id else run.get("active_attempt"),
		heartbeat=now,
	)
	return True


def _finish_locked(run, *, state, reason, now):
	"""Write the terminal state and release the reservation. It counts nothing: the refresh
	was counted once, when it was accepted (``admit``)."""
	_update(
		RUN,
		run,
		state=state,
		end_reason=reason,
		ended_at_epoch=now,
		active_session=None,
		worker_token=None,
		lease_until=0,
		dispatch_pending=0,
	)
	return run


def finish(run_id, *, docname, worker_token, now, reason):
	"""Write the terminal state and release the session reservation."""
	_, run = lock_run(run_id, docname)
	if not owns(run, worker_token, now):
		return None
	if run.get("active_attempt"):
		raise RuntimeError("Cannot finish with an unsettled provider call")
	return _finish_locked(
		run, state="complete" if reason == "complete" else "stopped", reason=reason, now=now
	)


def _mark_uncertain(run, reason, *, force=False):
	"""The run's open Calling attempt becomes uncertain: never sent again, its usage unknown.

	An attempt the run cannot vouch for (missing, another run's or another worker's) raises,
	unless ``force``. Then only this run's own Calling attempt is written; for a missing row
	or another run's, the run itself counts an uncertain call of unknown usage. Returns True
	when the attempt was inconsistent. The run names the Calling attempt it made uncertain in
	``uncertain_attempt``, for the caller's one Error Log row."""
	name = run.get("active_attempt")
	inconsistent = False
	if name:
		attempt = _read(ATTEMPT, name, lock=True)
		if (
			not attempt
			or attempt.get("run_id") != run["name"]
			or attempt.get("worker_token") != run.get("worker_token")
		):
			if not force:
				raise JournalUnavailable("Refresh journal attempt is inconsistent")
			inconsistent = True
			if attempt and attempt.get("run_id") != run["name"]:
				attempt = None
		if not attempt or attempt["state"] == "calling":
			if attempt:
				_update(ATTEMPT, attempt, state="uncertain", error_kind=reason)
				run["uncertain_attempt"] = name
			_update(
				RUN,
				run,
				uncertain=(run.get("uncertain") or 0) + 1,
				usage_incomplete=(run.get("usage_incomplete") or 0) + 1,
			)
	_update(RUN, run, active_attempt=None)
	return inconsistent


def _release(run, docname, *, state, reason, now):
	"""End an active run: its open call uncertain, its session reservation released.

	An inconsistent row is force-released (E2) rather than holding its session, and a site
	cap slot, until the journal is repaired by hand. Release fails open, spend fails closed:
	a reservation naming another session is cleared, an attempt that is not this run's is
	never written (the run counts an uncertain call of unknown usage instead). The run comes
	back with ``inconsistent`` naming what was wrong and ``uncertain_attempt`` naming the call
	it made uncertain, for the caller's Error Log rows (``ai_jobs._log_ending``)."""
	problems = []
	if run.get("active_session") not in (None, "", docname) or run.get("session_name") != docname:
		problems.append("reservation")
	if _mark_uncertain(run, reason, force=True):
		problems.append("attempt")
	_finish_locked(run, state=state, reason=reason, now=now)
	if problems:
		run["inconsistent"] = " and ".join(problems)
	return run


def interrupt(run_id, *, docname, now, reason, wait=True):
	"""Recover an expired run without automatically repeating its call.

	``wait=False`` (the recovery sweep) takes the session row with NOWAIT, so a row held by a
	dead or slow transaction raises a lock-unavailable error at once instead of stalling the
	sweep for a lock wait timeout. An inconsistent run is force-released (``_release``)."""
	_, run = lock_run(run_id, docname, wait=wait, force=True)
	if not expired(run, now):
		return None
	return _release(run, docname, state="interrupted", reason=reason, now=now)


def continue_run(run_id, *, docname, worker_token, now, queued_seconds=1800, progressed=True):
	"""Write the next slice's dispatch intent before the queue sees it.

	The dispatch throttle restarts with the slice, so a quick yield is dispatched at once
	instead of waiting for the sweep (R3). A slice that could not begin any item
	(``progressed`` False) adds to the run's idle streak; ``IDLE_SLICE_LIMIT`` such slices in
	a row stop the run with ``no_progress`` instead of respinning until its deadline (O5)."""
	_, run = lock_run(run_id, docname)
	if not owns(run, worker_token, now):
		return None
	if run.get("active_attempt"):
		raise RuntimeError("Cannot continue with an unsettled provider call")
	if run["deadline"] <= now:
		return _finish_locked(run, state="stopped", reason="deadline", now=now)
	idle = 0 if progressed else (run.get("idle_slices") or 0) + 1
	if idle >= IDLE_SLICE_LIMIT:
		_update(RUN, run, idle_slices=idle)
		return _finish_locked(run, state="stopped", reason="no_progress", now=now)
	_update(
		RUN,
		run,
		state="queued",
		slice_no=run["slice_no"] + 1,
		worker_token=None,
		lease_until=now + queued_seconds,
		dispatch_pending=1,
		dispatched_at_epoch=0,
		idle_slices=idle,
		heartbeat=now,
	)
	return run


def cancel(run_id, *, docname, now, cancelled_by=None):
	"""Cancel the durable intent even when Redis is down. Caller authorizes the session;
	a run of another session, or one no longer active, is not cancelled (None) and nothing
	changes. An inconsistent run is force-released (``_release``)."""
	_, run = lock_run(run_id, docname, force=True)
	if not run or run["state"] not in ACTIVE:
		return None
	_update(RUN, run, cancelled_by=cancelled_by)
	return _release(run, docname, state="cancelled", reason="cancelled", now=now)


def abandon(run_id, *, docname, worker_token, now):
	"""An escaping worker failure immediately leaves an honest terminal state."""
	_, run = lock_run(run_id, docname)
	if not run or run["state"] != "running" or not worker_token or run.get("worker_token") != worker_token:
		return None
	_mark_uncertain(run, "worker_interrupted")
	return _finish_locked(run, state="interrupted", reason="worker_interrupted", now=now)


def prepare_dispatch(run_id, *, docname, now, wait=True):
	"""Throttle delivery of a committed SQL intent; claiming acknowledges it.

	Keep the intent pending until a worker claims it, including when Redis
	accepts the job but the enqueue acknowledgement or its transaction is lost.
	The deterministic RQ id saves queue space; claim is the execution fence.
	``wait=False`` (the recovery sweep) takes the session row with NOWAIT.
	"""
	parent, run = lock_run(run_id, docname, wait=wait)
	if not parent or not run or run["state"] != "queued" or not run.get("dispatch_pending"):
		return None
	if (run.get("lease_until") or 0) <= now or run["deadline"] <= now:
		return None
	previous = run.get("dispatched_at_epoch") or 0
	if previous and now - previous < DISPATCH_INTERVAL:
		return None
	_update(RUN, run, dispatched_at_epoch=now)
	return run


def checkpoint(run_id, *, docname, worker_token, now, **values):
	"""Publish bounded progress only while the slice owns this generation."""
	if set(values) - {"total_items", "steps_state", "blocked_uncertain"}:
		raise ValueError("Invalid refresh progress fields")
	parent, run = lock_run(run_id, docname)
	if not parent or parent["status"] != "Ready" or not owns(run, worker_token, now):
		return None
	_update(RUN, run, heartbeat=now, **values)
	return run
