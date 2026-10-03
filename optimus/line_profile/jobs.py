"""Durable Phase 2 delivery and ownership.

SQL reserves each analysis generation before enqueue and fences every write.
Redis only delivers jobs. A crashed analysis requires a bounded explicit retry;
queue redelivery never repeats a claimed generation. No request runs analysis.
"""

import json
import time
import uuid
from contextvars import ContextVar

import frappe
from frappe import _

from optimus import ai_fix, ai_jobs
from optimus import ai_refresh_store as store

TABLE = "Optimus Phase Two Run"
QUEUE_SECONDS = 1800
JOB_SECONDS = 1500
LEASE_SECONDS = JOB_SECONDS + 120
MAX_ATTEMPTS = 4
JOURNAL_FIELDS = (
	"analyze_generation", "analyze_requested_by", "analyze_worker_token", "analyze_lease_until",
	"analyze_dispatch_at", "analyze_dispatch_pending", "analyze_attempts", "analyze_render_pending",
)
_RESULT_SAVE = ContextVar("optimus_phase2_result_save", default=False)


def _validate_journal_row(row, before):
	fields = JOURNAL_FIELDS
	if any(row.get(key) or before.get(key) for key in JOURNAL_FIELDS):
		fields += ("run_uuid", "parent", "parenttype", "parentfield")
		if not _RESULT_SAVE.get():
			fields += ("status", "results_json", "warnings_json", "total_ms", "ended_at")
	if any((row.get(key) or None) != (before.get(key) or None) for key in fields):
		raise frappe.ValidationError(_("Phase 2 analysis state is managed by the server."))


def validate_child_journal(doc):
	# REST can save a child before it saves the parent. Parent-only validation
	# would see the already-changed database row and miss the forged state.
	_validate_journal_row(doc, doc.get_doc_before_save() or {})


def protect_child_journal(doc):
	if any(doc.get(key) for key in JOURNAL_FIELDS):
		raise frappe.ValidationError(_("Phase 2 analysis state is managed by the server."))


def validate_parent_journal(doc):
	"""Read-only metadata is a UI hint; reject forged state on every parent save.

	Queue transitions use SQL under ownership locks. Ordinary edits (including
	System Manager edits) may neither change nor remove that internal state.
	"""
	old = doc.get_doc_before_save()
	previous = {row.get("name"): row for row in (old.phase_2_runs or [])} if old else {}
	for row in doc.phase_2_runs or []:
		before = previous.pop(row.get("name"), {})
		_validate_journal_row(row, before)
	if any(any(row.get(key) for key in JOURNAL_FIELDS) for row in previous.values()):
		raise frappe.ValidationError(_("Phase 2 analysis state is managed by the server."))


class MissingInput(ValueError):
	"""Captured Phase 2 data expired or is malformed; do not save empty results."""


class Phase2Failed(RuntimeError):
	"""A fixed, body-free worker error; details use the scrubbed log boundary."""


def _now():
	return time.time()


def _locked(docname, run_uuid):
	parent = store._read("Optimus Session", docname, lock=True)
	row = frappe.db.get_value(TABLE, {"parent": docname, "run_uuid": run_uuid}, "*", as_dict=True, for_update=True)
	return parent, row


def _write(row, **values):
	frappe.db.set_value(TABLE, row["name"], values, update_modified=False)
	row.update(values)
	# Reject stale Desk saves that loaded the parent before this transition.
	ai_jobs._touch_session(row["parent"])


def _authorized(parent, user):
	from optimus.permissions import may_act_on_session

	if not user or user == "Guest" or not frappe.db.get_value("User", user, "enabled"):
		return False
	if user != "Administrator" and not {"System Manager", "Optimus User"}.intersection(frappe.get_roles(user)):
		return False
	return may_act_on_session(
		user=user, owner=parent.get("owner") or "",
		can_read=bool(frappe.has_permission("Optimus Session", "read", parent["name"], user=user)),
		can_write=bool(frappe.has_permission("Optimus Session", "write", parent["name"], user=user)),
	)


