# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""SQL journal for optional AI refresh work.

These operations participate in the caller's transaction. They never commit,
contact a provider, enqueue a job or use Redis as the source of truth. An
existing run locks its Session, Run and Attempt in that order. Admission also
serializes capacity with the Control row. A caller commits a calling attempt
before sending HTTP, then commits its result and reported usage together.
"""

import hashlib

import frappe

RUN = "Optimus AI Refresh Run"
ATTEMPT = "Optimus AI Refresh Attempt"
CONTROL = "Optimus AI Refresh Control"
ACTIVE = frozenset({"queued", "running"})
TERMINAL = frozenset({"complete", "stopped", "cancelled", "interrupted"})


class JournalUnavailable(RuntimeError):
	"""Fail closed when a deployment lacks the authoritative journal/mutex."""


def _timestamp(value):
	# The selection cursor must also be representable by datetime in the worker.
	return type(value) in (int, float) and 0 <= value < 253402214400


def _read(table, name, *, lock=False):
	return frappe.db.get_value(table, name, "*", as_dict=True, for_update=lock)


def _update(table, row, **values):
	if table == RUN:
		values["seq"] = (row.get("seq") or 0) + 1
	frappe.db.set_value(table, row["name"], values, update_modified=False)
	row.update(values)


def _insert(table, name, **values):
	row = {"doctype": table, "name": name, **values}
	frappe.get_doc(row).insert(ignore_permissions=True)
	return row


def _locked(run_id):
	# The parent reference is immutable. Read it first so every path uses the
	# same lock order, including cancellation and late usage reconciliation.
	docname = frappe.db.get_value(RUN, run_id, "session_name")
	if not docname:
		return None, None
	parent = _read("Optimus Session", docname, lock=True)
	run = _read(RUN, run_id, lock=True)
	if run and run["session_name"] != docname:
		raise RuntimeError("Refresh journal parent changed")
	if run and run.get("active_session") not in (None, "", docname):
		raise JournalUnavailable("Refresh journal reservation is inconsistent")
	return parent, run


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
):
	"""Reserve a session and enforce caps under a single short SQL mutex.

	Caller checks permissions, provider configuration, Phase 2 and workers
	before admission. Analyze-time enrichment reserves only its own session;
	user refreshes also take site/user capacity. No queue call happens here.
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
	control = frappe.db.get_value(CONTROL, "site", "*", as_dict=True, for_update=True, wait=False)
	if not control:
		raise JournalUnavailable("AI refresh storage is not initialized")
	parent = _read("Optimus Session", docname, lock=True)
	if not parent or parent.get("session_uuid") != session_uuid or parent["status"] != "Ready":
		return {"status": "refused", "reason": "not_ready"}
	current = frappe.db.get_value(RUN, {"active_session": docname}, "*", as_dict=True, for_update=True)
	if current:
		return {"status": "already_running", "run": current}
	if frappe.db.get_values(
		"Optimus Phase Two Run", {"parent": docname, "status": ["in", ["Recording", "Analyzing"]]},
		["name"], for_update=True, limit=1,
	):
		return {"status": "refused", "reason": "phase2"}
	retry_no = 0
	steps_state, render_pending = "not_requested", 0
	if resume_from:
		if not isinstance(resume_from, str) or len(resume_from) > 140:
			raise ValueError("Invalid refresh resume identity")
		previous = _read(RUN, resume_from, lock=True)
		if not previous or previous["session_name"] != docname or previous["scope"] != scope:
			raise ValueError("Invalid refresh resume parent")
		if previous["state"] not in {"stopped", "cancelled", "interrupted"}:
			return {"status": "refused", "reason": "not_resumable"}
		if frappe.db.get_value(RUN, {"resume_from": resume_from}, "name", for_update=True):
			return {"status": "refused", "reason": "already_resumed"}
		retry_no = previous.get("retry_no", 0)
		if type(retry_no) is not int or retry_no < 0:
			raise JournalUnavailable("Refresh journal retry count is inconsistent")
		retry_no += 1
		if retry_no > 3:
			return {"status": "refused", "reason": "retry_limit"}
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
		# Current reads are essential on MariaDB: the HTTP permission/config
		# checks may have opened an older REPEATABLE READ snapshot before this
		# request waited for admission. A plain SELECT could miss the winner.
		active = frappe.db.get_values(
			RUN,
			{"scope": "all", "state": ["in", sorted(ACTIVE)]},
			["requested_by"],
			as_dict=True,
			for_update=True,
			limit=site_cap + 1,
		)
		if len(active) >= site_cap:
			return {"status": "refused", "reason": "site_cap"}
		if sum(row["requested_by"] == requested_by for row in active) >= user_cap:
			return {"status": "refused", "reason": "user_cap"}
	# PostgreSQL REPEATABLE READ does not refresh even a locking SELECT's
	# snapshot. Changing the mutex row makes an older waiting transaction fail
	# serialization and retry, instead of missing the new active reservation.
	_update(CONTROL, control, generation=(control.get("generation") or 0) + 1)
	run = _insert(
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
		completion_counted=0,
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
	)
	return {"status": "queued", "run": run}


