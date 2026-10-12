# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Queued AI enrichment. SQL owns progress; RQ delivery can be duplicated.

No public endpoint lives here. Callers must authorize admission and polling.
The worker rechecks authorization and inputs before sending and saving each
item. A Calling record commits before HTTP, without a journal or Session row
lock spanning the request. Result and outcome commit in one short SQL transaction
that also adds the usage to the session counter; the counter is log-and-continue
(#72, D2), so the journal's ``tokens_reported`` is the reconciliation source.
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
_SKIP_KINDS = frozenset({"excluded", "no_input", "not_eligible"})
_ERROR_KINDS = frozenset(
	{
		"auth",
		"not_found",
		"quota",
		"config",
		"rate_limited",
		"timeout",
		"transport",
		"server",
		"bad_request",
		"bad_response",
		"internal",
		"unknown",
		*_SKIP_KINDS,
	}
)


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
	value = frappe.conf.get(name, default)
	if type(value) is not int or not minimum <= value <= maximum:
		raise ValueError("Invalid AI refresh configuration")
	return value


def slice_seconds():
	return _setting_int("optimus_ai_slice_seconds", 120, 30, 1800)


def _call_timeout():
	return ai_fix._resolve_timeout_seconds()


def _transaction(operation):
	"""A short owned transaction, with fresh interrupts after rollback."""
	guard = ai_fix._InterruptGuard(base=True)
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
	row as its first statement (``store._locked``) instead of reading the run first."""
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
		guard = ai_fix._InterruptGuard(base=True)
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
	guard = ai_fix._InterruptGuard(base=True)
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
		cleanup_error = None
		try:
			with guard:
				frappe.db.rollback()
				if docname:
					_retry_sql(
						lambda: store.abandon(run_id, docname=docname, worker_token=worker_token, now=_now())
					)
		except Exception as exc:
			cleanup_error = exc
		failure_type = type(failure).__name__
		log_failure_type = ""
		try:
			with guard:
				if failure is not None:
					ai_fix.log_ai_failure("optimus AI refresh worker", failure)
				if cleanup_error is not None:
					ai_fix.log_ai_failure("optimus AI refresh recovery", cleanup_error)
				_transaction(lambda: None)  # preserve scrubbed logs before RQ rolls back
		except Exception as exc:
			log_failure_type = type(exc).__name__
		failure = cleanup_error = None
		if guard.pending():
			raise guard.interrupt()
		raise RefreshSliceFailed(
			f"AI refresh worker failed ({failure_type}); log persistence failure ({log_failure_type or 'none'})"
		)


def _drive(run_id, docname, slice_no, worker_token):
	started = time.monotonic()
	seconds, call_limit = slice_seconds(), _call_timeout()
	run = _retry_sql(
		lambda: store.claim(
			run_id,
			docname=docname,
			slice_no=slice_no,
			worker_token=worker_token,
			now=_now(),
			lease_seconds=seconds + call_limit + 180,
		)
	)
	if not run:
		return
	memo, items = {}, 0
	while True:
		run = _read_run(run_id)
		if not store._owns(run, worker_token, _now()):
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

		try:
			attempt = _retry_sql(
				lambda: _begin_attempt(run, item, run_id=run_id, docname=docname, worker_token=worker_token)
			)
		except StopRefresh as exc:
			reason = exc.reason
		if reason:
			_end(run_id, docname, worker_token, reason)
			return
		if not attempt:
			_yield(run_id, docname, worker_token)
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
		guard = ai_fix._InterruptGuard()
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
		outcome = "skipped" if kind in _SKIP_KINDS else "failed" if failure is not None else "succeeded"
		if guard.pending():
			outcome, kind = "uncertain", "timeout"
		if failure is not None and outcome == "failed":
			ai_fix.log_ai_failure("optimus AI refresh item", failure, session_uuid=run["session_uuid"])
		failure = None
		_transaction(lambda: None)  # close any provider/logging transaction

		def settle(*, final=outcome, persist=True, error_kind=kind):
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
		# saves its known cost and a failed outcome without trying that answer again.
		storage_guard = ai_fix._InterruptGuard(base=True)
		with storage_guard:
			save(settle, fallback=lambda: settle(final="failed", persist=False, error_kind="internal"))
		if storage_guard.pending():
			item = result = None
			raise storage_guard.interrupt()
		if guard.pending():
			item = result = failure = None
			raise guard.interrupt()
		items += 1


_SETTLE_TITLE = "optimus AI refresh result storage"
_COMMITTED_AFTER_ERROR = "committed: the error came after the COMMIT; the outcome and its tokens are saved"
_RECORDED = "recorded: the attempt's outcome and its tokens are saved (read back after the error)"
_UNKNOWN = "unknown: the outcome could not be read back after the error, so these tokens may well be recorded"


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
	- When the write did not commit and a ``fallback`` is given (the failed-outcome write:
	  the known usage without the answer), it runs next, with the same discipline.
	- Every row is written once both writes ended, with an ``outcome=``: the session
	  counter's own rows (it logs one, ``savepoint_rollback=failed``, when the whole
	  transaction is gone) and each write's failure (with ``tries`` and ``tokens``). When the
	  fallback recorded the tokens, the first write's rows say ``recorded by fallback``, so a
	  reconciler never adds them twice.
	- When neither write saved an outcome, the last error is raised. The usage counter is
	  log-and-continue (#72, D2): a statement-level counter failure is logged by the counter
	  and the write still commits, so the journal's ``tokens_reported`` is the source for
	  reconciling the session's ``ai_tokens_spent``.

	An RQ job timeout leaves as a fresh instance. Returns True once an outcome is saved."""
	reference = {"session_uuid": session_uuid, "docname": docname, "run_id": run_id, "attempt": attempt_id}
	_result, error, logged, tries, settled = _write_outcome(operation, attempt_id)
	if error is None or settled is True:
		rows = _rows(logged, error, f"committed on try {tries}", error_outcome=_COMMITTED_AFTER_ERROR)
		raised = None
	elif fallback is None:
		rows = _rows(logged, error, _failed(settled, tries), tries=tries, tokens=tokens)
		raised = error
	else:
		fb_result, fb_error, fb_logged, fb_tries, fb_settled = _write_outcome(fallback, attempt_id)
		if fb_error is None and fb_result:
			# The failed-outcome write recorded the tokens: the answer's write did not commit.
			first = f"not committed after try {tries}; recorded by fallback"
			rows = _rows(logged, error, first, tries=tries, tokens=tokens)
			rows += _rows(fb_logged, None, f"committed on try {fb_tries}")
			raised = None
		else:
			# A no-op fallback (the attempt was already settled) or a failed one: the state
			# read back now tells whether the outcome and its tokens are saved.
			final = _attempt_settled(attempt_id) if fb_error is None else fb_settled
			if final is True:
				rows = _rows(logged, error, _RECORDED, tokens=tokens) + _rows(fb_logged, fb_error, _RECORDED)
				raised = None
			else:
				rows = _rows(logged, error, _failed(final, tries), tries=tries, tokens=tokens)
				rows += _rows(fb_logged, fb_error, _failed(final, fb_tries), tries=fb_tries, tokens=tokens)
				raised = fb_error if fb_error is not None else error
		fb_error = fb_logged = None
	error = logged = None
	for exc, context in rows:
		ai_fix.log_ai_failure(_SETTLE_TITLE, exc, **reference, **context)
	rows = None
	if raised is not None:
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
		guard = ai_fix._InterruptGuard(base=True)
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


def _begin_attempt(run, item, *, run_id, docname, worker_token):
	"""The send-intent transaction: write the Calling attempt, or None to yield.

	Every lock comes before the first plain read: the session and run (``store._locked``),
	then the item's rows and the Phase 2 rows (``item.valid()``, which rechecks the input
	and runs ``_check_run``), and only then the requester's plain User read. A read view
	opened earlier would turn the Finding lock into a 1020 on MariaDB 11.8 when a Finding
	writer that skips the session lock (#72's write of a zero-token answer) committed in
	between. When the input is no longer valid, ``_check_run`` raises ``StopRefresh`` if the
	run itself must end; otherwise the slice yields."""
	store._locked(run_id, docname)
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
	)