def _admit(docname, session_uuid, run_uuid, requested_by, *, generation, now, from_recording=False):
	"""Caller commits this reservation before dispatch, under the AI mutex."""
	reason = store.lock_phase2_parent(docname)
	if reason:
		return {"status": "refused", "reason": reason}
	parent, row = _locked(docname, run_uuid)
	if not row or parent.get("session_uuid") != session_uuid:
		return {"status": "refused", "reason": "not_found"}
	if not _authorized(parent, requested_by):
		return {"status": "refused", "reason": "permission"}
	if row["status"] == "Analyzing" and row.get("analyze_generation") and (row.get("analyze_lease_until") or 0) > now:
		return {"status": "already_running", "run": row}
	if row["status"] not in ({"Recording"} if from_recording else {"Analyzing", "Failed"}):
		return {"status": "refused", "reason": "not_retryable"}
	attempts = row.get("analyze_attempts") or 0
	if type(attempts) is not int or not 0 <= attempts < MAX_ATTEMPTS:
		return {"status": "refused", "reason": "retry_limit"}
	_write(row, status="Analyzing", analyze_generation=generation, analyze_requested_by=requested_by,
		analyze_worker_token=None, analyze_attempts=attempts + 1, analyze_lease_until=now + QUEUE_SECONDS,
		analyze_dispatch_at=0, analyze_dispatch_pending=1)
	return {"status": "queued", "run": row}


def _claim(docname, run_uuid, generation, worker_token, *, now):
	parent, row = _locked(docname, run_uuid)
	if not (
		parent and parent["status"] == "Ready" and row and row["status"] == "Analyzing"
		and generation and row.get("analyze_generation") == generation
		and not row.get("analyze_worker_token") and (row.get("analyze_lease_until") or 0) > now
	):
		return None
	if not _authorized(parent, row.get("analyze_requested_by")):
		_write(row, status="Failed", analyze_dispatch_pending=0,
			warnings_json=json.dumps(["Phase 2 analysis refused: permission changed. Recheck session access."]))
		return None
	_write(row, analyze_worker_token=worker_token, analyze_dispatch_pending=0, analyze_lease_until=now + LEASE_SECONDS)
	return {**row, "session_uuid": parent["session_uuid"]}


def _owns(row, run, now):
	return bool(row and row["status"] == "Analyzing" and run.get("analyze_worker_token")
		and row.get("analyze_worker_token") == run["analyze_worker_token"]
		and row.get("analyze_generation") == run["analyze_generation"]
		and (row.get("analyze_lease_until") or 0) > now)


def _complete(run, *, now, persist):
	parent, row = _locked(run["parent"], run["run_uuid"])
	if not (
		_owns(row, run, now) and parent and parent["status"] == "Ready"
		and parent.get("session_uuid") == run["session_uuid"]
		and _authorized(parent, row.get("analyze_requested_by"))
	):
		return False
	# The callback must not commit: the findings and child outcome are one unit.
	token = _RESULT_SAVE.set(True)
	try:
		persist()
	finally:
		_RESULT_SAVE.reset(token)
	_write(row, status="Ready", analyze_dispatch_pending=0, analyze_render_pending=1)
	return True


def _fail(run, *, now, reason):
	_parent, row = _locked(run["parent"], run["run_uuid"])
	if not _owns(row, run, now):
		return False
	message = (
		"Phase 2 input is missing or invalid. Record a new line-profile pass."
		if reason == "input_missing" else
		"Phase 2 analysis failed or was interrupted. Retry analysis while its captured input is available."
	)
	_write(row, status="Failed", analyze_dispatch_pending=0, warnings_json=json.dumps([message]))
	return True


def _log(title, failure):
	ai_fix.log_ai_failure(title, failure)
	ai_jobs._transaction(lambda: None)


def _identity(*values):
	if any(not isinstance(value, str) or not value or len(value) > 140 for value in values):
		raise ValueError("Invalid Phase 2 identity")


def request(docname, session_uuid, run_uuid, requested_by, *, from_recording=False):
	"""Gated callers admit one durable generation; SQL survives queue failure."""
	_identity(docname, session_uuid, run_uuid, requested_by)
	ai_jobs._transaction(lambda: None)
	out = ai_jobs._retry_sql(lambda: _admit(docname, session_uuid, run_uuid, requested_by,
		generation=uuid.uuid4().hex, now=_now(), from_recording=from_recording))
	if out["status"] in {"queued", "already_running"}:
		_notify({**out["run"], "session_uuid": session_uuid}, "phase_2_run_analyzing")
		_dispatch(out["run"])
	return {
		"run_uuid": run_uuid, "session_uuid": session_uuid, "ran_inline": False,
		"status": "Analyzing" if out["status"] in {"queued", "already_running"} else "Refused",
		"reason": out.get("reason"),
	}


