# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Queued AI enrichment. SQL owns progress; RQ delivery can be duplicated.

No public endpoint lives here. Callers must authorize admission and polling.
The worker rechecks authorization and inputs before sending and saving each
item. A Calling record commits before HTTP, without a journal or Session row
lock spanning the request. Result and outcome commit in one short SQL transaction
that also adds the usage to the session counter; the counter is log-and-continue
(#72), so the journal's ``tokens_reported`` is the reconciliation source.
SQL conflicts are retried as whole short transactions, never as provider calls.
"""

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import frappe

from optimus import ai_fix, safe_call, safe_commit
from optimus import ai_refresh_store as store

_JOB_METHOD = "optimus.ai_jobs.run_ai_refresh_slice"
# Seconds an HTTP admission or cancellation waits for a row lock on Postgres before it answers
# ``busy`` (``_bound_http_lock_wait``). MariaDB keeps the server's ``innodb_lock_wait_timeout``.
HTTP_LOCK_WAIT_SECONDS = 30
# The failure kinds the journal records under their own name; any other becomes ``internal``.
# They are #72's: every kind ``ai_fix`` classifies (``KIND_HINTS`` has one line for each, and
# holds the skip and fatal kinds), so a kind #72 adds is recorded as itself and, when it is
# fatal, stops the run at once. The engine adds no kind of its own.
_ERROR_KINDS = frozenset(ai_fix.KIND_HINTS)


class RefreshSliceFailed(RuntimeError):
	"""A type-only worker failure; never carries provider data or request text."""


class StopRefresh(Exception):
	def __init__(self, reason):
		self.reason = reason
		super().__init__(reason)


@dataclass
class PreparedItem:
	kind: str
	target: str
	input_hash: str
	send: Callable
	valid: Callable
	persist: Callable


def _now():
	return time.time()


def _setting_int(name, default, minimum, maximum):
	"""A whole-number site setting. ``bench set-config`` stores a string unless it is given
	``-p``, so a string of digits is read as its number; anything else out of range
	raises, naming the setting and the command that sets it."""
	value = frappe.conf.get(name, default)
	if type(value) is str and len(value) <= 12 and value.strip().isascii() and value.strip().isdecimal():
		value = int(value.strip())
	if type(value) is not int or not minimum <= value <= maximum:
		from frappe import _

		raise ValueError(
			_(
				"Invalid site configuration {0}: use a whole number from {1} to {2}, for example "
				"bench --site <site> set-config -p {0} {3}"
			).format(name, minimum, maximum, default)
		)
	return value


def slice_seconds():
	return _setting_int("optimus_ai_slice_seconds", 120, 30, 1800)


def _call_timeout():
	return ai_fix._resolve_timeout_seconds()


def _transaction(operation):
	"""A short owned transaction, with fresh interrupts after rollback."""
	guard = safe_call.InterruptGuard(base=True)
	failure = result = None
	try:
		with guard:
			result = operation()
			safe_commit()
	except Exception as exc:
		failure = exc
	if failure is not None or guard.pending():
		rollback_error = None
		try:
			with guard:
				frappe.db.rollback()
		except Exception as exc:
			rollback_error = type(exc).__name__
		if guard.pending():
			operation = result = failure = None
			raise guard.interrupt()
		if rollback_error:
			operation = result = failure = None
			raise RefreshSliceFailed(f"AI refresh transaction rollback failed ({rollback_error})")
		raise failure
	return result


def _read_run(run_id):
	return frappe.db.get_value(store.RUN, run_id, "*", as_dict=True)


def _session_of(run_id):
	"""The run's session, read and committed in its own short transaction.

	The reference is immutable, so every later journal transaction can lock that session
	row as its first statement (``store.lock_run``) instead of reading the run first."""
	return _transaction(lambda: (_read_run(run_id) or {}).get("session_name"))


def _retry_sql(operation, *, retry_lock_waits=True):
	"""Run one journal operation in its own short transaction; retry SQL conflicts only.

	The caller's transaction is committed first, so the operation's first statement (the
	locking read of the session row) opens the transaction. A read view opened before it
	would turn that lock into a 1020 on MariaDB 11.8 once the lock holder commits.

	A conflict (``store.is_retryable_conflict``: a deadlock, a lock wait, a snapshot or
	serialization conflict, raw or as Frappe wraps it) rolls back and runs the operation
	again, at most three transactions in all, with a short delay. Anything else is raised
	at once. The operation never contacts a provider, so a retry never repeats a call. A
	busy admission mutex is a refusal returned by ``store.admit``, not a conflict.

	``retry_lock_waits=False`` (an HTTP admission) raises a lock wait timeout (MariaDB 1205,
	Postgres 55P03) at once instead of waiting another timeout; deadlocks, snapshot and
	serialization conflicts are still retried. Worker and settlement paths retry it.
	"""
	_transaction(lambda: None)
	for attempt in range(3):
		guard = safe_call.InterruptGuard(base=True)
		failure = None
		try:
			with guard:
				return _transaction(operation)
		except Exception as exc:
			failure = exc
		if guard.pending():
			operation = failure = None
			raise guard.interrupt()
		if (
			not store.is_retryable_conflict(failure)
			or (not retry_lock_waits and store.is_lock_unavailable(failure))
			or attempt == 2
		):
			raise failure
		failure = None
		time.sleep(0.02 * (attempt + 1))


def _bound_http_lock_wait():
	"""On Postgres, end this transaction's row lock waits after ``HTTP_LOCK_WAIT_SECONDS`` at
	most: the first statements of an HTTP admission or cancellation.

	Postgres waits for a row lock for as long as it takes unless ``lock_timeout`` is set, and
	Frappe sets none, so a click on a session row that a dead or slow transaction holds would
	wait until the web server killed the request, and its backend would go on waiting. The
	current limit is read first: a stricter one of the role, the database or the server is kept;
	otherwise ``SET LOCAL`` sets the bound, which ends with this transaction, so the pooled
	connection keeps its own setting. ``SHOW`` and ``SET`` take no snapshot (a SELECT would), so
	the session lock is still the first read. A lock not granted in time raises 55P03, which both
	paths answer as ``busy``. MariaDB gets no statement: a session-level one would outlive the
	transaction on the pooled connection, so the server's ``innodb_lock_wait_timeout`` applies.
	Worker paths retry a lock wait and set nothing."""
	if frappe.db.db_type != "postgres":
		return
	rows = frappe.db.sql("SHOW lock_timeout", as_dict=True)
	current = _pg_milliseconds(rows[0].get("lock_timeout")) if rows else None
	if current and current <= HTTP_LOCK_WAIT_SECONDS * 1000:
		return
	frappe.db.sql("SET LOCAL lock_timeout = %s", (f"{HTTP_LOCK_WAIT_SECONDS}s",))


_PG_TIME_UNITS = {"ms": 1, "s": 1000, "min": 60_000, "h": 3_600_000, "d": 86_400_000}


def _pg_milliseconds(value):
	"""A Postgres time setting as ``SHOW`` prints it (``0``, ``250ms``, ``2s``, ``1min``) in
	milliseconds, or None when it cannot be read."""
	match = re.fullmatch(r"(\d+)(ms|s|min|h|d)?", str(value).strip())
	return int(match[1]) * _PG_TIME_UNITS[match[2] or "ms"] if match else None


def run_ai_refresh_slice(run_id: str, slice_no: int = 0) -> None:
	"""RQ entry: replaying a claimed generation cannot send another request."""
	if (
		not isinstance(run_id, str)
		or not run_id
		or len(run_id) > 140
		or type(slice_no) is not int
		or slice_no < 0
	):
		raise ValueError("Invalid AI refresh delivery")
	worker_token = uuid.uuid4().hex
	guard = safe_call.InterruptGuard(base=True)
	failure = docname = None
	previous = getattr(frappe.local, "optimus_analyzing", False)
	frappe.local.optimus_analyzing = True
	try:
		with guard:
			docname = _session_of(run_id)
			if docname:
				_drive(run_id, docname, slice_no, worker_token)
	except Exception as exc:
		failure = exc
	finally:
		frappe.local.optimus_analyzing = previous
	if failure is not None or guard.pending():
		# A failure before claim is harmless; after send the committed intent
		# becomes uncertain. If SQL itself is down it stays Calling until recovery.
		cleanup_error = released = None
		unsettled = getattr(failure, _UNSETTLED_ATTR, None)
		cut = getattr(failure, _CUT_ATTR, None)
		try:
			with guard:
				frappe.db.rollback()
				if docname:
					released = _retry_sql(
						lambda: store.abandon(run_id, docname=docname, worker_token=worker_token, now=_now())
					)
		except Exception as exc:
			cleanup_error = exc
		failure_type = type(failure).__name__
		log_failure_type = ""
		try:
			with guard:
				if failure is not None:
					ai_fix.log_ai_failure(
						"optimus ai refresh worker",
						failure,
						docname=docname,
						run_id=run_id,
						**_unsettled_context(unsettled, released),
					)
				if cleanup_error is not None:
					ai_fix.log_ai_failure(
						"optimus ai refresh cleanup", cleanup_error, docname=docname, run_id=run_id
					)
				_log_ending(released, docname)  # the interrupted run, and the call it left uncertain
				_transaction(lambda: None)  # preserve scrubbed logs before RQ rolls back
		except Exception as exc:
			log_failure_type = type(exc).__name__
		failure = cleanup_error = None
		if guard.pending():
			raise guard.interrupt()
		if isinstance(cut, safe_call.InterruptGuard) and cut.pending():
			raise cut.interrupt()  # the job timeout that cut the provider call (``_CUT_ATTR``)
		raise RefreshSliceFailed(
			f"AI refresh worker failed ({failure_type}); log persistence failure ({log_failure_type or 'none'})"
		)


def _unsettled_context(unsettled, released):
	"""``tokens`` and ``outcome`` for the worker's last row when the outcome write gave up:
	``_settle``'s outcome, and that the cleanup left the open attempt uncertain."""
	if not unsettled:
		return {}
	tokens, outcome = unsettled
	if released and outcome != _UNKNOWN:
		outcome += "; the attempt is now uncertain"
	return {"tokens": tokens, "outcome": outcome}


def _drive(run_id, docname, slice_no, worker_token):
	started = time.monotonic()
	seconds, call_limit = slice_seconds(), _call_timeout()
	signals = []

	def claim():
		signals.clear()  # a try that is rolled back and repeated leaves nothing behind
		return store.claim(
			run_id,
			docname=docname,
			slice_no=slice_no,
			worker_token=worker_token,
			now=_now(),
			lease_seconds=seconds + call_limit + 180,
			signals=signals,
		)

	run = _retry_sql(claim)
	_log_signals(signals, run_id, docname)
	if not run:
		return
	memo, items = {}, 0  # the slice's scratch dict (``_next_item``)
	while True:
		run = _read_run(run_id)
		if not store.owns(run, worker_token, _now()):
			return
		if run.get("breaker_kind"):
			_end(run_id, docname, worker_token, "breaker")
			return
		remaining = int(run["deadline"] - _now() - 5)
		if remaining < min(15, call_limit):
			_end(run_id, docname, worker_token, "deadline")
			return
		if items and time.monotonic() - started >= seconds:
			_yield(run_id, docname, worker_token)
			return
		reason, item = None, None
		try:
			_check_run(run)
			item = _next_item(run, memo)
		except StopRefresh as exc:
			reason = exc.reason
		if reason:
			_end(run_id, docname, worker_token, reason)
			return
		if item is None:
			latest = _read_run(run_id)
			_end(
				run_id,
				docname,
				worker_token,
				"uncertain" if latest and latest.get("blocked_uncertain") else "complete",
			)
			return
		remaining = int(run["deadline"] - _now() - 5)
		if remaining < min(15, call_limit):
			_end(run_id, docname, worker_token, "deadline")
			return
		# Finish the input/config read view, then recheck in the locked intent
		# transaction. MariaDB may otherwise keep a pre-call snapshot alive.
		_transaction(lambda: None)

		def begin():
			signals.clear()
			return _begin_attempt(
				run, item, run_id=run_id, docname=docname, worker_token=worker_token, signals=signals
			)

		try:
			attempt = _retry_sql(begin)
		except StopRefresh as exc:
			reason = exc.reason
		_log_signals(signals, run_id, docname)
		if reason:
			_end(run_id, docname, worker_token, reason)
			return
		if not attempt:
			_yield(run_id, docname, worker_token, progressed=items > 0)
			return
		remaining = int(run["deadline"] - _now() - 5)
		if remaining < min(15, call_limit):
			_settle(
				lambda: store.settle_attempt(
					run_id,
					attempt["name"],
					docname=docname,
					worker_token=worker_token,
					outcome="skipped",
					tokens=0,
					usage_complete=True,
					now=_now(),
					error_kind="deadline",
				),
				run_id=run_id,
				docname=docname,
				attempt_id=attempt["name"],
				tokens=0,
				session_uuid=run["session_uuid"],
			)
			_end(run_id, docname, worker_token, "deadline")
			return
		result = failure = None
		guard = safe_call.InterruptGuard()
		try:
			result = item.send(min(call_limit, remaining))
		except Exception as exc:
			guard.note(exc)
			failure = exc
		kind = getattr(failure, "kind", "internal") if failure is not None else ""
		kind = kind if kind in _ERROR_KINDS or not kind else "internal"
		usage = getattr(failure, "usage", None) if failure is not None else (result or {}).get("tokens")
		tokens = (usage or {}).get("total_tokens", 0)
		if type(tokens) is not int or tokens < 0:
			tokens = 0
		complete = (
			getattr(failure, "usage_complete", False)
			if failure is not None
			else (result or {}).get("usage_complete", False)
		)
		outcome = (
			"skipped" if kind in ai_fix.AI_SKIP_KINDS else "failed" if failure is not None else "succeeded"
		)
		if guard.pending():
			outcome, kind = "uncertain", "timeout"
		if tokens > ai_fix._MAX_TOKEN_COUNT:
			ai_fix.log_ai_failure(
				"optimus ai refresh item",
				docname=docname,
				run_id=run_id,
				attempt=attempt["name"],
				item=item.kind,
				tokens=tokens,
				outcome=(
					f"clamped: the count is above {ai_fix._MAX_TOKEN_COUNT}, so the session counter "
					"ignores it; the run and the attempt keep it"
				),
			)
		if failure is not None and outcome == "failed":
			ai_fix.log_ai_failure(
				"optimus ai refresh item",
				failure,
				session_uuid=run["session_uuid"],
				docname=docname,
				run_id=run_id,
				attempt=attempt["name"],
				kind=kind,
				item=item.kind,
			)
		failure = None

		def settle(*, final=outcome, persist=True, error_kind=kind):
			signals.clear()
			return store.settle_attempt(
				run_id,
				attempt["name"],
				docname=docname,
				worker_token=worker_token,
				outcome=final,
				tokens=tokens,
				usage_complete=complete is True,
				now=_now(),
				error_kind=error_kind,
				persist=(lambda: item.persist(result)) if persist else None,
				signals=signals,
			)

		def save(operation, fallback=None):
			return _settle(
				operation,
				fallback=fallback,
				run_id=run_id,
				docname=docname,
				attempt_id=attempt["name"],
				tokens=tokens,
				session_uuid=run["session_uuid"],
			)

		# A successful provider reply can fail local validation/storage. The fallback then
		# saves its known cost and a failed outcome without trying that answer again. Any
		# other outcome is saved as it was: an uncertain call stays uncertain (never sent
		# again), a fatal kind still stops the run and a skip stays a skip.
		storage_guard = safe_call.InterruptGuard(base=True)
		try:
			with storage_guard:
				_transaction(lambda: None)  # close any provider/logging transaction
				save(
					settle,
					fallback=lambda: settle(
						final="failed" if outcome == "succeeded" else outcome,
						persist=False,
						error_kind=kind or "internal",
					),
				)
		except Exception as exc:
			# No outcome was saved (that commit or both writes failed): the worker cleanup writes
			# its rows, then raises the job timeout that cut the call, when one did, instead of
			# this error.
			if guard.pending():
				safe_call.best_effort(lambda failure=exc: setattr(failure, _CUT_ATTR, guard), None)
			raise
		if storage_guard.pending():
			item = result = None
			raise storage_guard.interrupt()
		# before a pending job timeout stops the job: the call it cut is one ``uncertain`` row
		_log_signals(signals, run_id, docname)
		if guard.pending():
			item = result = failure = None
			raise guard.interrupt()
		items += 1


def _log_signals(signals, run_id, docname):
	"""Write one Error Log row for each row the store recorded, after the transaction that
	recorded it has ended. ``discarded``: an answer was dropped because its attempt is
	stale or fenced. ``duplicate_blocked``: the journal stopped a second send or a second
	delivery. Each row carries the session, the run and the signal's own context
	(``attempt``, ``kind``, ``slice_no``, ``reason``). ``uncertain``: a call was settled with
	an unknown outcome (``_log_uncertain``). Never raises."""
	if not signals:
		return
	for entry in signals:
		if entry["signal"] == "discarded":
			ai_fix.log_ai_failure("optimus ai refresh discarded", docname=docname, run_id=run_id, **entry)
		elif entry["signal"] == "uncertain":
			_log_uncertain(docname, run_id, entry["attempt"], entry["reason"])
		else:
			ai_fix.log_ai_failure(
				"optimus ai refresh duplicate blocked", docname=docname, run_id=run_id, **entry
			)
	signals.clear()
	_transaction(lambda: None)  # keep the rows: the next read must not start inside this transaction


# On an error ``_settle`` raises: ``(tokens, outcome)``, for the worker cleanup's last row.
_UNSETTLED_ATTR = "_optimus_refresh_unsettled"
# On an error ``_drive`` raises after the job timeout cut the provider call: that call's guard,
# so the worker cleanup raises the timeout again, fresh, once its rows are written.
_CUT_ATTR = "_optimus_refresh_cut_by_timeout"
_COMMITTED_AFTER_ERROR = "committed: the error came after the COMMIT; the outcome and its tokens are saved"
_RECORDED = "recorded: the attempt's outcome and its tokens are saved (read back after the error)"
_UNKNOWN = "unknown: the outcome could not be read back after the error, so these tokens may well be recorded"
_TIMED_OUT = "unknown: the job timed out during the outcome write; the attempt is now uncertain"


def _settle(operation, *, run_id, docname, attempt_id, tokens, session_uuid=None, fallback=None):
	"""Commit one attempt's outcome, its answer and its usage in one short transaction.

	The discipline is ``analyze._write_ai_answer``'s (#72), mirrored rather than reused: the
	engine's write is ``store.settle_attempt``, which locks the session, run and attempt,
	decides whether the answer may still be written (owner, deadline, input recheck) and
	records the outcome, where ``_write_ai_answer`` writes one Finding row and its spend.

	- The first try commits what the caller's transaction holds, so the write starts without
	  a read view from before the provider call (the 1020 trap on MariaDB 11.8).
	- A conflict (``store.is_retryable_conflict``) is rolled back and the write tried again
	  from the start, ``analyze.AI_WRITE_ATTEMPTS`` tries in all. That is #72's
	  ``frappe.QueryDeadlockError`` plus the lock conflicts of the locking reads only this
	  write takes (a lock wait, a Postgres serialization failure). Trying again is safe: the
	  attempt's ``settled`` bit turns a second outcome into a no-op, and the provider is
	  never asked again.
	- Any other failure, or the last try's, is rolled back, and the attempt's ``settled`` bit
	  is read again in a fresh query: when it is set the outcome is committed (the error came
	  after the COMMIT, from an ``after_commit`` callback or a lost reply), so the write counts
	  as done and the error is logged with ``outcome=committed ...``. When it cannot be read,
	  the outcome is unknown and its tokens may well be recorded (#72's wording).
	- When the write did not commit and a ``fallback`` is given (the outcome and the known
	  usage without the answer: a reply's outcome becomes a failure, any other outcome and
	  its kind are kept), it runs next, with the same discipline.
	- Every row is written once both writes ended, with an ``outcome=``: the session
	  counter's own rows (it logs one, ``savepoint_rollback=failed``, when the whole
	  transaction is gone) and each write's failure (with ``tries`` and ``tokens``). When the
	  fallback recorded the tokens, the first write's rows say ``recorded by fallback``, so a
	  reconciler never adds them twice.
	- When neither write saved an outcome, the last error is raised, carrying the tokens and
	  that outcome (``_UNSETTLED_ATTR``) for the worker cleanup's last row. The usage counter is
	  log-and-continue (#72): a statement-level counter failure is logged by the counter
	  and the write still commits, so the journal's ``tokens_reported`` is the source for
	  reconciling the session's ``ai_tokens_spent``.
	- An RQ job timeout during the writes, before an outcome is saved, writes one row with
	  ``tokens`` and ``outcome=unknown: the job timed out during the outcome write ...``,
	  after the ``try``, and leaves as a fresh instance.

	An RQ job timeout leaves as a fresh instance. Returns True once an outcome is saved."""
	reference = {"session_uuid": session_uuid, "docname": docname, "run_id": run_id, "attempt": attempt_id}
	rows, raised, unsettled = (), None, None
	guard = safe_call.InterruptGuard(base=True)
	with guard:
		_result, error, logged, tries, settled = _write_outcome(operation, attempt_id)
		if error is None or settled is True:
			rows = _rows(logged, error, f"committed on try {tries}", error_outcome=_COMMITTED_AFTER_ERROR)
		elif fallback is None:
			rows = _rows(logged, error, _failed(settled, tries), tries=tries, tokens=tokens)
			raised, unsettled = error, _failed(settled, tries)
		else:
			fb_result, fb_error, fb_logged, fb_tries, fb_settled = _write_outcome(fallback, attempt_id)
			if fb_error is None and fb_result:
				# The fallback recorded the outcome and the tokens: the first write did not commit.
				first = f"not committed after try {tries}; recorded by fallback"
				rows = _rows(logged, error, first, tries=tries, tokens=tokens)
				rows += _rows(fb_logged, None, f"committed on try {fb_tries}")
			else:
				# A no-op fallback (the attempt was already settled) or a failed one: the state
				# read back now tells whether the outcome and its tokens are saved.
				final = _attempt_settled(attempt_id) if fb_error is None else fb_settled
				if final is True:
					rows = _rows(logged, error, _RECORDED, tokens=tokens)
					rows += _rows(fb_logged, fb_error, _RECORDED)
				else:
					rows = _rows(logged, error, _failed(final, tries), tries=tries, tokens=tokens)
					rows += _rows(
						fb_logged, fb_error, _failed(final, fb_tries), tries=fb_tries, tokens=tokens
					)
					raised = fb_error if fb_error is not None else error
					unsettled = _failed(final, fb_tries if fb_error is not None else tries)
			fb_error = fb_logged = None
		error = logged = None
	if guard.pending():
		# The job timed out before an outcome was saved (a write, a retry or a read-back waited
		# on locks): the worker cleanup has no failure to carry the reply's known tokens, so
		# this one row does. The cleanup then makes the open attempt uncertain.
		rows = raised = error = logged = fb_error = fb_logged = None
		ai_fix.log_ai_failure(
			"optimus ai refresh result storage", **reference, tokens=tokens, outcome=_TIMED_OUT
		)
		safe_call.best_effort(lambda: _transaction(lambda: None), None)
		raise guard.interrupt()
	for exc, context in rows:
		ai_fix.log_ai_failure("optimus ai refresh result storage", exc, **reference, **context)
	rows = None
	if raised is not None:
		safe_call.best_effort(lambda: setattr(raised, _UNSETTLED_ATTR, (tokens, unsettled)), None)
		raise raised
	return True


def _failed(settled, tries):
	return _UNKNOWN if settled is None else f"not committed after try {tries}"


def _rows(logged, error, outcome, *, error_outcome=None, **context):
	"""One write's Error Log rows: each counter error the counter logged (``outcome``), then
	the write's own error, with ``context``."""
	rows = [(exc, {"outcome": outcome}) for exc in logged if exc is not error]
	if error is not None:
		rows.append((error, {"outcome": error_outcome or outcome, **context}))
	return rows


def _write_outcome(operation, attempt_id):
	"""One settlement write, tried up to ``analyze.AI_WRITE_ATTEMPTS`` times.

	Returns ``(result, error, logged, tries, settled)``: the operation's result, the last
	error (None once it committed), the counter errors the counter logged itself, the tries
	made, and the attempt's state: True once committed or read back as settled, False when
	read back as not settled, None when it could not be read."""
	from optimus.analyze import AI_WRITE_ATTEMPTS

	result = error = None
	logged, tries = [], 0
	while tries < AI_WRITE_ATTEMPTS:
		tries += 1
		error = None
		guard = safe_call.InterruptGuard(base=True)
		try:
			with guard:
				if tries == 1:
					_transaction(lambda: None)
				result = _transaction(operation)
		except Exception as exc:
			error = exc
		if guard.pending():
			operation = error = result = None
			logged.clear()
			raise guard.interrupt()
		if error is None:
			break
		if getattr(error, ai_fix._LOGGED_ATTR, False):
			logged.append(error)
		# Retries more than #72's _write_ai_answer (lock waits, Postgres 40001): only this
		# write takes locking reads, and its settled bit makes a repeat a no-op.
		if not store.is_retryable_conflict(error):
			break
	settled = True if error is None else _attempt_settled(attempt_id)
	return result, error, logged, tries, settled


def _attempt_settled(attempt_id):
	"""The attempt's committed ``settled`` bit, read in a fresh query after the rollback:
	True or False, or None when it cannot be read. An RQ job timeout leaves fresh."""
	state = safe_call.best_effort(lambda: _transaction(lambda: bool(store.attempt_settled(attempt_id))), None)
	return state if isinstance(state, bool) else None


def _begin_attempt(run, item, *, run_id, docname, worker_token, signals=None):
	"""The send-intent transaction: write the Calling attempt, or None to yield.

	Every lock comes before the first plain read: the session and run (``store.lock_run``),
	then the item's rows and the Phase 2 rows (``item.valid()``, which rechecks the input
	and runs ``_check_run``), and only then the requester's plain User read. A read view
	opened earlier would turn the Finding lock into a 1020 on MariaDB 11.8 when a Finding
	writer that skips the session lock (#72's write of a zero-token answer) committed in
	between. When the input is no longer valid, ``_check_run`` raises ``StopRefresh`` if the
	run itself must end; otherwise the slice yields."""
	store.lock_run(run_id, docname)
	if not item.valid():
		_check_run(run, locked=True)
		return None
	return store.begin_attempt(
		run_id,
		docname=docname,
		worker_token=worker_token,
		kind=item.kind,
		target=item.target,
		input_hash=item.input_hash,
		now=_now(),
		signals=signals,
	)


def _end(run_id, docname, worker_token, reason):
	run = _retry_sql(
		lambda: store.finish(run_id, docname=docname, worker_token=worker_token, now=_now(), reason=reason)
	)
	_note_ending(run, docname)
	return run


def _yield(run_id, docname, worker_token, *, progressed=True):
	run = _retry_sql(
		lambda: store.continue_run(
			run_id, docname=docname, worker_token=worker_token, now=_now(), progressed=progressed
		)
	)
	if run and run["state"] == "queued":
		_dispatch_pending(run_id, docname)
	else:
		_note_ending(run, docname)


def _check_run(run, *, locked=False):
	"""Recheck the session, the requester and Phase 2 activity; raise ``StopRefresh``.

	With ``locked`` the session and Phase 2 rows are read with locks, and both locking reads
	come before the first plain read, so a read view opened by a plain read never precedes a
	lock (a 1020 on MariaDB 11.8). The requester's User row is a plain read: a lock on it
	would block the user's own User writes while the journal locks are held. The Phase 2
	read is a FOR UPDATE by filter, the engine's one gap-locking read (not on a journal
	table): on MariaDB another session's Phase 2 insert can wait for this short
	transaction."""
	from optimus.permissions import may_act_on_session

	parent = frappe.db.get_value("Optimus Session", run["session_name"], "*", as_dict=True, for_update=locked)
	if not parent or parent.get("status") != "Ready" or parent.get("session_uuid") != run["session_uuid"]:
		raise StopRefresh("not_ready")
	phase2 = frappe.db.get_values(
		"Optimus Phase Two Run",
		{"parent": parent["name"], "status": ["in", ["Recording", "Analyzing"]]},
		["name"],
		limit=1,
		for_update=locked,
	)
	user = run.get("requested_by")
	if not user or user == "Guest" or not frappe.db.get_value("User", user, "enabled"):
		raise StopRefresh("permission")
	if user != "Administrator" and not {"System Manager", "Optimus User"}.intersection(
		frappe.get_roles(user)
	):
		raise StopRefresh("permission")
	# The access checks get the session built from this row, not its name: given a name, Frappe
	# v15 loads the whole document (every child table) for each check, and two rechecks run in
	# the locked send and save transactions. The owner, share, role and User Permission checks
	# read only the session's own fields; no child table of a session has a Link field.
	session = frappe.get_doc({**parent, "doctype": "Optimus Session"})
	if not may_act_on_session(
		user=user,
		owner=parent.get("owner") or "",
		can_read=bool(frappe.has_permission("Optimus Session", "read", session, user=user)),
		can_write=bool(frappe.has_permission("Optimus Session", "write", session, user=user)),
	):
		raise StopRefresh("permission")
	if phase2:
		raise StopRefresh("phase2")
	return parent


def _signature(doc, row=None):
	"""Stable SQL input identity; never store prompt text in the journal."""
	from optimus.ai_prompts import PROMPT_VERSION

	parent = {k: doc.get(k) for k in ("session_uuid", "recordings_file")}
	if row is None:
		parent.update({k: doc.get(k) for k in ("title", "notes")})
		finding = None
	else:
		# Document.as_dict() includes transient flags and normalizes SQL
		# Decimal/NULL values. Compare only actual inputs with the same numeric
		# representation, otherwise a valid answer can always look stale.
		finding = {
			k: row.get(k)
			for k in (
				"name",
				"parent",
				"parenttype",
				"parentfield",
				"finding_type",
				"severity",
				"title",
				"customer_description",
				"technical_detail_json",
				"action_ref",
				"llm_fix_json",
			)
		}
		finding["estimated_impact_ms"] = float(row.get("estimated_impact_ms") or 0)
		finding["affected_count"] = int(row.get("affected_count") or 0)
	text = json.dumps([PROMPT_VERSION, parent, finding], sort_keys=True, default=str, allow_nan=False)
	return hashlib.sha256(text.encode()).hexdigest()


def _chain(run):
	"""The names of ``run`` and of the runs it resumed, newest first. Bounded by the retry
	limit; a cycle in corrupt data ends the walk."""
	names, name = [], run["name"]
	while name and name not in names and len(names) < 5:
		names.append(name)
		name = (
			run.get("resume_from") if len(names) == 1 else frappe.db.get_value(store.RUN, name, "resume_from")
		)
	return names


def _attempts(run):
	"""``(tried, uncertain, fixes_spent)``: this run's attempts, the session's uncertain
	attempts as ``(kind, target, input_hash)`` keys, and the fix attempts of the whole resume
	chain, which is what the item cap limits: a resume continues the same refresh, so it
	does not get the cap again."""
	fields = ["run_id", "kind", "target_name", "input_hash", "state"]
	chain = _chain(run)
	attempts = frappe.get_all(
		store.ATTEMPT, filters={"run_id": ["in", chain]}, fields=fields, limit_page_length=10001
	)
	uncertain = []
	if not run.get("retry_uncertain"):
		uncertain = frappe.get_all(
			store.ATTEMPT,
			filters={"session_name": run["session_name"], "state": "uncertain"},
			fields=fields,
			limit_page_length=10001,
		)
	if len(attempts) > 10000 or len(uncertain) > 10000:
		raise StopRefresh("history_limit")
	tried = [a for a in attempts if a["run_id"] == run["name"]]
	fixes_spent = sum(a["kind"] == "fix" for a in attempts)
	return tried, {(a["kind"], a["target_name"], a["input_hash"]) for a in uncertain}, fixes_spent


def _valid_item(run, doc, row, fingerprint):
	# The caller already locks Session before Run/Attempt. Current reads here
	# must not consult a pre-wait MariaDB snapshot when another actor changed it.
	# The Finding row is locked before _check_run's plain reads open a read view; the
	# caller (_begin_attempt, settle_attempt) has locked the session and run first.
	parent = frappe.db.get_value("Optimus Session", run["session_name"], "*", as_dict=True, for_update=True)
	if not parent:
		return False
	# A fix item is guarded by its fingerprint (the finding's own fields and the session's
	# recordings) and the Finding row lock, so an unrelated save of the session during the HTTP
	# call (a Desk edit, a counter, a Phase 2 save) does not discard a paid answer. The
	# Steps item rewrites the session's ``notes``, which the Desk form edits, so any session
	# save since the item was prepared makes it stale.
	if row is None and parent.get("modified") != doc.get("modified"):
		return False
	current = None
	if row is not None:
		current = frappe.db.get_value("Optimus Finding", row.name, "*", as_dict=True, for_update=True)
		if (
			not current
			or current.get("parent") != parent["name"]
			or current.get("parenttype") != "Optimus Session"
			or current.get("parentfield") != "findings"
		):
			return False
	reason = None
	try:
		_check_run(run, locked=True)
	except StopRefresh as exc:
		reason = exc.reason
	if reason:
		return False
	return _signature(parent, current) == fingerprint


def _build_payload(row, file_cache, grounding, *, principal):
	"""The fix prompt's payload for one finding: the shared builder (``analyze``) with the same
	grounding as the synchronous Refresh. ``principal`` (the requesting user) is reserved for
	PR-4, which scopes the source and recording reads to it; nothing reads it yet, the worker's
	access having been checked for that user by ``_check_run``."""
	from optimus import analyze

	return analyze._ai_payload_for_finding(row, file_cache, **grounding)


def _grounding(doc, row, memo, run_id=None):
	"""The fix prompt's grounding for one finding, the same inputs as the synchronous Refresh
	(``analyze._run_ai_backfill``): the Phase 2 hottest-line index and the Slow Query index
	evidence. The slice's ``memo`` builds each once, not once per finding: a Phase 2
	index failure is logged once per slice, with the session, and the slice goes on without it."""
	from optimus import analyze

	actions = analyze.action_recording_map(doc.get("actions") or [])
	try:
		action = actions.get(int(row.get("action_ref"))) or {}
	except (TypeError, ValueError):
		action = {}
	uuid = action.get("recording_uuid")
	recordings = analyze.load_recordings_light(
		doc, [uuid] if uuid else [], memo=memo.setdefault("recordings", {})
	)
	if "phase2_index" not in memo:
		memo["phase2_index"] = analyze._phase2_index_for(doc)
	if "evidence" not in memo:
		scope, failed = analyze._run_ai_step(
			analyze._ai_evidence_scope,
			title="optimus ai refresh evidence",
			docname=doc.get("name"),
			run_id=run_id,
		)
		memo["evidence"] = (None, ()) if failed else scope
	evidence_lookup, tracked_apps = memo["evidence"]
	return {
		"phase2_index": memo["phase2_index"],
		"recordings_by_uuid": {rec["uuid"]: rec for rec in recordings if rec.get("uuid")},
		"actions_by_idx": actions,
		"evidence_lookup": evidence_lookup,
		"tracked_apps": tracked_apps,
	}


def _touch_session(name, **values):
	from frappe.utils import now_datetime

	frappe.db.set_value(
		"Optimus Session", name, {"modified": now_datetime(), **values}, update_modified=False
	)


def _fix_item(run, doc, row, memo):
	fingerprint = _signature(doc, row)
	payload = _build_payload(
		row,
		memo.setdefault("source", {}),
		_grounding(doc, row, memo, run["name"]),
		principal=run["requested_by"],
	)

	def valid():
		return _valid_item(run, doc, row, fingerprint)

	def send(timeout):
		return _suggest_fix(payload, timeout=timeout, run=run)

	def persist(result):
		if not valid():
			return False
		frappe.db.set_value(
			"Optimus Finding",
			row.name,
			{"llm_fix_json": json.dumps(result, allow_nan=False)},
			update_modified=False,
		)
		_touch_session(doc.name)
		return True

	return PreparedItem("fix", row.name, fingerprint, send, valid, persist)


def _suggest_fix(payload, *, timeout, run):
	"""The fix item's provider call, inside the engine's own usage boundary, as the Steps
	call is. ``suggest_fix`` counts each call it begins in this ``Usage``
	(``usage_out``), so a failure is a known zero only when the count proves nothing was
	sent; any other failure without reported usage stays unknown."""
	usage = ai_fix.Usage()
	return ai_fix._with_usage_on_failure(
		lambda: ai_fix.suggest_fix(
			payload,
			timeout=timeout,
			session_uuid=run["session_uuid"],
			docname=run["session_name"],
			usage_out=usage,
		),
		usage,
	)


def _steps_item(run, doc, memo):
	from optimus import analyze

	fingerprint = _signature(doc)
	actions = analyze._actions_for_humanizer(
		analyze.load_recordings_light(doc, memo=memo.setdefault("recordings", {}))
	)
	if not actions:
		return None

	def valid():
		return _valid_item(run, doc, None, fingerprint)

	def send(timeout):
		usage = ai_fix.Usage()

		def render():
			text = ai_fix.humanize_steps(
				actions,
				session_title=doc.get("title"),
				usage_out=usage,
				timeout=timeout,
				session_uuid=run["session_uuid"],
				docname=run["session_name"],
			)
			return analyze._assemble_humanized_notes(text)

		notes = ai_fix._with_usage_on_failure(render, usage)
		return {
			"notes": notes,
			"tokens": dict(usage),
			"usage_complete": usage.complete,
		}

	def persist(result):
		if not valid():
			return False
		# ``ai_steps_tokens`` is a 32-bit Int, as in #72: one reply's total cannot reach it, but
		# provider counts are bounded per field (``ai_fix._MAX_TOKEN_COUNT``), so their sum could.
		# This display figure is clamped so an absurd reply can never make the Steps save fail.
		# A total above the bound never reaches ``ai_tokens_spent`` either (``_token_count``
		# reads it as 0); only the run and the attempt keep it, and ``_drive`` logs one row.
		tokens = min(result["tokens"].get("total_tokens", 0), ai_fix._MAX_TOKEN_COUNT)
		_touch_session(doc.name, notes=result["notes"], ai_steps_tokens=tokens)
		return True

	return PreparedItem("steps", doc.name, fingerprint, send, valid, persist)


def _next_item(run, memo):
	"""The next item to send, or None when nothing is left; raises ``StopRefresh`` when the run
	must end. It publishes the run's progress before it returns.

	``memo`` is the slice's own scratch dict, one per ``_drive`` call, shared by every item of
	the slice: the source-file cache (``source``), the Phase 2 index and the Slow Query evidence
	(built once, ``_grounding``), and ``recordings``, the one entry that is #72's recordings-only
	memo (``analyze.load_recordings_light``), kept apart so it holds nothing else."""
	from optimus import analyze
	from optimus.settings import get_config

	if not ai_fix.is_available():
		# Switched off mid-run: nothing more can be sent, which is not a complete refresh.
		raise StopRefresh("disabled")
	doc = frappe.get_doc("Optimus Session", run["session_name"])
	tried, uncertain, fixes_spent = _attempts(run)
	names = {(a["kind"], a["target_name"]) for a in tried}
	rows = []
	if run.get("include_fixes") and ai_fix.is_available(section="findings"):
		rows = analyze.eligible_findings(
			doc.get("findings") or [],
			get_config(),
			regenerate_all=bool(run.get("regenerate_all")),
			requested_at=datetime.fromtimestamp(run["requested_at_epoch"], timezone.utc),
			include_outdated=run["scope"] == "all",
		)
	rows = [row for row in rows if ("fix", row.name) not in names]
	remaining = max(0, run["cap"] - fixes_spent) if run.get("cap") else len(rows)
	# Hash only the rows an uncertain attempt names: none, on the usual run.
	maybe = {target for kind, target, _ in uncertain if kind == "fix"}
	blocked = {
		row.name for row in rows if row.name in maybe and ("fix", row.name, _signature(doc, row)) in uncertain
	}
	step_state = run.get("steps_state") or "not_requested"
	steps = bool(run.get("include_steps") and step_state != "carried" and ("steps", doc.name) not in names)
	if steps and not ai_fix.is_available(section="humanize"):
		step_state, steps = "toggle_off", False
	if steps:
		step_state = "pending"
		notes = (doc.get("notes") or "").strip()
		if (
			run["scope"] != "all"
			and notes
			and not notes.startswith((analyze._AUTO_NOTES_PREAMBLE, analyze._HUMANIZED_NOTES_PREAMBLE))
		):
			step_state, steps = "kept", False
		elif uncertain and ("steps", doc.name, _signature(doc)) in uncertain:
			step_state, steps = "uncertain", False
	blocked_count = min(remaining, len(blocked)) + int(step_state == "uncertain")

	def progress():
		return _retry_sql(
			lambda: store.checkpoint(
				run["name"],
				docname=run["session_name"],
				worker_token=run["worker_token"],
				now=_now(),
				total_items=run["attempted"] + min(remaining, len(rows)) + int(steps),
				blocked_uncertain=blocked_count,
				steps_state=step_state,
			)
		)

	progress()
	if remaining:
		for row in rows:
			if row.name not in blocked:
				return _fix_item(run, doc, row, memo)
	if steps:
		item = _steps_item(run, doc, memo)
		if item is not None:
			return item
		step_state, steps = "no_input", False
		progress()
	return None


def _dispatch_pending(run_id, docname, *, nowait=False):
	"""Hand a committed slice intent to RQ (``_send_slice``) and log a failed enqueue.

	True once enqueued. False when nothing is due (claimed, ended or throttled) or the
	enqueue failed (logged; the intent stays pending). ``nowait`` (an HTTP request: the
	admission already waited for the session row, or an attach) takes that row with NOWAIT
	and never retries, so a click waits one lock timeout at most; a busy row
	answers None, logs no row and leaves the intent pending, for the session's next click
	(``_resend_pending``), a takeover or the sweep."""
	from optimus.analyze import _run_ai_step

	sent, failed = _run_ai_step(
		lambda: _send_slice(run_id, docname, nowait=nowait),
		title="optimus ai refresh enqueue",
		docname=docname,
		run_id=run_id,
	)
	if failed:
		_transaction(lambda: None)  # keep the logged row; the intent stays pending
		return False
	return sent


def _send_slice(run_id, docname, *, nowait=False):
	"""Enqueue one pending slice: True once sent, False when nothing is due (claimed, ended or
	throttled), None when a ``nowait`` lock was not granted. Any other failure is raised."""
	try:
		run = _retry_sql(
			lambda: store.prepare_dispatch(run_id, docname=docname, now=_now(), wait=not nowait),
			retry_lock_waits=not nowait,
		)
		if not run:
			return False
		enqueue = getattr(frappe.enqueue, "__wrapped__", frappe.enqueue)
		_transaction(
			lambda: enqueue(
				_JOB_METHOD,
				queue=ai_queue(),
				timeout=slice_seconds() + _call_timeout() + 300,
				is_async=True,
				enqueue_after_commit=True,
				deduplicate=True,
				job_id=f"optimus-ai-{run_id}-{run['slice_no']}",
				run_id=run_id,
				slice_no=run["slice_no"],
			)
		)
		return True
	except Exception as exc:
		if nowait and store.is_lock_unavailable(exc):
			return None
		raise


def _deliver(run_id, docname, *, nowait=False):
	"""``(sent, failure)`` for one pending slice (``_send_slice``): its ordinary failure is
	returned, not raised or logged, so the sweep can classify it and log it once with the
	rest; an RQ job timeout leaves as a fresh instance (``_isolated``)."""
	return _isolated(lambda: _send_slice(run_id, docname, nowait=nowait))


def ai_queue():
	value = frappe.conf.get("optimus_ai_queue", "long")
	if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", value):
		from frappe import _

		raise ValueError(
			_(
				"Invalid site configuration {0}: use a queue name of 1 to 40 lowercase letters, digits, "
				"underscores or hyphens that starts with a letter, for example "
				"bench --site <site> set-config {0} long"
			).format("optimus_ai_queue")
		)
	return value


def _workers_listening(docname=None):
	"""Unknown Redis state refuses new admission, without guessing it is empty."""
	from frappe.utils.background_jobs import get_queue, get_workers

	from optimus.analyze import _run_ai_step

	listening, failed = _run_ai_step(
		lambda: bool(get_workers(queue=get_queue(ai_queue()))),
		title="optimus ai refresh worker availability",
		docname=docname,
	)
	return None if failed else listening


def start_refresh(
	*,
	docname,
	session_uuid,
	requested_by,
	scope="all",
	regenerate_all=False,
	cap=20,
	include_fixes=True,
	include_steps=True,
	resume_from=None,
	retry_uncertain=False,
):
	"""Internal admission API. The HTTP caller also applies its user rate limit.

	This commits its own short transactions. Analyze may call it only after
	persisting the profiling result. A missing worker never selects inline work.

	A session that is not Ready, a requester without access or a Phase 2 run in progress is a
	refusal (``not_ready``, ``permission``, ``phase2``), answered before any progress is shown.
	"""
	if any(not isinstance(v, str) or not v or len(v) > 140 for v in (docname, session_uuid, requested_by)):
		raise ValueError("Invalid AI refresh identity")
	reason = None
	try:
		_check_run({"session_name": docname, "session_uuid": session_uuid, "requested_by": requested_by})
	except StopRefresh as exc:
		reason = exc.reason
	if reason:
		return {"status": "refused", "reason": reason}
	# Attaching only reads progress. Redis or a disabled provider must not hide
	# an existing run; admission below still serializes any competing new run. A run
	# whose lease or deadline has passed is no longer attached to: the admission takes
	# it over, so a crashed worker needs neither the scheduler nor a manual cancel.
	now = _now()
	current = frappe.db.get_value(store.RUN, {"active_session": docname}, "*", as_dict=True)
	if current and current.get("state") in store.ACTIVE and not store.expired(current, now):
		_resend_pending(current, docname, now)
		return {"status": "already_running", "refresh": public_state(current, now=now)}
	if not ai_fix.is_available():
		return {"status": "refused", "reason": "disabled"}
	# Every setting is read before admission: a run admitted with an unusable slice
	# length could never be dispatched and held its session until the queued lease expired.
	max_seconds = _setting_int("optimus_ai_refresh_max_seconds", 3600, 60, 86400)
	site_cap = _setting_int("optimus_ai_max_active_refreshes", 2, 1, 1000)
	slice_seconds()
	ai_queue()
	if _refresh_flag_held(session_uuid):
		# Q3: #72's synchronous Refresh holds this session; it refuses a click while this
		# engine's reservation is held, and the engine refuses while its flag is. Nothing is
		# counted or billed.
		from frappe import _

		return {
			"status": "refused",
			"reason": "refresh_running",
			"message": _(
				"A Refresh AI suggestions is already running for this session. "
				"Wait for it to finish, then try again."
			),
		}
	workers = _workers_listening(docname)
	if workers is not True:
		return {"status": "refused", "reason": "no_worker" if workers is False else "queue_unavailable"}
	gated, excluded = _selection_counts(docname, include_fixes, resume_from)
	run_id = uuid.uuid4().hex

	def admission():
		_bound_http_lock_wait()
		return store.admit(
			run_id=run_id,
			docname=docname,
			session_uuid=session_uuid,
			requested_by=requested_by,
			scope=scope,
			now=_now(),
			max_seconds=max_seconds,
			site_cap=site_cap,
			user_cap=2,
			cap=cap,
			include_fixes=include_fixes,
			include_steps=include_steps,
			regenerate_all=regenerate_all,
			resume_from=resume_from,
			retry_uncertain=retry_uncertain,
			gated=gated,
			excluded=excluded,
		)

	out = _retry_sql(admission, retry_lock_waits=False)
	# A takeover commits with every answer, a refusal included.
	_note_ending(out.pop("taken_over", None), docname)
	if out["status"] == "already_running" and not out.get("run"):
		# The unique reservation refused the insert, but that run was not visible to the
		# admission's snapshot (Postgres): read it again in a fresh transaction.
		current = _transaction(
			lambda: frappe.db.get_value(store.RUN, {"active_session": docname}, "*", as_dict=True)
		)
		if not current:
			return {"status": "refused", "reason": "busy"}
		out = {"status": "already_running", "run": current}
	if out["status"] == "queued":
		# The admission already waited for the session row: the dispatch takes it with
		# NOWAIT, so the click waits once. A busy row leaves the committed intent
		# pending for the next click (``_resend_pending``) instead of failing the response.
		_dispatch_pending(run_id, docname, nowait=True)
	if out.get("run"):
		return {"status": out["status"], "refresh": public_state(out["run"])}
	return out


def _refresh_flag_held(session_uuid):
	"""True while #72's synchronous Refresh holds this session's flag
	(``redis_keys.ai_refresh_inflight``), read fresh from Redis (``api._refresh_flight_held``).
	Redis not answering is not a hold: a cache failure never blocks a refresh (as there)."""
	from optimus import api

	return api._refresh_flight_held(session_uuid)


def live_run(docname):
	"""The session's live engine run (active, lease and deadline not passed), or None.

	Read in a fresh transaction, after the caller's read view is closed, so it answers from
	the current committed state, never from a view opened earlier in the request. For #72's
	synchronous Refresh (``api.refill_ai_suggestions``), which refuses while this run holds
	the session (Q3). A run whose lease or deadline passed is dead: the next admission takes it
	over, and it does not block the Refresh."""
	_transaction(lambda: None)
	run = _transaction(lambda: frappe.db.get_value(store.RUN, {"active_session": docname}, "*", as_dict=True))
	if run and run.get("state") in store.ACTIVE and not store.expired(run, _now()):
		return run
	return None


def _selection_counts(docname, include_fixes, resume_from=None):
	"""``(gated, excluded)``: the findings the selection leaves out, as #71's Refresh toasts
	report them (``AiSelection.gated`` and ``.excluded``), taken at admission and kept on the
	run for the page. Both are zero when no fix is asked for (a resume asks what the run it
	continues asked). Best effort: a failure is logged and the run goes on without the counts."""
	if resume_from:
		include_fixes = bool(frappe.db.get_value(store.RUN, resume_from, "include_fixes"))
	if not include_fixes or not ai_fix.is_available(section="findings"):
		return 0, 0

	def count():
		from optimus import analyze
		from optimus.settings import get_config

		doc = frappe.get_doc("Optimus Session", docname)
		chosen = analyze.eligible_findings(doc.get("findings") or [], get_config())
		return chosen.gated, chosen.excluded

	from optimus.analyze import _run_ai_step

	counts, failed = _run_ai_step(count, title="optimus ai refresh selection", docname=docname)
	return (0, 0) if failed else counts


def _resend_pending(run, docname, now):
	"""On attach, send again a committed slice intent that never reached a worker: a
	busy dispatch or a Redis failure left it pending, and the sweep is not scheduled.

	Best effort. The gate is the plain read: a queued run with a pending intent past the
	dispatch interval, so a polling click takes no lock. The send takes the session row with
	NOWAIT. A busy row, a failed enqueue (``_dispatch_pending`` logs it) or any other error
	leaves the intent pending and never changes the attach answer. The worker's claim stays
	the fence and the deterministic job id deduplicates a job RQ still holds."""
	if (
		run.get("state") != "queued"
		or not run.get("dispatch_pending")
		or (run.get("dispatched_at_epoch") or 0) > now - store.DISPATCH_INTERVAL
	):
		return
	_isolated(lambda: _dispatch_pending(run["name"], docname, nowait=True))


# End reasons an operator must see: one ``optimus ai refresh ended`` row each, as for every run
# that ends ``interrupted``. A run that completed, spent its time, stopped on its breaker or on
# uncertain items, lost its Ready session or was cancelled writes none.
_LOGGED_END_REASONS = frozenset({"no_progress", "history_limit", "disabled", "phase2", "permission"})


def _log_uncertain(docname, run_id, attempt, reason):
	"""One ``optimus ai refresh uncertain`` row: a call whose outcome is unknown, so it is never
	sent again and its usage may be missing from the run (check the provider's usage)."""
	ai_fix.log_ai_failure(
		"optimus ai refresh uncertain", docname=docname, run_id=run_id, attempt=attempt, reason=reason
	)


def _log_ending(run, docname):
	"""The Error Log rows of a run that a committed transaction ended or released, written
	after it: ``optimus ai refresh release`` when an inconsistent journal row was
	force-released, ``optimus ai refresh uncertain`` when its open Calling attempt became
	uncertain, and ``optimus ai refresh ended`` when it ended ``interrupted`` or for a reason
	in ``_LOGGED_END_REASONS``. True when a row was written, for the caller to commit."""
	if not run:
		return False
	written = False
	if run.get("inconsistent"):
		ai_fix.log_ai_failure(
			"optimus ai refresh release",
			store.JournalUnavailable(f"Refresh journal {run['inconsistent']} is inconsistent"),
			docname=docname,
			run_id=run["name"],
			outcome="released; its open call is uncertain and its usage unknown",
		)
		written = True
	if run.get("uncertain_attempt"):
		_log_uncertain(docname, run["name"], run["uncertain_attempt"], run.get("end_reason"))
		written = True
	if run.get("state") == "interrupted" or run.get("end_reason") in _LOGGED_END_REASONS:
		ai_fix.log_ai_failure(
			"optimus ai refresh ended",
			docname=docname,
			run_id=run["name"],
			state=run.get("state"),
			reason=run.get("end_reason"),
		)
		written = True
	return written


def _note_ending(run, docname):
	"""``_log_ending``, then keep its rows."""
	if _log_ending(run, docname):
		_transaction(lambda: None)


def recover_pending():
	"""Bounded recovery sweep: an accelerator. An expired run is also taken over by its
	session's next admission (``store.admit``), so recovery works with the scheduler paused.
	Never repeats an uncertain provider operation.

	A SQL lease expiry fences the old worker even if RQ still lists it. Late
	known usage may settle, but its answer cannot replace a newer generation.

	Three independent passes: expired leases, spent deadlines, then committed intents that
	never reached a worker. Each row is its own short transaction and takes its session row
	with NOWAIT, so a row a dead or slow transaction holds is skipped at once (``busy``; the
	next sweep or admission tries again) instead of stalling the sweep. A failing row
	or candidate read never stops the rest: each failure is logged once, after the
	passes, with its ``run_id``. Each run it ends gets its rows then too (``_log_ending``):
	``ended``, ``uncertain`` for its open call, ``release`` when it was inconsistent.

	Returns the counts: ``interrupted``, ``dispatched``, ``busy`` and ``failed``."""
	now = _now()
	counts = dict.fromkeys(("interrupted", "dispatched", "busy", "failed"), 0)
	failures, ended = [], []
	active = ["in", sorted(store.ACTIVE)]
	passes = (
		("lease_expired", {"state": active, "lease_until": ["<=", now]}, "lease_until asc"),
		("deadline", {"state": active, "deadline": ["<=", now]}, "deadline asc"),
		(
			None,
			{
				"state": "queued",
				"dispatch_pending": 1,
				"dispatched_at_epoch": ["<=", now - store.DISPATCH_INTERVAL],
			},
			"dispatched_at_epoch asc",
		),
	)
	for reason, filters, order_by in passes:
		rows, failure = _isolated(
			lambda: _transaction(
				lambda: frappe.get_all(
					store.RUN,
					filters=filters,
					fields=["name", "session_name"],
					order_by=order_by,
					limit_page_length=100,
				)
			)
		)
		if failure is not None:
			failures.append((None, None, failure))
		for row in rows or ():
			name, session = row["name"], row["session_name"]
			if reason is None:
				sent, failure = _deliver(name, session, nowait=True)
				outcome = "dispatched" if sent else "busy" if sent is None else None
			else:
				run, failure = _isolated(
					lambda: _retry_sql(
						lambda: store.interrupt(name, docname=session, now=now, reason=reason, wait=False),
						retry_lock_waits=False,
					)
				)
				if failure is not None and store.is_lock_unavailable(failure):
					failure = None
					outcome = "busy"
				else:
					outcome = "interrupted" if run else None
				if run:
					ended.append((run, session))
			if failure is not None:
				failures.append((name, session, failure))
			elif outcome:
				counts[outcome] += 1
	failure = None
	counts["failed"] = len(failures)
	written = False
	for run, session in ended:
		written = _log_ending(run, session) or written
	for run_id, session, exc in failures:
		ai_fix.log_ai_failure("optimus ai refresh recovery", exc, docname=session, run_id=run_id)
	if written or failures:
		failures = None
		_transaction(lambda: None)
	return counts


def _isolated(operation):
	"""``(result, failure)``: an ordinary failure is returned rather than raised, so one bad
	row cannot stop the sweep; an RQ job timeout still leaves as a fresh instance."""
	guard = safe_call.InterruptGuard(base=True)
	result = failure = None
	try:
		with guard:
			result = operation()
	except Exception as exc:
		failure = exc
	if guard.pending():
		result = failure = None
		raise guard.interrupt()
	return result, failure


def refresh_state(docname):
	"""Read only; the caller enforces session access before asking for progress."""
	run = frappe.db.get_value(
		store.RUN, {"session_name": docname}, "*", as_dict=True, order_by="creation desc"
	)
	return public_state(run) if run else None


def cancel_refresh(run_id, *, docname, requested_by):
	"""Cancel a run of the session ``docname``, which the caller authorizes. SQL cancellation
	works without Redis.

	``{"status": "cancelled", "refresh": ...}``, or a refusal: ``not_running`` when the run
	is not an active run of this session (another session's run changes nothing), and
	``busy`` when the session row stayed locked for one lock wait timeout. An HTTP request
	waits once; deadlocks and snapshot or serialization conflicts are still retried. A
	run with an inconsistent journal row is force-released, with one Error Log row."""

	def cancellation():
		_bound_http_lock_wait()
		return store.cancel(run_id, docname=docname, now=_now(), cancelled_by=requested_by)

	guard = safe_call.InterruptGuard(base=True)
	run = failure = None
	try:
		with guard:
			run = _retry_sql(cancellation, retry_lock_waits=False)
	except Exception as exc:
		failure = exc
	if guard.pending():
		run = failure = None
		raise guard.interrupt()
	if failure is not None:
		if not store.is_lock_unavailable(failure):
			raise failure
		failure = None
		return {"status": "refused", "reason": "busy"}
	if not run:
		return {"status": "refused", "reason": "not_running"}
	_note_ending(run, docname)
	return {"status": "cancelled", "refresh": public_state(run)}


def public_state(run, now=None):
	"""A counts-only view; SQL remains readable during a Redis outage.

	An active run whose lease or deadline has passed shows as it will be recorded:
	``interrupted``, with ``stalled`` True and the expiry as its ``end_reason``, before any
	recovery has written it. The session's next admission takes it over."""

	def count(key):
		value = run.get(key) or 0
		return value if type(value) is int and value >= 0 else 0

	now = _now() if now is None else now
	stalled = store.expired(run, now)
	state = "interrupted" if stalled else run.get("state")
	state = state if state in store.ACTIVE | store.TERMINAL else "unknown"
	return {
		"run_id": run["name"],
		"seq": count("seq"),
		"retry_no": count("retry_no"),
		"state": state,
		"scope": run.get("scope"),
		"count": count("total_items"),
		"done": count("completed"),
		"failed": count("failed"),
		"skipped": count("skipped"),
		"uncertain": count("uncertain"),
		"blocked_uncertain": count("blocked_uncertain"),
		"attempted": count("attempted"),
		"gated": count("gated_count"),
		"excluded": count("excluded_count"),
		"not_reached": max(0, count("total_items") - count("attempted")),
		"usage": {
			"tokens_reported": count("tokens_reported"),
			"incomplete_attempts": count("usage_incomplete"),
		},
		"end_reason": store.expiry_reason(run, now) if stalled else run.get("end_reason") or "",
		"stalled": stalled,
		"breaker_kind": run.get("breaker_kind") or "",
		"render_pending": bool(run.get("render_pending")),
		"steps_state": run.get("steps_state") or "",
		"requested_at": run.get("requested_at_epoch"),
		"updated_at": run.get("heartbeat"),
		"finished_at": run.get("ended_at_epoch"),
	}
