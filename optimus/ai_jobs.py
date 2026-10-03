# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Queued AI enrichment. SQL owns progress; RQ delivery can be duplicated.

No public endpoint lives here. Callers must authorize admission and polling.
The worker rechecks authorization and inputs before sending and saving each
item. A Calling record commits before HTTP, without a journal or Session row
lock spanning the request. Result, usage and outcome share one SQL transaction.
"""

import hashlib
import html
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import frappe
from frappe import _

from optimus import ai_fix, safe_commit
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


def _retry_sql(operation):
	"""Retry definite rollback conflicts only, at most three short transactions.

	Admission uses NOWAIT on its shared mutex. PostgreSQL can also reject an
	older snapshot after that mutex changed. Neither case warrants retrying a
	provider call or changing the site's transaction isolation level.
	"""
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
		retryable = getattr(failure, "pgcode", None) in {"40001", "40P01", "55P03"} or (
			failure.args and type(failure.args[0]) is int and failure.args[0] in {1205, 1213}
		)
		if not retryable or attempt == 2:
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
	failure = None
	previous = getattr(frappe.local, "optimus_analyzing", False)
	frappe.local.optimus_analyzing = True
	try:
		with guard:
			_drive(run_id, slice_no, worker_token)
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
				_retry_sql(lambda: store.abandon(run_id, worker_token=worker_token, now=_now()))
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


def _drive(run_id, slice_no, worker_token):
	started = time.monotonic()
	seconds, call_limit = slice_seconds(), _call_timeout()
	run = _retry_sql(
		lambda: store.claim(
			run_id,
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
			_end(run_id, worker_token, "breaker")
			return
		remaining = int(run["deadline"] - _now() - 5)
		if remaining < min(15, call_limit):
			_end(run_id, worker_token, "deadline")
			return
		if items and time.monotonic() - started >= seconds:
			_yield(run_id, worker_token)
			return
		reason, item = None, None
		try:
			_check_run(run)
			item = _next_item(run, memo)
		except StopRefresh as exc:
			reason = exc.reason
		if reason:
			_end(run_id, worker_token, reason)
			return
		if item is None:
			latest = _read_run(run_id)
			_end(
				run_id,
				worker_token,
				"uncertain" if latest and latest.get("blocked_uncertain") else "complete",
			)
			return
		remaining = int(run["deadline"] - _now() - 5)
		if remaining < min(15, call_limit):
			_end(run_id, worker_token, "deadline")
			return
		# Finish the input/config read view, then recheck in the locked intent
		# transaction. MariaDB may otherwise keep a pre-call snapshot alive.
		_transaction(lambda: None)

		def begin():
			store._locked(run_id)
			_check_run(run, locked=True)
			if not item.valid():
				return None
			return store.begin_attempt(
				run_id,
				worker_token=worker_token,
				kind=item.kind,
				target=item.target,
				input_hash=item.input_hash,
				now=_now(),
			)

		try:
			attempt = _retry_sql(begin)
		except StopRefresh as exc:
			reason = exc.reason
		if reason:
			_end(run_id, worker_token, reason)
			return
		if not attempt:
			_yield(run_id, worker_token)
			return
		remaining = int(run["deadline"] - _now() - 5)
		if remaining < min(15, call_limit):
			_settle(
				lambda: store.settle_attempt(
					run_id,
					attempt["name"],
					worker_token=worker_token,
					outcome="skipped",
					tokens=0,
					usage_complete=True,
					now=_now(),
					error_kind="deadline",
				)
			)
			_end(run_id, worker_token, "deadline")
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
				worker_token=worker_token,
				outcome=final,
				tokens=tokens,
				usage_complete=complete is True,
				now=_now(),
				error_kind=error_kind,
				persist=(lambda: item.persist(result)) if persist else None,
			)

		storage_error = None
		storage_guard = ai_fix._InterruptGuard(base=True)
		try:
			with storage_guard:
				_settle(settle)
		except Exception as exc:
			storage_error = exc
		if storage_guard.pending():
			item = result = storage_error = None
			raise storage_guard.interrupt()
		if storage_error is not None:
			# A successful provider reply can fail local validation/storage. Save
			# its known cost and a failed outcome without trying that answer again.
			ai_fix.log_ai_failure(
				"optimus AI refresh result storage", storage_error, session_uuid=run["session_uuid"]
			)
			storage_error = None
			_transaction(lambda: None)
			_settle(lambda: settle(final="failed", persist=False, error_kind="internal"))
		if guard.pending():
			item = result = failure = None
			raise guard.interrupt()
		items += 1


def _settle(operation):
	"""Retry only the SQL outcome, once, including an ambiguous COMMIT reply.

	The attempt's settled bit makes both rolled-back and already-committed
	cases safe. This function never invokes the provider again.
	"""
	failure = None
	for _attempt in range(2):
		guard = ai_fix._InterruptGuard(base=True)
		succeeded = False
		try:
			with guard:
				_transaction(operation)
				succeeded = True
		except Exception as exc:
			failure = exc
		if guard.pending():
			operation = failure = None
			raise guard.interrupt()
		if succeeded:
			if failure is not None:
				ai_fix.log_ai_failure("optimus AI refresh accounting recovered", failure)
				failure = None
				_transaction(lambda: None)
			return
		raise_after_retry = failure
	raise raise_after_retry


def _end(run_id, worker_token, reason):
	# Completed answers are already committed. Rendering must neither hold a
	# provider transaction nor turn a failed attachment into a repeated call.
	if reason in {"complete", "uncertain", "breaker"}:
		failure = None
		guard = ai_fix._InterruptGuard(base=True)
		try:
			with guard:
				_render_pending(run_id, worker_token)
		except StopRefresh as exc:
			reason = exc.reason
		except Exception as exc:
			failure = exc
		if guard.pending():
			failure = None
			raise guard.interrupt()
		if failure is not None:
			ai_fix.log_ai_failure("optimus AI refresh report", failure)
			failure = None
			_transaction(lambda: None)
			reason = "render_failed"
	return _retry_sql(lambda: store.finish(run_id, worker_token=worker_token, now=_now(), reason=reason))


def _render_pending(run_id, worker_token):
	from optimus.report_refresh import render_report

	run = _read_run(run_id)
	if not run or not store._owns(run, worker_token, _now()):
		return
	if run["deadline"] <= _now():
		raise StopRefresh("deadline")
	_check_run(run)
	# A previous cancelled/failed run may have saved answers without a report.
	# This also repairs that report when the new selection makes no AI calls.
	if frappe.db.get_value(store.RUN, {"session_name": run["session_name"], "render_pending": 1}, "name"):
		render_report(run["session_name"], run_id=run_id, worker_token=worker_token)


def _yield(run_id, worker_token):
	run = _retry_sql(lambda: store.continue_run(run_id, worker_token=worker_token, now=_now()))
	if run and run["state"] == "queued":
		_dispatch_pending(run_id)


def _check_run(run, *, locked=False):
	from optimus.permissions import may_act_on_session

	parent = frappe.db.get_value("Optimus Session", run["session_name"], "*", as_dict=True, for_update=locked)
	if not parent or parent.get("status") != "Ready" or parent.get("session_uuid") != run["session_uuid"]:
		raise StopRefresh("not_ready")
	user = run.get("requested_by")
	if not user or user == "Guest" or not frappe.db.get_value("User", user, "enabled", for_update=locked):
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
	if frappe.db.get_values(
		"Optimus Phase Two Run",
		{"parent": parent["name"], "status": ["in", ["Recording", "Analyzing"]]},
		["name"],
		limit=1,
		for_update=locked,
	):
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
	parent = frappe.db.get_value("Optimus Session", run["session_name"], "*", as_dict=True, for_update=True)
	if not parent or parent.get("modified") != doc.get("modified"):
		return False
	reason = None
	try:
		_check_run(run, locked=True)
	except StopRefresh as exc:
		reason = exc.reason
	if reason:
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


def _dispatch_pending(run_id):
	guard = ai_fix._InterruptGuard(base=True)
	failure = None
	try:
		with guard:
			run = _retry_sql(lambda: store.prepare_dispatch(run_id, now=_now()))
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
	try:
		_check_run({"session_name": docname, "session_uuid": session_uuid, "requested_by": requested_by})
	except StopRefresh as exc:
		return {"status": "refused", "reason": exc.reason}
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
		)
	)
	if out["status"] == "queued":
		_dispatch_pending(run_id)
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
		fields=["name"],
		order_by="lease_until asc",
		limit_page_length=100,
	)
	for row in expired:
		_retry_sql(lambda: store.interrupt(row["name"], now=now, reason="lease_expired"))
	budget = frappe.get_all(
		store.RUN,
		filters={"state": ["in", sorted(store.ACTIVE)], "deadline": ["<=", now]},
		fields=["name"],
		order_by="deadline asc",
		limit_page_length=100,
	)
	for row in budget:
		_retry_sql(lambda: store.interrupt(row["name"], now=now, reason="deadline"))
	pending = frappe.get_all(
		store.RUN,
		filters={"state": "queued", "dispatch_pending": 1, "dispatched_at_epoch": ["<=", now - 30]},
		fields=["name"],
		order_by="dispatched_at_epoch asc",
		limit_page_length=100,
	)
	for row in pending:
		_dispatch_pending(row["name"])


def refresh_state(docname):
	"""Read only; the caller enforces session access before asking for progress."""
	run = frappe.db.get_value(
		store.RUN, {"session_name": docname}, "*", as_dict=True, order_by="creation desc"
	)
	return public_state(run) if run else None


def active_refresh(docname):
	run = frappe.db.get_value(store.RUN, {"active_session": docname}, "*", as_dict=True)
	return public_state(run) if run else None


def refresh_plan(doc):
	"""Count the same eligible rows the worker selects, without source or file I/O."""
	from optimus import analyze
	from optimus.settings import get_config

	cfg = get_config()
	fixes = bool(cfg.ai_enabled and cfg.ai_suggest_findings)
	cutoff = datetime.fromtimestamp(_now(), timezone.utc)
	rows = doc.get("findings") or []
	pending = len(analyze.eligible_findings(rows, cfg, requested_at=cutoff)) if fixes else 0
	total = len(analyze.eligible_findings(rows, cfg, regenerate_all=True, requested_at=cutoff)) if fixes else 0
	cap = cfg.ai_refresh_max_findings
	return {
		"pending": pending, "total": total, "cap": cap,
		"selected": min(pending, cap) if cap else pending,
		"selected_all": min(total, cap) if cap else total,
		"steps": bool(cfg.ai_enabled and cfg.ai_humanize_steps and doc.get("actions")),
	}


def admission_message(reason):
	"""Fixed translated explanations, never provider text or exception detail."""
	if reason == "no_worker":
		return _("No background worker is listening on the AI queue {0}.").format(ai_queue())
	return {
		"queue_unavailable": _("The background queue is unavailable. Try again after it recovers."),
		"phase2": _("Wait for the active Phase 2 run to finish."),
		"disabled": _("AI suggestions are disabled or the provider is not configured."),
		"not_ready": _("The session must be Ready before AI suggestions can run."),
		"permission": _("The requesting user can no longer update this session."),
		"site_cap": _("The site already has the maximum number of active AI refreshes."),
		"user_cap": _("You already have the maximum number of active AI refreshes."),
		"retry_limit": _("This refresh has reached its resume limit. Start a new refresh to try again."),
		"already_resumed": _("This run has already been resumed. Reload its current progress."),
		"not_resumable": _("This run cannot be resumed."),
	}.get(reason, _("AI enrichment could not start. See the Error Log for details."))


def record_admission_notice(docname, *, reason):
	"""Record an automatic refusal on the already committed session timeline."""
	message = _("AI enrichment was not started: {0}").format(admission_message(reason))
	_transaction(lambda: frappe.get_doc({
		"doctype": "Comment", "comment_type": "Info",
		"reference_doctype": "Optimus Session", "reference_name": docname,
		"content": html.escape(message),
	}).insert(ignore_permissions=True))


def cancel_refresh(run_id, *, requested_by):
	"""Caller authorizes the run's parent. SQL cancellation works without Redis."""
	run = _retry_sql(lambda: store.cancel(run_id, now=_now(), cancelled_by=requested_by))
	return public_state(run) if run else None


def prepare_analyze_retry(docname, session_uuid, *, requested_by):
	"""Called after the action gate; a stale refresh cannot write into a retry."""
	_transaction(lambda: None)
	return _retry_sql(lambda: store.prepare_analyze_retry(
		docname, session_uuid, requested_by=requested_by, now=_now(),
	))


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