def _prepare_dispatch(run, *, now):
	parent, row = _locked(run["parent"], run["run_uuid"])
	if not (parent and row and row["status"] == "Analyzing" and row.get("analyze_dispatch_pending")
		and not row.get("analyze_worker_token") and row.get("analyze_generation")
		and row.get("analyze_generation") == run.get("analyze_generation")
		and (row.get("analyze_lease_until") or 0) > now
		and (not row.get("analyze_dispatch_at") or row["analyze_dispatch_at"] + 30 <= now)):
		return None
	_write(row, analyze_dispatch_at=now)
	return {**row, "session_uuid": parent["session_uuid"]}


def _enqueue(run):
	enqueue = getattr(frappe.enqueue, "__wrapped__", frappe.enqueue)
	ai_jobs._transaction(lambda: enqueue(
		"optimus.line_profile.analyzer.run_analyze", queue="long", timeout=JOB_SECONDS,
		is_async=True, enqueue_after_commit=True, deduplicate=True,
		job_id=f"optimus-phase2-{run['analyze_generation']}",
		session_uuid=run["session_uuid"], run_uuid=run["run_uuid"], generation=run["analyze_generation"],
	))


def _dispatch(run):
	guard = ai_fix._InterruptGuard(base=True)
	failure = None
	try:
		with guard:
			pending = ai_jobs._retry_sql(lambda: _prepare_dispatch(run, now=_now()))
			if pending:
				_enqueue(pending)
	except Exception as exc:
		failure = exc
	if guard.pending():
		failure = None
		raise guard.interrupt()
	if failure is not None:
		_log("optimus Phase 2 enqueue", failure)


def _legacy_generation(docname, session_uuid, run_uuid):
	"""Adopt only an old queued delivery, never a completed/claimed generation."""
	reason = store.lock_phase2_parent(docname)
	if reason:
		return None
	parent, row = _locked(docname, run_uuid)
	if not row or row["status"] != "Analyzing" or row.get("analyze_generation"):
		return None
	out = _admit(docname, session_uuid, run_uuid, parent.get("owner"), generation=uuid.uuid4().hex, now=_now())
	return out.get("run", {}).get("analyze_generation")


def run(session_uuid, run_uuid, *, generation=None):
	"""Worker entry. Analysis is pure work between a committed claim and save."""
	from optimus.line_profile import analyzer, capture

	_identity(session_uuid, run_uuid)
	if generation is not None:
		_identity(generation)
	guard = ai_fix._InterruptGuard(base=True)
	run_row = failure = None
	previous = getattr(frappe.local, "optimus_analyzing", False)
	frappe.local.optimus_analyzing = True
	try:
		with guard:
			docname = frappe.db.get_value("Optimus Session", {"session_uuid": session_uuid}, "name")
			ai_jobs._transaction(lambda: None)
			if not docname:
				return
			if generation is None:
				generation = ai_jobs._retry_sql(lambda: _legacy_generation(docname, session_uuid, run_uuid))
				if not generation:
					return
			run_row = ai_jobs._retry_sql(lambda: _claim(docname, run_uuid, generation, uuid.uuid4().hex, now=_now()))
			if not run_row:
				return
			if run_row["session_uuid"] != session_uuid:
				raise Phase2Failed("Phase 2 session changed")
			_notify(run_row, "phase_2_run_analyzing")
			results, result, total_ms = analyzer._compute_run(docname, run_uuid)
			ai_jobs._transaction(lambda: None)
			saved = ai_jobs._retry_sql(lambda: _complete(run_row, now=_now(), persist=lambda: analyzer._persist_run(
				docname, run_uuid, results, result, total_ms,
			)))
			if saved:
				# This is outside the result transaction. Failure cannot revert a
				# completed generation; even an ambiguous COMMIT is fenced by Ready.
				_postprocess(run_row)
				capture.cleanup_run(run_uuid)
	except Exception as exc:
		failure = exc
	finally:
		frappe.local.optimus_analyzing = previous
	if failure is not None or guard.pending():
		cleanup_error = None
		try:
			with guard:
				frappe.db.rollback()
				if run_row:
					changed = ai_jobs._retry_sql(lambda: _fail(run_row, now=_now(),
						reason="input_missing" if isinstance(failure, MissingInput) else "failed"))
					if changed:
						_notify(run_row, "phase_2_run_failed")
		except Exception as exc:
			cleanup_error = exc
		try:
			with guard:
				if failure is not None:
					_log("optimus Phase 2 worker", failure)
				if cleanup_error is not None:
					_log("optimus Phase 2 recovery", cleanup_error)
		except Exception:
			# SQL state (or its lease expiry) remains the visible recovery path.
			# A logging outage must never replace a pending hard interrupt.
			pass
		finally:
			failure = cleanup_error = run_row = result = results = None
		if guard.pending():
			raise guard.interrupt()
		raise Phase2Failed("Phase 2 analysis failed; saved profiling results are retained")