def _end(run_id, docname, worker_token, reason):
	return _retry_sql(
		lambda: store.finish(run_id, docname=docname, worker_token=worker_token, now=_now(), reason=reason)
	)


def _yield(run_id, docname, worker_token):
	run = _retry_sql(
		lambda: store.continue_run(run_id, docname=docname, worker_token=worker_token, now=_now())
	)
	if run and run["state"] == "queued":
		_dispatch_pending(run_id, docname)


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
	if not may_act_on_session(
		user=user,
		owner=parent.get("owner") or "",
		can_read=bool(frappe.has_permission("Optimus Session", "read", parent["name"], user=user)),
		can_write=bool(frappe.has_permission("Optimus Session", "write", parent["name"], user=user)),
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


def _attempts(run):
	fields = ["kind", "target_name", "input_hash", "state"]
	tried = frappe.get_all(
		store.ATTEMPT, filters={"run_id": run["name"]}, fields=fields, limit_page_length=10001
	)
	uncertain = []
	if not run.get("retry_uncertain"):
		uncertain = frappe.get_all(
			store.ATTEMPT,
			filters={"session_name": run["session_name"], "state": "uncertain"},
			fields=fields,
			limit_page_length=10001,
		)
	if len(tried) > 10000 or len(uncertain) > 10000:
		raise StopRefresh("history_limit")
	return tried, {(a["kind"], a["target_name"], a["input_hash"]) for a in uncertain}


def _valid_item(run, doc, row, fingerprint):
	# The caller already locks Session before Run/Attempt. Current reads here
	# must not consult a pre-wait MariaDB snapshot when another actor changed it.
	# The Finding row is locked before _check_run's plain reads open a read view; the
	# caller (_begin_attempt, settle_attempt) has locked the session and run first.
	parent = frappe.db.get_value("Optimus Session", run["session_name"], "*", as_dict=True, for_update=True)
	if not parent or parent.get("modified") != doc.get("modified"):
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
	"""One source/recording boundary, scoped to the requesting principal."""
	from optimus import analyze

	return analyze._ai_payload_for_finding(row, file_cache, **grounding)


def _grounding(doc, row, memo):
	from optimus import analyze

	actions = analyze.action_recording_map(doc.get("actions") or [])
	try:
		action = actions.get(int(row.get("action_ref"))) or {}
	except (TypeError, ValueError):
		action = {}
	uuid = action.get("recording_uuid")
	recordings = analyze.load_recordings_light(doc, [uuid] if uuid else [], memo=memo)
	return {
		"phase2_index": analyze._phase2_index_for(doc),
		"recordings_by_uuid": {rec["uuid"]: rec for rec in recordings if rec.get("uuid")},
		"actions_by_idx": actions,
	}


def _touch_session(name, **values):
	from frappe.utils import now_datetime

	frappe.db.set_value(
		"Optimus Session", name, {"modified": now_datetime(), **values}, update_modified=False
	)


def _fix_item(run, doc, row, memo):
	fingerprint = _signature(doc, row)
	payload = _build_payload(
		row, memo.setdefault("source", {}), _grounding(doc, row, memo), principal=run["requested_by"]
	)

	def valid():
		return _valid_item(run, doc, row, fingerprint)

	def send(timeout):
		return ai_fix.suggest_fix(
			payload, timeout=timeout, session_uuid=run["session_uuid"], docname=run["session_name"]
		)

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


def _steps_item(run, doc, memo):
	from optimus import analyze

	fingerprint = _signature(doc)
	actions = analyze._actions_for_humanizer(analyze.load_recordings_light(doc, memo=memo))
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
		_touch_session(
			doc.name, notes=result["notes"], ai_steps_tokens=result["tokens"].get("total_tokens", 0)
		)
		return True

	return PreparedItem("steps", doc.name, fingerprint, send, valid, persist)


def _next_item(run, memo):
	from optimus import analyze
	from optimus.settings import get_config

	doc = frappe.get_doc("Optimus Session", run["session_name"])
	tried, uncertain = _attempts(run)
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
	remaining = max(0, run["cap"] - sum(a["kind"] == "fix" for a in tried)) if run.get("cap") else len(rows)
	blocked = {row.name for row in rows if ("fix", row.name, _signature(doc, row)) in uncertain}
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
		elif ("steps", doc.name, _signature(doc)) in uncertain:
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


def _dispatch_pending(run_id, docname):
	guard = ai_fix._InterruptGuard(base=True)
	failure = None
	try:
		with guard:
			run = _retry_sql(lambda: store.prepare_dispatch(run_id, docname=docname, now=_now()))
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
	except Exception as exc:
		failure = exc
	if guard.pending():
		failure = None
		raise guard.interrupt()
	if failure is not None:
		ai_fix.log_ai_failure("optimus AI refresh enqueue", failure)
		failure = None
		_transaction(lambda: None)
		return False
	return True


def ai_queue():
	value = frappe.conf.get("optimus_ai_queue", "long")
	if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", value):
		raise ValueError("Invalid AI refresh queue configuration")
	return value


def _workers_listening():
	"""Unknown Redis state refuses new admission, without guessing it is empty."""
	from frappe.utils.background_jobs import get_queue, get_workers

	guard = ai_fix._InterruptGuard(base=True)
	failure = None
	try:
		with guard:
			return bool(get_workers(queue=get_queue(ai_queue())))
	except Exception as exc:
		failure = exc
	if guard.pending():
		raise guard.interrupt()
	ai_fix.log_ai_failure("optimus AI worker availability", failure)
	return None


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
	"""
	if any(not isinstance(v, str) or not v or len(v) > 140 for v in (docname, session_uuid, requested_by)):
		raise ValueError("Invalid AI refresh identity")
	_check_run({"session_name": docname, "session_uuid": session_uuid, "requested_by": requested_by})
	# Attaching only reads progress. Redis or a disabled provider must not hide
	# an existing run; admission below still serializes any competing new run.
	current = frappe.db.get_value(store.RUN, {"active_session": docname}, "*", as_dict=True)
	if current and current.get("state") in store.ACTIVE:
		return {"status": "already_running", "refresh": public_state(current)}
	if not ai_fix.is_available():
		return {"status": "refused", "reason": "disabled"}
	workers = _workers_listening()
	if workers is not True:
		return {"status": "refused", "reason": "no_worker" if workers is False else "queue_unavailable"}
	run_id = uuid.uuid4().hex
	out = _retry_sql(
		lambda: store.admit(
			run_id=run_id,
			docname=docname,
			session_uuid=session_uuid,
			requested_by=requested_by,
			scope=scope,
			now=_now(),
			max_seconds=_setting_int("optimus_ai_refresh_max_seconds", 3600, 60, 86400),
			site_cap=_setting_int("optimus_ai_max_active_refreshes", 2, 1, 1000),
			user_cap=2,
			cap=cap,
			include_fixes=include_fixes,
			include_steps=include_steps,
			regenerate_all=regenerate_all,
			resume_from=resume_from,
			retry_uncertain=retry_uncertain,
		),
		retry_lock_waits=False,
	)
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
		_dispatch_pending(run_id, docname)
	if out.get("run"):
		return {"status": out["status"], "refresh": public_state(out["run"])}
	return out


def recover_pending():
	"""Bounded scheduler sweep. Never repeat an uncertain provider operation.

	A SQL lease expiry fences the old worker even if RQ still lists it. Late
	known usage may settle, but its answer cannot replace a newer generation.
	"""
	now = _now()
	expired = frappe.get_all(
		store.RUN,
		filters={"state": ["in", sorted(store.ACTIVE)], "lease_until": ["<=", now]},
		fields=["name", "session_name"],
		order_by="lease_until asc",
		limit_page_length=100,
	)
	for row in expired:
		_retry_sql(
			lambda: store.interrupt(row["name"], docname=row["session_name"], now=now, reason="lease_expired")
		)
	budget = frappe.get_all(
		store.RUN,
		filters={"state": ["in", sorted(store.ACTIVE)], "deadline": ["<=", now]},
		fields=["name", "session_name"],
		order_by="deadline asc",
		limit_page_length=100,
	)
	for row in budget:
		_retry_sql(
			lambda: store.interrupt(row["name"], docname=row["session_name"], now=now, reason="deadline")
		)
	pending = frappe.get_all(
		store.RUN,
		filters={"state": "queued", "dispatch_pending": 1, "dispatched_at_epoch": ["<=", now - 30]},
		fields=["name", "session_name"],
		order_by="dispatched_at_epoch asc",
		limit_page_length=100,
	)
	for row in pending:
		_dispatch_pending(row["name"], row["session_name"])


def refresh_state(docname):
	"""Read only; the caller enforces session access before asking for progress."""
	run = frappe.db.get_value(
		store.RUN, {"session_name": docname}, "*", as_dict=True, order_by="creation desc"
	)
	return public_state(run) if run else None


def cancel_refresh(run_id, *, docname, requested_by):
	"""Caller authorizes the session ``docname``; a run of another session is not cancelled.
	SQL cancellation works without Redis."""
	run = _retry_sql(lambda: store.cancel(run_id, docname=docname, now=_now(), cancelled_by=requested_by))
	return public_state(run) if run else None


def public_state(run):
	"""A counts-only view; SQL remains readable during a Redis outage."""

	def count(key):
		value = run.get(key) or 0
		return value if type(value) is int and value >= 0 else 0

	state = run.get("state")
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
		"not_reached": max(0, count("total_items") - count("attempted")),
		"usage": {
			"tokens_reported": count("tokens_reported"),
			"incomplete_attempts": count("usage_incomplete"),
		},
		"end_reason": run.get("end_reason") or "",
		"breaker_kind": run.get("breaker_kind") or "",
		"render_pending": bool(run.get("render_pending")),
		"steps_state": run.get("steps_state") or "",
		"requested_at": run.get("requested_at_epoch"),
		"updated_at": run.get("heartbeat"),
		"finished_at": run.get("ended_at_epoch"),
	}