def lock_phase2_parent(docname, *, allow_failed=False):
	"""Caller owns this transaction until its Phase 2 row commits.

	The shared mutex update also fences PostgreSQL REPEATABLE READ snapshots
	opened before an opposing admission. No Redis/provider call happens here.
	"""
	control = frappe.db.get_value(CONTROL, "site", "*", as_dict=True, for_update=True, wait=False)
	if not control:
		raise JournalUnavailable("AI refresh storage is not initialized")
	parent = _read("Optimus Session", docname, lock=True)
	if not parent or parent.get("status") not in ({"Ready", "Failed"} if allow_failed else {"Ready"}):
		return "not_ready"
	if frappe.db.get_value(RUN, {"active_session": docname}, "name", for_update=True):
		return "ai_refresh"
	_update(CONTROL, control, generation=(control.get("generation") or 0) + 1)
	return None


def _owns(run, worker_token, now):
	return bool(
		worker_token
		and run
		and run["state"] == "running"
		and run.get("active_session")
		and run.get("worker_token") == worker_token
		and (run.get("lease_until") or 0) > now
	)


def claim(run_id, *, slice_no, worker_token, now, lease_seconds):
	"""Claim a queued slice once. Replaying even the same token is a no-op."""
	parent, run = _locked(run_id)
	if (
		not parent
		or not run
		or not run.get("active_session")
		or run["state"] != "queued"
		or run["slice_no"] != slice_no
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


def begin_attempt(run_id, *, worker_token, kind, target, input_hash, now):
	"""Persist the send intent before HTTP. Returning None forbids a new call."""
	if kind not in {"fix", "steps"} or not isinstance(target, str) or not target:
		raise ValueError("Invalid refresh target")
	parent, run = _locked(run_id)
	if not parent or parent["status"] != "Ready" or not _owns(run, worker_token, now):
		return None
	if run["deadline"] <= now or run.get("active_attempt") or run.get("breaker_kind"):
		return None
	name = attempt_name(run_id, kind, target)
	if _read(ATTEMPT, name, lock=True):
		return None
	attempt = _insert(
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
	_update(RUN, run, active_attempt=name, attempted=run["attempted"] + 1, heartbeat=now)
	return attempt


def settle_attempt(
	run_id, attempt_id, *, worker_token, outcome, tokens, usage_complete, now, persist=None, error_kind=""
):
	"""Save an outcome, its result and usage in one transaction, at most once.

	The original worker may reconcile a known late outcome after cancellation
	or expiry. It may count its reported usage, but cannot write a stale answer.
	``persist`` is a database-only result write, never a provider call or commit.
	"""
	if (
		outcome not in {"succeeded", "failed", "skipped", "uncertain"}
		or type(tokens) is not int
		or tokens < 0
		or type(usage_complete) is not bool
	):
		raise ValueError("Invalid refresh outcome")
	parent, run = _locked(run_id)
	if not run:
		return False
	attempt = _read(ATTEMPT, attempt_id, lock=True)
	if not attempt or attempt.get("run_id") != run_id or attempt.get("worker_token") != worker_token:
		return False
	if attempt.get("settled") or attempt["state"] not in {"calling", "uncertain"}:
		return False
	can_write = bool(
		parent and parent["status"] == "Ready" and run["deadline"] > now and _owns(run, worker_token, now)
	)
	final = "discarded" if outcome == "succeeded" and not can_write else outcome
	if outcome == "succeeded" and can_write and persist is not None:
		if persist() is False:
			final = "discarded"
	if tokens and parent:
		from optimus.analyze import _add_ai_spend

		_add_ai_spend(parent["name"], tokens)
	was_uncertain = attempt["state"] == "uncertain"
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
	if final == "failed":
		streak += 1
	elif final == "succeeded":
		streak = 0
	breaker = run.get("breaker_kind") or ""
	if final == "failed" and (
		error_kind in {"auth", "not_found", "quota", "config", "rate_limited"} or streak >= 3
	):
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


def _finish_locked(parent, run, *, state, reason, now):
	if not run.get("completion_counted"):
		if parent and run["scope"] == "all" and run.get("completed"):
			from optimus.analyze import _bump_ai_refresh_count

			_bump_ai_refresh_count(parent["name"])
		_update(RUN, run, completion_counted=1)
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


def finish(run_id, *, worker_token, now, reason):
	"""Completion accounting and terminal state are inseparable SQL writes."""
	parent, run = _locked(run_id)
	if not _owns(run, worker_token, now):
		return None
	if run.get("active_attempt"):
		raise RuntimeError("Cannot finish with an unsettled provider call")
	return _finish_locked(
		parent, run, state="complete" if reason == "complete" else "stopped", reason=reason, now=now
	)


def _mark_uncertain(run, reason):
	name = run.get("active_attempt")
	if name:
		attempt = _read(ATTEMPT, name, lock=True)
		if (
			not attempt
			or attempt.get("run_id") != run["name"]
			or attempt.get("worker_token") != run.get("worker_token")
		):
			raise JournalUnavailable("Refresh journal attempt is inconsistent")
		if attempt and attempt["state"] == "calling":
			_update(ATTEMPT, attempt, state="uncertain", error_kind=reason)
			_update(
				RUN,
				run,
				uncertain=(run.get("uncertain") or 0) + 1,
				usage_incomplete=(run.get("usage_incomplete") or 0) + 1,
			)
	_update(RUN, run, active_attempt=None)


def interrupt(run_id, *, now, reason):
	"""Recover an expired worker without automatically repeating its call."""
	parent, run = _locked(run_id)
	if (
		not run
		or run["state"] not in ACTIVE
		or ((run.get("lease_until") or 0) > now and run["deadline"] > now)
	):
		return None
	_mark_uncertain(run, reason)
	return _finish_locked(parent, run, state="interrupted", reason=reason, now=now)


def continue_run(run_id, *, worker_token, now, queued_seconds=1800):
	"""Write the next slice's dispatch intent before the queue sees it."""
	parent, run = _locked(run_id)
	if not _owns(run, worker_token, now):
		return None
	if run.get("active_attempt"):
		raise RuntimeError("Cannot continue with an unsettled provider call")
	if run["deadline"] <= now:
		return _finish_locked(parent, run, state="stopped", reason="deadline", now=now)
	_update(
		RUN,
		run,
		state="queued",
		slice_no=run["slice_no"] + 1,
		worker_token=None,
		lease_until=now + queued_seconds,
		dispatch_pending=1,
		heartbeat=now,
	)
	return run


def cancel(run_id, *, now, cancelled_by=None):
	"""Cancel the durable intent even when Redis is down. Caller authorizes it."""
	parent, run = _locked(run_id)
	if not run or run["state"] not in ACTIVE:
		return None
	_mark_uncertain(run, "cancelled")
	_update(RUN, run, cancelled_by=cancelled_by)
	return _finish_locked(parent, run, state="cancelled", reason="cancelled", now=now)


def prepare_analyze_retry(docname, session_uuid, *, requested_by, now):
	"""Fence optional workers and reset a Failed parent in the same transaction."""
	parent = _read("Optimus Session", docname, lock=True)
	if not parent or parent.get("session_uuid") != session_uuid or parent.get("status") != "Failed":
		return False
	run_id = frappe.db.get_value(RUN, {"active_session": docname}, "name", for_update=True)
	if run_id:
		cancel(run_id, now=now, cancelled_by=requested_by)
	for child in frappe.db.get_values(
		"Optimus Phase Two Run", {"parent": docname, "status": ["in", ["Recording", "Analyzing"]]},
		["name"], as_dict=True, for_update=True,
	):
		frappe.db.set_value("Optimus Phase Two Run", child["name"], {
			"status": "Failed", "analyze_dispatch_pending": 0,
			"warnings_json": '["Phase 2 stopped because profiling analysis was restarted."]',
		}, update_modified=False)
	frappe.db.set_value("Optimus Session", docname, {"status": "Stopping", "analyzer_warnings": None})
	return True


def abandon(run_id, *, worker_token, now):
	"""An escaping worker failure immediately leaves an honest terminal state."""
	parent, run = _locked(run_id)
	if not run or run["state"] != "running" or not worker_token or run.get("worker_token") != worker_token:
		return None
	_mark_uncertain(run, "worker_interrupted")
	return _finish_locked(parent, run, state="interrupted", reason="worker_interrupted", now=now)


def prepare_dispatch(run_id, *, now):
	"""Throttle delivery of a committed SQL intent; claiming acknowledges it.

	Keep the intent pending until a worker claims it, including when Redis
	accepts the job but the enqueue acknowledgement or its transaction is lost.
	The deterministic RQ id saves queue space; claim is the execution fence.
	"""
	parent, run = _locked(run_id)
	if not parent or not run or run["state"] != "queued" or not run.get("dispatch_pending"):
		return None
	if (run.get("lease_until") or 0) <= now or run["deadline"] <= now:
		return None
	previous = run.get("dispatched_at_epoch") or 0
	if previous and now - previous < 30:
		return None
	_update(RUN, run, dispatched_at_epoch=now)
	return run


def checkpoint(run_id, *, worker_token, now, **values):
	"""Publish bounded progress only while the slice owns this generation."""
	if set(values) - {"total_items", "steps_state", "blocked_uncertain"}:
		raise ValueError("Invalid refresh progress fields")
	parent, run = _locked(run_id)
	if not parent or parent["status"] != "Ready" or not _owns(run, worker_token, now):
		return None
	_update(RUN, run, heartbeat=now, **values)
	return run