def _render(run):
	from optimus.report_refresh import render_report

	render_report(run["parent"])


def _postprocess(run):
	guard = ai_fix._InterruptGuard(base=True)
	failure = None
	try:
		with guard:
			_render(run)
	except Exception as exc:
		failure = exc
	if guard.pending():
		failure = None
		raise guard.interrupt()
	if failure is not None:
		_log("optimus Phase 2 report", failure)
	_notify(run, "phase_2_run_ready")


def _notify(run, event):
	from optimus.line_profile.analyzer import _publish

	_publish(event, {
		"session_uuid": run["session_uuid"], "run_uuid": run["run_uuid"],
		"parent": run["parent"], "user": run.get("analyze_requested_by"),
	})


def _recover(docname, run_uuid, *, now):
	_parent, row = _locked(docname, run_uuid)
	if not row or row["status"] != "Analyzing" or not row.get("analyze_generation"):
		return False
	if (row.get("analyze_lease_until") or 0) > now:
		return False
	_write(row, status="Failed", analyze_dispatch_pending=0, warnings_json=json.dumps([
		"Phase 2 analysis expired before completion. Retry analysis while its captured input is available."
	]))
	return True


def recover_pending():
	"""Bounded scheduler scan. Keep input for explicit retries, never rerun work."""
	from optimus.analyze import _run_ai_step

	_result, failed = _run_ai_step(_recover_pending, title="optimus Phase 2 queue recovery")
	if failed:
		raise Phase2Failed("Phase 2 queue recovery failed; committed work is retained")


def _recover_pending():
	now = _now()
	rows = frappe.get_all(TABLE, filters={"status": "Analyzing", "analyze_generation": ["is", "set"],
		"analyze_lease_until": ["<=", now]},
		fields=["parent", "run_uuid", "analyze_generation", "analyze_requested_by"], order_by="analyze_lease_until asc", limit_page_length=100)
	ai_jobs._transaction(lambda: None)
	for row in rows:
		if not row.get("analyze_generation"):
			continue  # The legacy janitor handles jobs queued before this upgrade.
		changed = ai_jobs._retry_sql(lambda: _recover(row["parent"], row["run_uuid"], now=_now()))
		if changed:
			row["session_uuid"] = frappe.db.get_value("Optimus Session", row["parent"], "session_uuid")
			_notify(row, "phase_2_run_failed")
	# Separate scans prevent many live workers from hiding undelivered jobs.
	rows = frappe.get_all(TABLE, filters={"status": "Analyzing", "analyze_dispatch_pending": 1,
		"analyze_lease_until": [">", now]}, fields=["parent", "run_uuid", "analyze_generation"],
		order_by="analyze_dispatch_at asc", limit_page_length=100)
	ai_jobs._transaction(lambda: None)
	for row in rows:
		_dispatch(row)


def expire_legacy(docname, run_uuid, *, status, cutoff):
	"""Recheck stale candidates under locks, without spoiling a newer retry."""
	_parent, row = _locked(docname, run_uuid)
	if (not row or row["status"] != status or row.get("analyze_generation")
		or not row.get("modified") or row["modified"] >= cutoff):
		return False
	if status not in {"Recording", "Analyzing"}:
		return False
	message = (
		"Phase 2 recording expired. Record a new line-profile pass and stop it after reproducing the flow."
		if status == "Recording" else
		"Phase 2 analysis timed out or crashed. Retry analysis while its captured input is available."
	)
	_write(row, status="Failed", warnings_json=json.dumps([message]))
	return True
