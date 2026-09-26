# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Whitelisted HTTP API for the profiler.

Endpoints the floating widget and custom integrations call. Decorated with
`@frappe.whitelist()`, reachable as `/api/method/optimus.api.<name>`.
"""

import html
import time
from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime

from optimus import ratelimit, safe_commit, session
from optimus.permissions import may_act_on_session

# Roles allowed to call the profiler API. System Manager is always allowed
# (Frappe's superuser role); Optimus User is our dedicated role created
# on install via install.after_install. Adding Administrator explicitly
# because frappe.get_roles("Administrator") doesn't include "System Manager".
ALLOWED_ROLES = {"System Manager", "Optimus User", "Administrator"}



# Per-user rate limits (optimus.ratelimit), keyed by endpoint name and counted by the endpoint
# right after its gate passes (the gates themselves never count). A site can override any entry
# in site_config, for example "optimus_rate_limits": {"refill_ai_suggestions": [12, 3600]}.
# Read-only status polling and cancelling a background job are deliberately not limited.
_AI_LIMITS: dict[str, dict[str, int]] = {
	"refill_ai_suggestions": {"limit": 6, "seconds": 3600},
	"test_ai_connection": {"limit": 10, "seconds": 60},
}
_ACTION_LIMITS: dict[str, dict[str, int]] = {
	"regenerate_reports": {"limit": 30, "seconds": 60},
	"retry_analyze": {"limit": 5, "seconds": 60},
	"download_pdf": {"limit": 20, "seconds": 60},
	"export_session": {"limit": 20, "seconds": 60},
}


def _require_user() -> str:
	"""Return the calling user, or throw if Guest."""
	user = frappe.session.user
	if not user or user == "Guest":
		frappe.throw(_("You must be logged in to use the profiler."))
	return user


def _require_profiler_user() -> str:
	"""Return the calling user, or throw if they lack the profiler role.

	HTTP-level enforcement of the widget's role check: without it any
	authenticated user could POST to start a session (the JS check is
	cosmetic).
	"""
	user = _require_user()
	if user == "Administrator":
		return user
	roles = set(frappe.get_roles(user))
	if not (ALLOWED_ROLES & roles):
		frappe.throw(
			_("You need the Optimus User or System Manager role to use the profiler."),
			frappe.PermissionError,
		)
	return user


def _require_session_permission(session_uuid: str, permission_type: str = "read") -> str:
	"""Per-doc read gate for drain_progress, download_pdf, export_session and
	get_phase2_candidates. Session actions use _session_action_gate instead.
	Returns the resolved docname; throws when the ``session_uuid`` is missing
	or unknown, or when the caller lacks ``permission_type`` access.

	SECURITY: fails CLOSED. If ``has_permission`` itself raises, access is
	denied rather than downgraded to the role-only gate.
	"""
	if not session_uuid:
		frappe.throw(_("session_uuid is required"), frappe.ValidationError)
	docname = frappe.db.get_value(
		"Optimus Session", {"session_uuid": session_uuid}, "name"
	)
	if not docname:
		frappe.throw(
			_("No Optimus Session found for uuid {0}").format(html.escape(str(session_uuid))),
			frappe.DoesNotExistError,
			title=_("Optimus"),
		)
	try:
		allowed = frappe.has_permission("Optimus Session", permission_type, docname)
	except Exception:
		try:
			frappe.logger().warning(
				"optimus._require_session_permission: has_permission raised "
				f"for {docname} / {permission_type}; denying (fail-closed)"
			)
		except Exception:
			pass
		frappe.throw(
			_("You do not have permission to access this Optimus Session."),
			frappe.PermissionError,
		)
	if not allowed:
		frappe.throw(
			_("You do not have permission to access this Optimus Session."),
			frappe.PermissionError,
		)
	return docname


@dataclass(frozen=True)
class SessionRef:
	"""What a passed session gate hands the endpoint, so it never re-queries the row.

	``owner`` is the document owner (what the permission rule keys on). ``user`` is the session's
	recording-user field (equal to ``owner`` for sessions created by ``start()``). The acting user
	is ``frappe.session.user``, not a field here.
	"""

	docname: str
	session_uuid: str
	owner: str
	user: str
	status: str
	title: str | None


def _session_action_gate(
	session_uuid: str,
	*,
	action: str,
	statuses: tuple[str, ...] = ("Ready",),
	status_hint: str | None = None,
) -> SessionRef:
	"""The one permission gate for actions that change an Optimus Session or spend AI tokens on it.

	Allowed when the caller has the profiler role, can read the session AND is its owner, a System
	Manager (write through the DocPerm) or a user it is shared with for editing
	(``permissions.may_act_on_session``, design decision D1). The Optimus Session DocPerm is
	unchanged: a plain Optimus User keeps read with if_owner and no write.

	Fails closed: if ``frappe.has_permission`` raises, the caller is denied. ``statuses`` lists the
	session statuses the action accepts; an empty tuple accepts any status. Status is checked only
	after permission, so a stranger learns nothing about the session. ``status_hint`` (translated,
	``{0}`` = the session's current status) replaces the default status-mismatch message.

	Writes nothing and counts no rate limit: the endpoint calls
	``ratelimit.enforce_user_rate_limit`` itself right after this returns. ``action`` is the
	endpoint name and labels the log line.
	"""
	user = _require_profiler_user()
	if not session_uuid:
		frappe.throw(_("session_uuid is required"), frappe.ValidationError, title=_("Optimus"))
	row = frappe.db.get_value(
		"Optimus Session",
		{"session_uuid": session_uuid},
		["name", "owner", "user", "status", "title"],
		as_dict=True,
	)
	if not row:
		frappe.throw(
			_("No Optimus Session found for uuid {0}").format(html.escape(str(session_uuid))),
			frappe.DoesNotExistError,
			title=_("Optimus"),
		)
	engine_failed = False
	try:
		can_read = frappe.has_permission("Optimus Session", "read", row["name"], user=user)
		can_write = frappe.has_permission("Optimus Session", "write", row["name"], user=user)
	except Exception:
		engine_failed = True
		can_read = can_write = False
	if engine_failed:
		try:
			frappe.logger("optimus").warning(
				f"optimus._session_action_gate: has_permission raised for {row['name']} ({action}); denying"
			)
		except Exception:
			pass
	if not may_act_on_session(
		user=user, owner=row["owner"] or "", can_read=bool(can_read), can_write=bool(can_write)
	):
		frappe.throw(
			_(
				"You can't do this on this Optimus Session. Only its owner, a System Manager or "
				"a user it is shared with for editing can."
			),
			frappe.PermissionError,
			title=_("Optimus"),
		)
	if statuses and row["status"] not in statuses:
		if status_hint:
			message = status_hint.format(row["status"])
		else:
			message = _("This action needs a session in status {0}. This one is {1}.").format(
				", ".join(statuses), row["status"]
			)
		frappe.throw(message, frappe.ValidationError, title=_("Optimus"))
	return SessionRef(
		docname=row["name"],
		session_uuid=session_uuid,
		owner=row["owner"] or "",
		user=row["user"] or "",
		status=row["status"],
		title=row.get("title") or None,
	)


def _ai_unavailable_message(section: str | None) -> str:
	"""Translated reason an AI action can't run: the provider isn't configured (``section`` None),
	or the per-section toggle is off."""
	if section == "findings":
		return _(
			'AI fix suggestions on findings are turned off. Turn on "Fix suggestions on findings" '
			"under Optimus Settings ▸ AI."
		)
	if section == "humanize":
		return _('AI-written "Steps to Reproduce" is turned off. Turn it on under Optimus Settings ▸ AI.')
	return _(
		"AI suggestions aren't configured. Turn them on under Optimus Settings ▸ AI and set a "
		"provider, a model and, if the provider needs one, an API key."
	)


def _ai_session_gate(session_uuid: str, *, section: str | None, action: str) -> SessionRef:
	"""``_session_action_gate`` for AI actions (Ready sessions only), then the AI availability
	check for ``section`` ("findings", "humanize", or None for the combined refresh), then the
	per-user rate limit for ``action`` (a key of ``_AI_LIMITS``). The limit is counted only for a
	caller who passed every earlier check, so denied or misconfigured calls never use up a user's
	budget."""
	ref = _session_action_gate(session_uuid, action=action)
	from optimus import ai_fix

	if not ai_fix.is_available():
		frappe.throw(_ai_unavailable_message(None), frappe.ValidationError, title=_("Optimus"))
	if section and not ai_fix.is_available(section=section):
		frappe.throw(_ai_unavailable_message(section), frappe.ValidationError, title=_("Optimus"))
	ratelimit.enforce_user_rate_limit(action, **_AI_LIMITS[action])
	return ref


def _phase2_run_gate(
	run_uuid: str, *, action: str, run_statuses: tuple[str, ...] = ()
) -> tuple[SessionRef, dict]:
	"""Resolve a Phase 2 Run to its parent session and run ``_session_action_gate`` on it (a
	run_uuid is not self-authorizing). The parent's status is not constrained (develop never
	constrained it); ``run_statuses`` constrains the run row and is checked after permission.
	Returns the session ref and the run row ``{name, parent, status}``."""
	_require_profiler_user()
	if not run_uuid:
		frappe.throw(_("run_uuid is required"), frappe.ValidationError, title=_("Optimus"))
	run = frappe.db.get_value(
		"Optimus Phase Two Run", {"run_uuid": run_uuid}, ["name", "parent", "status"], as_dict=True
	)
	session_uuid = frappe.db.get_value("Optimus Session", run["parent"], "session_uuid") if run else None
	if not run or not session_uuid:
		frappe.throw(
			_("Phase 2 run {0} was not found.").format(html.escape(str(run_uuid))),
			frappe.DoesNotExistError,
			title=_("Optimus"),
		)
	ref = _session_action_gate(session_uuid, action=action, statuses=())
	if run_statuses and run["status"] not in run_statuses:
		frappe.throw(
			_("This Phase 2 run is {0}. This action needs it to be {1}.").format(
				run["status"], ", ".join(run_statuses)
			),
			frappe.ValidationError,
			title=_("Optimus"),
		)
	return ref, run


def _save_parent_bypassing_perms(parent) -> None:
	"""Persist a phase-2 line-profile parent Optimus Session. Ownership is
	enforced by the caller's _session_action_gate / _phase2_run_gate before this runs,
	so the DocType write-perm is bypassed here."""
	parent.flags.ignore_validate_update_after_submit = True
	parent.save(ignore_permissions=True)
	safe_commit()


@frappe.whitelist(methods=["POST"])
def start(
	label: str = "",
	capture_python_tree: bool = True,
	notes: str = "",
) -> dict:
	"""Begin a profiling session for the calling user.

	If the user already has an active session, it is stopped first (marked
	`Stopping`, Redis pointer cleared), making start() idempotent: clicking
	Start twice never produces two parallel sessions.

	Args:
	    label: Human-readable session label.
	    capture_python_tree: When True (default), pyinstrument captures a
	        Python call tree per recording and sidecar wraps capture
	        frappe.get_doc / cache.get_value / has_permission argument
	        identities. When False, only the SQL recorder runs.
	    notes: Free-form "steps to reproduce" / context text rendered at the
	        top of the report; also editable on the Optimus Session form.
	"""
	user = _require_profiler_user()

	# v0.3.0: clear any in-flight capture state from a previous request
	# on this worker BEFORE we look at session state, so leaked state
	# from a concurrent request doesn't influence the new session.
	from optimus import capture

	capture._force_stop_inflight_capture(local_proxy=frappe.local)

	# v0.5.0: mirror for infra capture.
	from optimus import infra_capture

	infra_capture._force_stop_inflight(frappe.local)

	# If the user is already recording, gracefully stop the previous one.
	previous = session.get_active_session_for(user)
	if previous:
		_stop_session(user, previous)

	session_uuid = frappe.generate_hash(length=16)
	now = now_datetime()
	title = (label or "").strip() or f"Profiling session @ {now.strftime('%Y-%m-%d %H:%M:%S')}"
	# Session name is a Data field (varchar 140) cap here so a long label from a
	# direct API call is cleanly trimmed instead of silently truncated by the DB.
	title = title[:140]

	# Create the DocType row in Recording state.
	doc_fields = {
		"doctype": "Optimus Session",
		"session_uuid": session_uuid,
		"title": title,
		"user": user,
		"status": "Recording",
		"started_at": now,
	}
	# v0.5.0: persist steps-to-reproduce / notes captured at start time.
	notes_clean = (notes or "").strip()
	if notes_clean:
		doc_fields["notes"] = notes_clean
	# Caller passed _require_profiler_user; the profiler creates their own
	# session, which regular users have no create-perm for.
	doc = frappe.get_doc(doc_fields).insert(ignore_permissions=True)

	# Store metadata in Redis (used by the analyze pipeline later).
	session.set_session_meta(
		session_uuid,
		{
			"session_uuid": session_uuid,
			"docname": doc.name,
			"user": user,
			"label": title,
			"started_at": now.isoformat(),
			"capture_python_tree": bool(capture_python_tree),
		},
	)

	# Flip the user's active flag last once this is set, the next
	# request from this user will start being recorded.
	session.set_active_session(user, session_uuid)

	return {
		"session_uuid": session_uuid,
		"docname": doc.name,
		"title": title,
		"started_at": now.isoformat(),
	}


@frappe.whitelist(methods=["POST"])
def stop() -> dict:
	"""End the calling user's active profiling session.

	Clears the Redis active pointer, marks the row ``Stopping`` and either
	enqueues the analyze job or runs it inline (scheduler-aware fallback, see
	``_enqueue_analyze``).

	Returns a dict with ``stopped``, ``session_uuid``, ``docname``,
	``ran_inline`` and, only when ran_inline is True, the final ``status``
	(``Ready`` or ``Failed``). The widget uses ran_inline + status to pick its
	terminal state (Analyzing / Report ready / Analyze failed).
	"""
	user = _require_profiler_user()
	active = session.get_active_session_for(user)
	if not active:
		return {"stopped": False, "reason": "no active session"}

	docname, ran_inline = _stop_session(user, active)

	# v0.5.0: when analyze runs inline, the session is already finalized
	# by the time we return. Read the actual status so the widget can
	# transition to the right terminal state otherwise a failed
	# inline analyze would show "Report ready" to the user despite the
	# session being marked Failed server-side.
	final_status = None
	if ran_inline and docname:
		try:
			final_status = frappe.db.get_value(
				"Optimus Session", docname, "status"
			)
		except Exception:
			final_status = None

	return {
		"stopped": True,
		"session_uuid": active,
		"docname": docname,
		"ran_inline": ran_inline,
		"status": final_status,
	}


def _stop_session(user: str, session_uuid: str) -> tuple[str | None, bool]:
	"""Clear the active pointer, mark the row Stopping and enqueue (or
	inline-run) the analyze job.

	Returns ``(docname, ran_inline)``: docname is None if no row matched;
	ran_inline is True when the session is already finalized (analyze ran
	synchronously or was rejected by the inline cap and marked Failed), False
	when analyze will run async.
	"""
	# v0.3.0: stop any in-flight pyinstrument session and clear capture
	# state on this worker before flipping the active flag, so a previous
	# in-flight capture from the same worker doesn't leak into a new
	# session started immediately after.
	from optimus import capture

	capture._force_stop_inflight_capture(local_proxy=frappe.local)

	# v0.5.0: clear any leaked infra start snapshot from a previous
	# session on the same worker.
	from optimus import infra_capture

	infra_capture._force_stop_inflight(frappe.local)

	_clear_active(user)
	docname = _mark_stopping(user, session_uuid)
	if not docname:
		return None, False

	# v0.5.1: notify any open widgets on this user's session that
	# we're transitioning out of Recording. Second, third, fourth
	# tabs will all switch their displays simultaneously without
	# polling. The stop-click itself runs on ONE tab; the others
	# learn about the stop via this event.
	_publish_session_event(
		"optimus_session_stopping",
		session_uuid=session_uuid,
		docname=docname,
		user=user,
	)

	# v0.6.0: if the flow enqueued background jobs, keep the session
	# accepting their recordings for a bounded window after Stop (the
	# active pointer was just cleared). analyze.run waits for these jobs
	# to finish before gathering recordings, so they aren't lost. The
	# draining deadline is the analyze wait + a grace margin covering the
	# analyze run itself. No-op when the wait is disabled (=0) or nothing
	# was enqueued.
	try:
		from optimus.settings import get_config

		wait_seconds = int(getattr(get_config(), "background_job_wait_seconds", 0) or 0)
		if wait_seconds > 0 and session.get_pending_jobs(session_uuid):
			session.set_draining(session_uuid, time.time() + wait_seconds + 60)
			# v0.13: surface the post-stop background-job capture as its own
			# status otherwise the session sits at "Stopping" for the whole
			# drain window (up to background_job_wait_seconds, default 300s) and
			# looks stuck. analyze.run keeps it here through the drain, then
			# flips to "Analyzing" once the jobs finish / the window closes.
			# safe_commit so the enqueued analyze worker can't read a stale
			# "Stopping" if the request transaction commits late.
			frappe.db.set_value(
				"Optimus Session", docname, "status", "Capturing Background Jobs"
			)
			safe_commit()
	except Exception:
		frappe.log_error(title="optimus set draining window")

	# v0.5.0: inline safety cap + scheduler fallback are both inside
	# _enqueue_analyze now, so every inline-path caller (stop,
	# retry_analyze, janitor) gets the same protection uniformly.
	ran_inline = _enqueue_analyze(session_uuid, docname=docname)
	return docname, ran_inline


def _publish_session_event(
	event_name: str,
	*,
	session_uuid: str,
	docname: str | None,
	user: str,
	**extra,
) -> None:
	"""Publish a session-state realtime event to all the user's Desk tabs via
	Frappe's Socket.IO bridge, driving the floating widget without HTTP
	polling. Event names: optimus_session_{stopping,analyzing,ready,failed}.

	All events carry ``session_uuid`` and ``docname`` so a widget with
	multiple open tabs can match the session it tracks. Best-effort: publish
	failures are swallowed, never interrupting the caller."""
	payload = {"session_uuid": session_uuid, "docname": docname}
	payload.update(extra)
	try:
		frappe.publish_realtime(event_name, payload, user=user)
	except Exception:
		# Don't log realtime is best-effort, the widget falls back
		# to its on-visibility-change status fetch.
		pass


def _clear_active(user: str) -> None:
	"""Remove the user's active session pointer from Redis.

	Idempotent; safe to call even if no session is active. Once this
	returns, no further requests from this user will activate recording.
	"""
	session.clear_active_session(user)


def _mark_stopping(user: str, session_uuid: str) -> str | None:
	"""Transition the Optimus Session row to the Stopping state.

	Returns the docname on success or None if no matching row exists.
	"""
	docname = frappe.db.get_value(
		"Optimus Session",
		{"session_uuid": session_uuid, "user": user},
		"name",
	)
	if not docname:
		return None

	frappe.db.set_value(
		"Optimus Session",
		docname,
		{"status": "Stopping", "stopped_at": now_datetime()},
	)
	safe_commit()
	return docname


def _enqueue_analyze(session_uuid: str, docname: str | None = None) -> bool:
	"""Enqueue analyze on the long queue, or run it inline when no worker
	will consume it (scheduler disabled).

	Inline analyze has a recording-count safety cap
	(``optimus_inline_analyze_limit``, default 50) to stay within gunicorn's
	request timeout. When the cap is exceeded and ``docname`` is given, the
	session is marked Failed with an actionable message and analyze is NOT
	run. Passing ``docname`` (all production callers do) enables that cap;
	None skips it.

	Returns True if the session is already finalized (Ready or Failed) by
	return: analyze ran synchronously or was rejected by the cap. Returns
	False when the job was pushed to the async queue (session still Stopping).
	Inline failures are caught here so the caller doesn't return a 500.
	"""
	from frappe.utils.scheduler import is_scheduler_disabled

	run_inline = False
	try:
		run_inline = bool(is_scheduler_disabled())
	except Exception:
		frappe.log_error(title="optimus scheduler check")

	if run_inline:
		# Inline cap check refuse huge sessions that would exceed
		# gunicorn's request timeout. Only applied when docname is
		# provided (all production callers provide it).
		if docname:
			cap = frappe.conf.get("optimus_inline_analyze_limit") or 50
			try:
				count = session.recording_count(session_uuid)
			except Exception:
				count = 0
			if count > cap:
				# IMPORTANT: the field is `analyzer_warnings` (plural,
				# with -s). An earlier v0.5.0 version wrote to a
				# phantom `analyze_error` field which doesn't exist
				# on the doctype, causing MariaDB to raise 'Unknown
				# column' and the stop API to return 500. The test
				# suite missed this because FakeDB.set_value accepted
				# any field name as a no-op.
				try:
					frappe.db.set_value(
						"Optimus Session",
						docname,
						{
							"status": "Failed",
							"analyzer_warnings": (
								f"Scheduler is disabled and this session "
								f"has {count} recordings, exceeding the "
								f"inline analyze cap of {cap}. "
								f"Re-enable the scheduler "
								f"(bench enable-scheduler) and click "
								f"Retry Analyze on this session's form view."
							),
						},
					)
					safe_commit()
				except Exception:
					frappe.log_error(
						title="optimus inline cap mark Failed"
					)
				# The session is finalized (Failed). Return True so
				# the caller treats it like any other inline result.
				return True

		frappe.logger().warning(
			f"optimus: scheduler disabled; running analyze "
			f"inline for session {session_uuid}. Caller will block "
			f"until analyze completes."
		)
		try:
			frappe.enqueue(
				"optimus.analyze.run",
				queue="long",
				session_uuid=session_uuid,
				now=True,
			)
		except Exception:
			# analyze.run already marked the session Failed and
			# re-raised. Swallow here so the caller returns 200 the
			# caller reads the final status off the doc and reports
			# it to the widget.
			frappe.log_error(
				title=f"optimus inline analyze {session_uuid}"
			)
		return True

	# Enqueue analyze on the async "long" queue. NOTE (v0.7.x): we deliberately
	# do NOT dedup with a stable job_id + is_job_enqueued guard. That guard
	# stranded sessions at "Stopping": a worker OOM-killed mid-analyze leaves a
	# zombie STARTED job, is_job_enqueued then returns True, the enqueue is
	# skipped and nothing transitions the session out of "Stopping" (retry hits
	# the same guard; no janitor sweep covered it). Concurrent-analyze RAM is
	# already bounded by analyze.run's single-flight, so a rare duplicate from a
	# double-Stop is harmless; a permanent strand is not. The janitor's
	# _sweep_stale_stopping is the durable backstop.
	frappe.enqueue(
		"optimus.analyze.run",
		queue="long",
		session_uuid=session_uuid,
		now=False,
	)
	return False


@frappe.whitelist()
def status() -> dict:
	"""Return whether the calling user has an active profiling session."""
	user = _require_profiler_user()
	active = session.get_active_session_for(user)
	if not active:
		return {"active": False}

	meta = session.get_session_meta(active) or {}
	return {
		"active": True,
		"session_uuid": active,
		"docname": meta.get("docname"),
		"label": meta.get("label"),
		"started_at": meta.get("started_at"),
	}


@frappe.whitelist()
def get_active_session() -> dict | None:
	"""Return full metadata for the calling user's active session, or None."""
	user = _require_profiler_user()
	active = session.get_active_session_for(user)
	if not active:
		return None
	return session.get_session_meta(active)


@frappe.whitelist()
def drain_progress(session_uuid: str) -> dict:
	"""Live progress of the post-Stop background-job capture, for the form's
	"Capturing Background Jobs (N left)" headline. Returns the current session
	status plus the number of the flow's jobs still pending in Redis; ``pending``
	drops to 0 as they finish and ``status`` flips to ``Analyzing`` once
	``analyze.run`` takes over the drained recordings."""
	_require_session_permission(session_uuid)
	status = frappe.db.get_value(
		"Optimus Session", {"session_uuid": session_uuid}, "status"
	)
	# Window remaining: set_draining stored deadline = stop + wait_seconds + 60
	# (60s grace for the analyze run itself); subtract the grace to get the
	# bg-wait window the user actually sits through (≈ background_job_wait_seconds
	# at the start, counting down to 0). The session can't stay here past it.
	meta = session.get_session_meta(session_uuid) or {}
	remaining = None
	until = meta.get("draining_until")
	if until:
		try:
			remaining = max(0, int(float(until) - time.time()) - 60)
		except (TypeError, ValueError):
			remaining = None
	try:
		from optimus.settings import get_config

		window = int(getattr(get_config(), "background_job_wait_seconds", 0) or 0)
	except Exception:
		window = 0
	return {
		"status": status,
		"pending": len(session.get_pending_jobs(session_uuid)),
		"remaining_seconds": remaining,
		"window_seconds": window or None,
	}


# ---------------------------------------------------------------------------
# v0.5.0: frontend metrics receiver
# ---------------------------------------------------------------------------

SOFT_CAP_FRONTEND_XHR = 1000
SOFT_CAP_FRONTEND_VITALS = 200


# v0.12.0: keys centralized in optimus.redis_keys. The two local helpers
# kept their original names + signatures so call sites don't churn, but
# now delegate to the canonical builders. Future PRs can inline the
# calls; for now this keeps the api.py diff localized.
def _frontend_xhr_key(session_uuid: str) -> str:
	from optimus import redis_keys

	return redis_keys.frontend_xhr(session_uuid)


def _frontend_vitals_key(session_uuid: str) -> str:
	from optimus import redis_keys

	return redis_keys.frontend_vitals(session_uuid)


@frappe.whitelist(methods=["POST"])
def submit_frontend_metrics(payload: str) -> dict:
	"""Receive a batch of frontend metrics (XHR timings + Web Vitals) as a
	JSON string (sendBeacon sends a raw Blob, so parsing is explicit).

	Stored as two Redis lists per session, appended via atomic RPUSH + LTRIM
	so concurrent submits (stop-time frappe.call vs a beforeunload beacon)
	don't lose entries; LTRIM enforces the soft cap (newest survive).

	Redis keys: profiler:frontend:<uuid>:{xhr,vitals} (JSON-encoded entries).
	"""
	user = _require_profiler_user()

	try:
		if isinstance(payload, str):
			data = frappe.parse_json(payload)
		else:
			data = payload
	except Exception:
		return {"accepted": False, "reason": "invalid json"}

	if not isinstance(data, dict):
		return {"accepted": False, "reason": "invalid payload"}

	session_uuid = data.get("session_uuid")
	if not session_uuid:
		return {"accepted": False, "reason": "missing session_uuid"}

	# Ownership check: only the user who owns the session can write to
	# its frontend blob. Silent-drop on missing meta because a
	# beforeunload beacon can legitimately arrive after the session has
	# already been stopped and its meta deleted no log spam.
	meta = session.get_session_meta(session_uuid) or {}
	if not meta or meta.get("user") != user:
		return {"accepted": False, "reason": "session not found"}

	# Client-side tail-preferring cap so a single oversized submit
	# doesn't push MAX entries through Redis only to have them trimmed.
	import json as _json

	xhr = (data.get("xhr") or [])[-SOFT_CAP_FRONTEND_XHR:]
	vitals = (data.get("vitals") or [])[-SOFT_CAP_FRONTEND_VITALS:]

	xhr_key = _frontend_xhr_key(session_uuid)
	vitals_key = _frontend_vitals_key(session_uuid)

	# Atomic append: RPUSH + LTRIM. Each entry is JSON-encoded as a
	# Redis list element. frappe.cache.rpush accepts one value at a
	# time we loop, which is O(n) round trips but fine at the
	# submission sizes we cap at (≤ 1000 XHRs, ≤ 200 vitals per call).
	if xhr:
		for entry in xhr:
			try:
				frappe.cache.rpush(xhr_key, _json.dumps(entry, default=str))
			except Exception:
				frappe.log_error(title="optimus frontend rpush (xhr)")
		# Tail-preferring trim: keep the last N entries.
		try:
			frappe.cache.ltrim(xhr_key, -SOFT_CAP_FRONTEND_XHR, -1)
			session.expire_key(xhr_key, session.SESSION_TTL_SECONDS)
		except Exception:
			frappe.log_error(title="optimus frontend ltrim (xhr)")

	if vitals:
		for entry in vitals:
			try:
				frappe.cache.rpush(vitals_key, _json.dumps(entry, default=str))
			except Exception:
				frappe.log_error(title="optimus frontend rpush (vitals)")
		try:
			frappe.cache.ltrim(vitals_key, -SOFT_CAP_FRONTEND_VITALS, -1)
			session.expire_key(vitals_key, session.SESSION_TTL_SECONDS)
		except Exception:
			frappe.log_error(title="optimus frontend ltrim (vitals)")

	# Report the current post-merge sizes so the client can confirm.
	try:
		xhr_count = frappe.cache.llen(xhr_key) or 0
	except Exception:
		xhr_count = 0
	try:
		vitals_count = frappe.cache.llen(vitals_key) or 0
	except Exception:
		vitals_count = 0

	return {
		"accepted": True,
		"xhr_count": xhr_count,
		"vitals_count": vitals_count,
	}


def _read_frontend_data(session_uuid: str) -> dict:
	"""Read the submit_frontend_metrics Redis lists back into the dict shape
	the frontend_timings analyzer expects. Bad JSON entries are silently
	skipped (the analyzer handles partial data).
	"""
	import json as _json

	xhr_key = _frontend_xhr_key(session_uuid)
	vitals_key = _frontend_vitals_key(session_uuid)

	def _decode_list(key):
		try:
			raw = frappe.cache.lrange(key, 0, -1) or []
		except Exception:
			return []
		out = []
		for item in raw:
			if isinstance(item, bytes):
				item = item.decode("utf-8", errors="replace")
			try:
				out.append(_json.loads(item))
			except Exception:
				continue
		return out

	return {
		"xhr": _decode_list(xhr_key),
		"vitals": _decode_list(vitals_key),
	}


@frappe.whitelist()
def health() -> dict:
	"""Health/metrics endpoint for ops scrapers: counts by session status and
	analyze-pipeline performance over the last 24 hours. Aggregate counts
	only, no session contents. Permission: any profiler user.
	"""
	_require_profiler_user()
	return {
		"by_status": _session_count_by_status(),
		"by_top_severity_ready": _session_count_by_severity(),
		"last_24h": _session_perf_24h(),
	}


# The health aggregates go through frappe.qb (the query builder), not raw SQL
# and not get_all aggregate-strings: Frappe v16 rejects "count(name) as x" as a
# field string and the get_all ``{'COUNT': 'name'}`` dict form returns a
# dialect-specific key (``COUNT(`name`)``). qb with explicit ``.as_()`` aliases
# is the one portable form (MariaDB + Postgres). Imports are local so api.py
# still imports under the unit-test frappe stub (which has no query builder);
# health() is unit-tested by mocking these three helpers.

def _session_count_by_status() -> dict:
	"""``{status: count}`` across all sessions."""
	from frappe.query_builder.functions import Count

	s = frappe.qb.DocType("Optimus Session")
	rows = (
		frappe.qb.from_(s)
		.select(s.status, Count(s.name).as_("cnt"))
		.groupby(s.status)
	).run(as_dict=True)
	return {r["status"]: int(r["cnt"]) for r in rows}


def _session_perf_24h() -> dict:
	"""Analyze-pipeline perf over the last 24h for Ready sessions. Cutoff is
	computed in Python (no MariaDB-only ``NOW() - INTERVAL``)."""
	from frappe.query_builder.functions import Avg, Count, Max

	cutoff = add_to_date(now_datetime(), days=-1)
	s = frappe.qb.DocType("Optimus Session")
	rows = (
		frappe.qb.from_(s)
		.select(
			Count(s.name).as_("cnt"),
			Avg(s.analyze_duration_ms).as_("avg_ms"),
			Max(s.analyze_duration_ms).as_("max_ms"),
		)
		.where((s.status == "Ready") & (s.modified > cutoff))
	).run(as_dict=True)
	agg = rows[0] if rows else {}
	return {
		"sessions_ready": int(agg.get("cnt") or 0),
		"analyze_avg_ms": round(float(agg.get("avg_ms") or 0), 2),
		"analyze_max_ms": round(float(agg.get("max_ms") or 0), 2),
	}


def _session_count_by_severity() -> dict:
	"""``{top_severity: count}`` for Ready sessions; NULL/'' → 'None'."""
	from frappe.query_builder.functions import Count

	s = frappe.qb.DocType("Optimus Session")
	rows = (
		frappe.qb.from_(s)
		.select(s.top_severity, Count(s.name).as_("cnt"))
		.where(s.status == "Ready")
		.groupby(s.top_severity)
	).run(as_dict=True)
	return {(r["top_severity"] or "None"): int(r["cnt"]) for r in rows}


# v0.4.0: onboarding toast state endpoints. Used by floating_widget.js
# to decide whether to render the one-time onboarding toast.
# v0.12.0: key construction moved to optimus.redis_keys.onboarding_seen.
# The TTL constant stays here as the policy lever bump from the 1-year
# default if a real complaint surfaces. The PREFIX constant is gone; the
# read/write sites below call redis_keys.onboarding_seen(user) directly.
ONBOARDING_CACHE_TTL_SECONDS = 365 * 24 * 60 * 60  # 1 year


@frappe.whitelist()
def check_onboarding_seen() -> dict:
	"""Has the current user dismissed the onboarding toast?

	Also returns True if the user has any existing Ready Optimus Session
	row (they're an experienced user; suppress the toast).
	"""
	user = _require_user()
	# Suppress for experienced users anyone with at least one Ready session
	try:
		existing = frappe.db.count(
			"Optimus Session",
			filters={"user": user, "status": "Ready"},
		)
		if existing and existing > 0:
			return {"seen": True}
	except Exception:
		pass
	# v0.12.13: onboarding_seen is the second value migrated to the
	# v0.12.0 versioned envelope. ``unwrap_value`` returns either the
	# wrapped payload (new-shape writers, v0.12.13+) or the legacy bare
	# value (pre-v0.12.13 writers strings like "1"). Both shapes
	# are truthy, so ``bool(payload)`` resolves the dismissed flag
	# regardless of which writer set it.
	from optimus import redis_keys, redis_schema

	raw = frappe.cache.get_value(redis_keys.onboarding_seen(user))
	payload, _version = redis_schema.unwrap_value(raw)
	return {"seen": bool(payload)}


@frappe.whitelist(methods=["POST"])
def mark_onboarding_seen() -> dict:
	"""Mark the onboarding toast as dismissed for the current user."""
	user = _require_user()
	from optimus import redis_keys, redis_schema

	frappe.cache.set_value(
		redis_keys.onboarding_seen(user),
		redis_schema.wrap_value("1"),
		expires_in_sec=ONBOARDING_CACHE_TTL_SECONDS,
	)
	return {"seen": True}


@frappe.whitelist()
def download_pdf(session_uuid: str) -> dict:
	"""Return the URL of the report PDF, generating it on first call.

	Permission: recording user, System Manager, or Administrator.
	Mirrors retry_analyze / export_session permission gating.
	"""
	user = _require_profiler_user()
	if not session_uuid:
		frappe.throw(_("session_uuid is required"))
	_require_session_permission(session_uuid, "read")

	row = frappe.db.get_value(
		"Optimus Session",
		{"session_uuid": session_uuid},
		["name", "user", "status"],
		as_dict=True,
	)
	if not row:
		frappe.throw(_("No Optimus Session found for uuid {0}").format(session_uuid))
	if row["status"] != "Ready":
		frappe.throw(_("Cannot generate PDF for session in '{0}' state").format(row['status']))

	roles = set(frappe.get_roles(user))
	if (
		row["user"] != user
		and "System Manager" not in roles
		and user != "Administrator"
	):
		frappe.throw(
			_("You can only download PDFs for your own sessions."),
			frappe.PermissionError,
		)

	ratelimit.enforce_user_rate_limit("download_pdf", **_ACTION_LIMITS["download_pdf"])

	from optimus import pdf_export

	url = pdf_export.get_or_generate_pdf(session_uuid)
	return {"file_url": url}


@frappe.whitelist()
def export_session(session_uuid: str) -> dict:
	"""Export an Optimus Session as a structured JSON blob for programmatic
	consumption (no HTML parsing): the full session with all child rows, top
	queries, table breakdown and finding technical details.

	Permission: recording user or System Manager only (mirrors the report
	download gate); other users get a permission error.
	"""
	import json

	user = _require_profiler_user()
	if not session_uuid:
		frappe.throw(_("session_uuid is required"))
	_require_session_permission(session_uuid, "read")

	row = frappe.db.get_value(
		"Optimus Session",
		{"session_uuid": session_uuid},
		"name",
		as_dict=True,
	)
	if not row:
		frappe.throw(_("No Optimus Session found for uuid {0}").format(session_uuid))

	doc = frappe.get_doc("Optimus Session", row["name"])

	# Permission gate (same logic as retry_analyze)
	roles = set(frappe.get_roles(user))
	if doc.user != user and "System Manager" not in roles and user != "Administrator":
		frappe.throw(_("You can only export your own sessions."), frappe.PermissionError)

	ratelimit.enforce_user_rate_limit("export_session", **_ACTION_LIMITS["export_session"])

	def _parse_json_field(value):
		if not value:
			return []
		try:
			return json.loads(value)
		except Exception:
			return []

	return {
		"schema_version": 1,
		"exported_at": frappe.utils.now_datetime().isoformat(),
		"session": {
			"session_uuid": doc.session_uuid,
			"title": doc.title,
			"user": doc.user,
			"status": doc.status,
			"started_at": str(doc.started_at) if doc.started_at else None,
			"stopped_at": str(doc.stopped_at) if doc.stopped_at else None,
			"total_duration_ms": doc.total_duration_ms,
			"total_requests": doc.total_requests,
			"total_queries": doc.total_queries,
			"total_query_time_ms": doc.total_query_time_ms,
			"analyze_duration_ms": getattr(doc, "analyze_duration_ms", None),
			"top_severity": getattr(doc, "top_severity", None),
			"analyzer_warnings": doc.analyzer_warnings,
			# v0.3.0 fields
			"total_python_ms": getattr(doc, "total_python_ms", None),
			"total_sql_ms": getattr(doc, "total_sql_ms", None),
			# v0.13: AI token usage (cumulative spend + refresh count + steps)
			"ai_tokens_spent": getattr(doc, "ai_tokens_spent", None),
			"ai_refresh_count": getattr(doc, "ai_refresh_count", None),
			"ai_steps_tokens": getattr(doc, "ai_steps_tokens", None),
		},
		"actions": [
			{
				"idx": a.idx,
				"action_label": a.action_label,
				"event_type": a.event_type,
				"http_method": a.http_method,
				"path": a.path,
				"recording_uuid": a.recording_uuid,
				"duration_ms": a.duration_ms,
				"queries_count": a.queries_count,
				"query_time_ms": a.query_time_ms,
				"slowest_query_ms": a.slowest_query_ms,
				# v0.3.0: include the call tree (or its overflow marker)
				"call_tree": _parse_json_field(getattr(a, "call_tree_json", None)),
			}
			for a in (doc.actions or [])
		],
		"findings": [
			{
				"idx": f.idx,
				"finding_type": f.finding_type,
				"severity": f.severity,
				"title": f.title,
				"customer_description": f.customer_description,
				"technical_detail": _parse_json_field(f.technical_detail_json),
				"estimated_impact_ms": f.estimated_impact_ms,
				"affected_count": f.affected_count,
				"action_ref": f.action_ref,
			}
			for f in (doc.findings or [])
		],
		"top_queries": _parse_json_field(doc.top_queries_json),
		"table_breakdown": _parse_json_field(doc.table_breakdown_json),
		# v0.3.0 top-level aggregates
		"hot_frames": _parse_json_field(getattr(doc, "hot_frames_json", None)),
		"session_time_breakdown": _parse_json_field(
			getattr(doc, "session_time_breakdown_json", None)
		),
	}


@frappe.whitelist(methods=["POST"])
def retry_analyze(session_uuid: str) -> dict:
	"""Retry the analyze job for a Failed session.

	Lets the session owner, a System Manager or a write-sharee (``_session_action_gate``) recover
	from transient analyzer errors (worker crash, DB timeout, ...) without a Frappe console. The
	session must be Failed; any other status is refused with a clear message.
	"""
	ref = _session_action_gate(session_uuid, action="retry_analyze", statuses=("Failed",))
	ratelimit.enforce_user_rate_limit("retry_analyze", **_ACTION_LIMITS["retry_analyze"])

	# Reset to Stopping so the analyze pipeline runs through its usual state transitions.
	frappe.db.set_value(
		"Optimus Session",
		ref.docname,
		{"status": "Stopping", "analyzer_warnings": None},
	)
	safe_commit()

	# Clear the cached PDF so the next download regenerates from the fresh HTML.
	try:
		from optimus import pdf_export

		pdf_export.clear_cached_pdf(ref.session_uuid)
	except Exception:
		pass

	# The scheduler-aware enqueue helper, so retry also works where the scheduler is
	# disabled (a bare frappe.enqueue would push to a queue no worker consumes). Passing docname
	# lets the inline cap mark the session Failed with a clear message when it is too large.
	ran_inline = _enqueue_analyze(ref.session_uuid, docname=ref.docname)

	# Read back the final status if inline analyze ran (same contract as stop()).
	final_status = None
	if ran_inline:
		try:
			final_status = frappe.db.get_value("Optimus Session", ref.docname, "status")
		except Exception:
			final_status = None

	return {
		"retried": True,
		"session_uuid": ref.session_uuid,
		"docname": ref.docname,
		"ran_inline": ran_inline,
		"status": final_status,
	}


def _render_session_report(docname: str, *, ai_backfill: bool = False) -> dict:
	"""Re-render the session's HTML report from stored data and re-attach it.

	Not whitelisted and ungated: every caller must already have passed ``_session_action_gate``
	(or run as trusted server code). Other Optimus modules may call it; the leading underscore
	means "not an HTTP endpoint", not "private to this module". ``ai_backfill=True`` first fills
	missing AI fix suggestions when "Suggest AI fixes by default" is on; only the whitelisted
	``regenerate_reports`` passes it until that path is removed, so a re-render never calls the
	LLM. AI endpoints use the default False: they have just generated what they wanted.

	Recordings are best-effort: if they expired from Redis (and no bundle is attached) the
	per-query drill-down renders empty and every persisted section stays intact. Clears the cached
	PDF. Returns ``{"regenerated": True, "recordings_available": int, "actions_total": int}``; a
	render failure raises. Failures are logged after their ``try`` block, never inside the
	``except`` (a log call inside an ``except`` lets Sentry attach the active frame's locals).
	"""
	from optimus import ai_fix
	from optimus import analyze as _analyze_mod

	doc = frappe.get_doc("Optimus Session", docname)
	recording_uuids = [
		a.recording_uuid for a in (doc.actions or []) if getattr(a, "recording_uuid", None)
	]
	fetch_error = None
	interrupt = None
	try:
		recordings = list(_analyze_mod._fetch_recordings(
			recording_uuids, recordings_bundle=_analyze_mod._load_recordings_bundle(doc)
		))
	except ai_fix._job_timeout_types() as e:
		interrupt = (type(e), e.args)
	except Exception as e:
		fetch_error, recordings = e, []
	if interrupt is not None:
		raise interrupt[0](*interrupt[1])
	if fetch_error is not None:
		ai_fix.log_ai_failure("optimus regenerate_reports fetch", fetch_error, session_uuid=doc.session_uuid)

	if ai_backfill:
		backfill_error = None
		try:
			_analyze_mod._backfill_ai_suggestions(doc)
		except ai_fix._job_timeout_types() as e:
			interrupt = (type(e), e.args)
		except Exception as e:
			backfill_error = e
		if interrupt is not None:
			raise interrupt[0](*interrupt[1])
		if backfill_error is not None:
			ai_fix.log_ai_failure("optimus regenerate ai backfill", backfill_error, session_uuid=doc.session_uuid)

	try:
		from optimus import pdf_export

		pdf_export.clear_cached_pdf(doc.session_uuid)
	except ai_fix._job_timeout_types() as e:
		interrupt = (type(e), e.args)
	except Exception:
		pass
	if interrupt is not None:
		raise interrupt[0](*interrupt[1])

	_analyze_mod._render_and_attach_reports(docname, recordings)
	return {
		"regenerated": True,
		"recordings_available": len(recordings),
		"actions_total": len(doc.actions or []),
	}


def _rerender_after_ai(ref: SessionRef) -> bool:
	"""Re-render after an AI endpoint persisted (and committed) its results.

	A render failure must not turn saved, already-billed work into an error response: it is rolled
	back, logged through the AI log chokepoint (after the ``try``, not inside the ``except``) and
	reported as ``regenerated: False`` (the user can click Regenerate Reports). Never calls the
	whitelisted ``regenerate_reports``, so no second gate or rate limit runs after the LLM spend.
	RQ job timeouts still escape as fresh instances and stop the worker job.
	"""
	from optimus import ai_fix

	failure = None
	interrupt = None
	try:
		return bool(_render_session_report(ref.docname).get("regenerated"))
	except ai_fix._job_timeout_types() as e:
		interrupt = (type(e), e.args)
	except Exception as e:
		failure = e
	if interrupt is not None:
		raise interrupt[0](*interrupt[1])
	try:
		frappe.db.rollback()
	except ai_fix._job_timeout_types() as e:
		interrupt = (type(e), e.args)
	except Exception:
		pass
	if interrupt is not None:
		failure = None
		raise interrupt[0](*interrupt[1])
	ai_fix.log_ai_failure("optimus AI re-render", failure, session_uuid=ref.session_uuid)
	return False


@frappe.whitelist(methods=["POST"])
def regenerate_reports(session_uuid: str) -> dict:
	"""Re-render the HTML report from stored session data without re-running any analyzer (fast,
	idempotent). Use it when the template or renderer changed and you want the new UI on an
	already-analyzed session.

	Each call replaces the report File attachment and clears the cached PDF. Allowed on Ready or
	Failed sessions; any other status is refused with the long-standing message that names
	retry_analyze (pinned by the real-bench integration test). Permission:
	``_session_action_gate`` (the owner, a System Manager or a user the session is shared with for
	editing); then the per-user limit. Until the AI path is removed from regenerate it still
	backfills missing AI fix suggestions first when "Suggest AI fixes by default" is on.
	"""
	ref = _session_action_gate(
		session_uuid,
		action="regenerate_reports",
		statuses=("Ready", "Failed"),
		status_hint=_("regenerate_reports requires the session to be in a terminal state (Ready or Failed); this one is '{0}'. Wait for analyze to finish, or use retry_analyze to restart a stuck pipeline."),
	)
	ratelimit.enforce_user_rate_limit("regenerate_reports", **_ACTION_LIMITS["regenerate_reports"])
	out = _render_session_report(ref.docname, ai_backfill=True)
	return {
		"regenerated": bool(out.get("regenerated")),
		"session_uuid": ref.session_uuid,
		"docname": ref.docname,
		"recordings_available": out["recordings_available"],
		"actions_total": out["actions_total"],
	}





@frappe.whitelist(methods=["POST"])
def test_ai_connection() -> dict:
	"""Probe the configured AI provider. System-Manager-only (Optimus Settings itself is
	System-Manager-only) and limited per user. Returns ``{"ok": bool, "message": str,
	"model": str}``; never raises on a provider failure (the detail is in ``message``)."""
	user = _require_profiler_user()
	if "System Manager" not in set(frappe.get_roles(user)) and user != "Administrator":
		frappe.throw(
			_("Only a System Manager can test the AI connection."),
			frappe.PermissionError,
			title=_("Optimus"),
		)
	ratelimit.enforce_user_rate_limit("test_ai_connection", **_AI_LIMITS["test_ai_connection"])
	# The settings probe isn't billable to any session: clear the spend marker so its tokens
	# don't land on whatever session this worker last served.
	from optimus import analyze as _analyze_mod

	_analyze_mod._mark_ai_spend_session(None)
	from optimus import ai_fix

	return ai_fix.test_connection()


@frappe.whitelist()
def ai_capabilities() -> dict:
	"""The per-section LLM toggles, for the Optimus Session form to decide
	which AI buttons to show. Any logged-in profiler user no Profiler
	Settings read permission needed (the server still enforces the toggles).
	Returns ``{enabled, findings, indexes, humanize}`` (all bools)."""
	_require_profiler_user()
	from optimus.settings import get_config
	cfg = get_config()
	return {
		"enabled": bool(getattr(cfg, "ai_enabled", False)),
		"findings": bool(getattr(cfg, "ai_suggest_findings", True)),
		"indexes": bool(getattr(cfg, "ai_suggest_indexes", True)),
		"humanize": bool(getattr(cfg, "ai_humanize_steps", True)),
	}








def _humanize_steps_core(doc, *, title: str | None = None) -> dict:
	"""Validation-free rewrite of the session's "Steps to Reproduce" via the
	configured LLM. Caller is responsible for the permission / status / AI-
	available / toggle gates and for the final re-render. Returns
	``{"updated": bool, "reason": str|None}`` so the composite endpoint can
	report per-step outcomes without raising.
	"""
	from optimus import ai_fix
	from optimus import analyze as _analyze_mod

	_analyze_mod._mark_ai_spend_session(getattr(doc, "session_uuid", None))

	recording_uuids = [
		a.recording_uuid for a in (doc.actions or [])
		if getattr(a, "recording_uuid", None)
	]
	fetch_error = None
	try:
		recordings = list(_analyze_mod._fetch_recordings(
			recording_uuids, recordings_bundle=_analyze_mod._load_recordings_bundle(doc)
		))
	except Exception as e:
		fetch_error = e
		recordings = []
	if fetch_error is not None:
		ai_fix.log_ai_failure(
			"optimus humanize_steps fetch", fetch_error, session_uuid=getattr(doc, "session_uuid", None),
		)

	actions = _analyze_mod._actions_for_humanizer(recordings)
	if not actions:
		return {
			"updated": False,
			"reason": (
				"This session has no user actions to summarise it was all "
				"background / polling traffic (or the recordings have expired)."
			),
		}
	_steps_usage: dict = {}
	try:
		steps_md = ai_fix.humanize_steps(actions, session_title=title, usage_out=_steps_usage)
	except ai_fix.AiFixError as e:
		return {"updated": False, "reason": str(e)}

	frappe.db.set_value(
		"Optimus Session", doc.name, {
			"notes": _analyze_mod._assemble_humanized_notes(steps_md),
			# Tokens for this Steps-to-Reproduce humanization. The report's
			# session total rolls it in alongside fix + index suggestions
			# (notes is markdown, so the count needs its own field).
			"ai_steps_tokens": int(_steps_usage.get("total_tokens") or 0),
		},
	)
	safe_commit()
	return {"updated": True, "reason": None}





def _refill_indexes_for_doc(doc) -> dict:
	"""Walk the session's table breakdown and run the per-table index AI
	helper for every table that has a heuristic ``recommended_index`` but
	no ``ai_index`` yet. Returns ``{"added": N, "failed": N, "skipped": N}``.
	Caller is responsible for permission / status / AI-available gates and
	for the final re-render.
	"""
	import json as _json

	from optimus import analyze as _analyze_mod
	from optimus.ai_fix import log_ai_failure

	try:
		breakdown = _json.loads(doc.table_breakdown_json or "[]")
	except Exception:
		breakdown = []

	eligible = [
		t for t in (breakdown or [])
		if isinstance(t, dict)
		and (t.get("recommended_index") or {}).get("columns")
		and not t.get("ai_index")
	]
	added = failed = skipped = 0
	for t in eligible:
		table_name = t.get("table")
		if not table_name:
			skipped += 1
			continue
		error = None
		try:
			out = _analyze_mod._run_table_index_ai_backfill(doc, table_name=table_name)
		except Exception as e:
			error = e
		if error is not None:
			# One title for every table (the table goes in the message), so the
			# Error Log groups these rows instead of creating one title per table.
			log_ai_failure(
				"optimus refill_indexes", error,
				session_uuid=getattr(doc, "session_uuid", None), table=table_name,
			)
			failed += 1
			continue
		if out.get("ok"):
			added += 1
		else:
			# Helper returned a reason (e.g. provider missing for one call)
			# treat as skipped, not failed, since the doc state is unchanged.
			skipped += 1
	return {"added": added, "failed": failed, "skipped": skipped}


@frappe.whitelist(methods=["POST"])
def refill_ai_suggestions(session_uuid: str) -> dict:
	"""Single-button entry point: re-fills every AI-generated report section in one round-trip:
	(1) overwrite every eligible finding's fix suggestion, (2) rewrite Steps to Reproduce, (3) run
	the per-table index helper for tables with a candidate but no AI advice, (4) one final
	re-render.

	Each step is gated by its per-section toggle; a toggle-off step is skipped, not errored.
	``_ai_session_gate`` runs once at the top (permission, Ready status, AI configured, per-user
	limit).
	"""
	ref = _ai_session_gate(session_uuid, section=None, action="refill_ai_suggestions")

	from optimus import analyze as _analyze_mod
	from optimus.settings import get_config

	cfg = get_config()
	doc = frappe.get_doc("Optimus Session", ref.docname)

	# Count this refresh (cumulative; only ever increases). Portable read-modify-write off
	# the already-loaded doc; update_modified=False so the counter bump doesn't touch `modified`.
	# Refresh is user-initiated and rate-limited, so the non-atomic increment is acceptable
	# (PR-2 makes it atomic).
	frappe.db.set_value(
		"Optimus Session", doc.name, "ai_refresh_count",
		(getattr(doc, "ai_refresh_count", 0) or 0) + 1,
		update_modified=False,
	)

	fixes = {"added": 0, "failed": 0, "skipped_time": 0, "skipped": None}
	if cfg.ai_suggest_findings:
		counts = _analyze_mod._run_ai_backfill(doc, cap=0, regenerate_all=True)
		fixes = {
			"added": counts.get("added", 0),
			"failed": counts.get("failed", 0),
			"skipped_time": counts.get("skipped_time", 0),
			"skipped": None,
		}
	else:
		fixes["skipped"] = "toggle_off"

	steps = {"updated": False, "reason": None}
	if cfg.ai_humanize_steps:
		# Re-fetch the doc: the backfill above may have mutated rows.
		doc = frappe.get_doc("Optimus Session", ref.docname)
		steps = _humanize_steps_core(doc, title=ref.title or None)
	else:
		steps["reason"] = "toggle_off"

	indexes = {"added": 0, "failed": 0, "skipped": 0, "skipped_reason": None}
	if cfg.ai_suggest_indexes:
		doc = frappe.get_doc("Optimus Session", ref.docname)
		indexes = _refill_indexes_for_doc(doc)
		indexes["skipped_reason"] = None
	else:
		indexes["skipped_reason"] = "toggle_off"

	return {
		"ok": True,
		"session_uuid": ref.session_uuid,
		"fixes": fixes,
		"steps": steps,
		"indexes": indexes,
		"regenerated": _rerender_after_ai(ref),
	}


@frappe.whitelist()
def get_installed_apps_for_tracking() -> list[str]:
	"""Return the bench's installed apps for the Optimus Settings ▸ Tracked
	Apps autocomplete, excluding ``optimus`` itself. System-Manager-only.
	"""
	if "System Manager" not in (frappe.get_roles() or []):
		frappe.throw(
			_("Only System Manager can list installed apps for the "
			"Optimus Settings picker.")
		)
	apps = frappe.get_installed_apps() or []
	return [app for app in apps if app != "optimus"]


@frappe.whitelist()
def get_config_profiles() -> dict:
	"""Return the named Sensitivity Profile presets (from
	``optimus.settings._PROFILES``) for the Optimus Settings form's
	``config_profile`` handler to fill threshold fields. System-Manager-only.
	"""
	if "System Manager" not in (frappe.get_roles() or []):
		frappe.throw(
			_("Only System Manager can read the Optimus sensitivity profiles.")
		)
	from optimus import settings
	# Plain dict of dicts JSON-serializable as-is for the whitelist response.
	return {name: dict(values) for name, values in settings._PROFILES.items()}


# ---------------------------------------------------------------------------
# Phase-2 line profiler API
# ---------------------------------------------------------------------------
# Three whitelisted endpoints that the Optimus Session form's "Phase 2:
# Line Profile" section calls into:
#
#   get_phase2_candidates(session_uuid) populate the curated picker
#   start_line_profile_pass(session_uuid, picks) begin a phase-2 run
#   stop_line_profile_pass(run_uuid) end the run, enqueue analyze
#
# Phase-2 implementation lives in optimus.line_profile.*
# this surface is the thin transport layer.


def _picker_empty_hint(action_count, with_tree, parsed_ok, candidate_count, ignored_apps_filtered=0):
	"""Map the picker's empty-state counter triple to a human-readable reason,
	shown in the dialog when the curated list is empty so the operator can
	self-diagnose."""
	if action_count == 0:
		return (
			"This session has no recorded actions. Nothing to "
			"line-profile - capture a session that exercises the "
			"slow flow first."
		)
	if with_tree == 0:
		return (
			"None of this session's actions carry a captured call "
			"tree. Most common cause: this session was analyzed "
			"before the Sprint-1 HMAC fix loaded. Run ``bench "
			"restart`` and capture a new session. Existing Ready "
			"sessions can be repaired only by re-running analyze."
		)
	if parsed_ok == 0:
		return (
			"Actions carry call trees but none parsed as valid "
			"JSON. Check the bench log for 'Failed to deserialize "
			"pyi tree' or 'optimus analyze' error entries."
		)
	if candidate_count and ignored_apps_filtered >= candidate_count:
		# Built, but all dropped as Ignored Apps.
		return (
			f"Call trees loaded, but all {ignored_apps_filtered} candidate "
			"frame(s) belong to apps on your Ignored Apps list, so none are "
			"offered here. Remove that app from Optimus Settings › Ignored "
			"Apps (keep at least one entry clearing the list restores the "
			"framework-app defaults), or type the function you want below."
		)
	if candidate_count == 0:
		return (
			"Call trees loaded but every frame was filtered out as "
			"framework plumbing. The session may be too short or "
			"hit only framework code. Use the textbox below to type "
			"the function you want."
		)
	return ""


@frappe.whitelist()
def get_phase2_candidates(session_uuid: str) -> dict:
	"""Return the curated candidate list for the phase-2 picker UI.

	Parses each action's ``call_tree_json`` and builds a top-30 list of
	user-app frames (framework apps surfaced separately as "observations").
	The response also carries a ``diagnostic`` dict the dialog renders as a
	callout when both lists are empty, explaining why.
	"""
	import json as _json

	from optimus.line_profile import capture as _lp_capture
	from optimus.line_profile import picker as _lp_picker

	_require_profiler_user()
	# SECURITY: ownership gate a phase-2 read exposes the session's captured
	# call-tree internals (function names, file paths). Without this, any Optimus
	# User holding another user's session_uuid could read it.
	parent_docname = _require_session_permission(session_uuid, "read")

	doc = frappe.get_doc("Optimus Session", parent_docname)

	trees = []
	for action in (doc.actions or []):
		raw = action.call_tree_json
		if not raw:
			continue
		try:
			tree = _json.loads(raw)
		except (TypeError, ValueError):
			continue
		# pyinstrument trees are stored either as the full session shape
		# (``{root: {...}}``) or just the root node.
		if isinstance(tree, dict) and "root" in tree:
			tree = tree["root"]
		trees.append(tree)

	# Phase K v0.7 GA: tree-indented DFS picker - parents land in the
	# list before their children, each row tagged with ``depth`` so
	# the JS dialog can render hierarchy via indented labels.
	candidates = _lp_picker._build_tree_indented_candidates(trees)
	# Pre-filter count for the diagnostic/hint captured before the ignored
	# filter reassigns ``candidates`` below.
	raw_candidate_count = len(candidates)

	# Drop candidates whose app is on the Ignored Apps list the report
	# already excludes those apps, so offering them here would be inconsistent.
	try:
		from optimus.settings import get_ignored_apps
		_ignored_apps = get_ignored_apps()
	except Exception:
		_ignored_apps = ()
	candidates, ignored_apps_filtered = _lp_picker.filter_out_ignored_apps(
		candidates, _ignored_apps
	)

	# v0.6.0 Round 6: surface the configured auto-expand default so the
	# picker dialog ticks/un-ticks its checkbox per Optimus Settings.
	try:
		from optimus.settings import get_config
		default_auto_expand = bool(get_config().phase2_default_auto_expand)
	except Exception:
		default_auto_expand = True

	action_count = len(doc.actions or [])
	actions_with_tree = sum(
		1 for a in (doc.actions or []) if a.call_tree_json
	)
	trees_parsed_ok = len(trees)

	return {
		"session_uuid": session_uuid,
		"docname": parent_docname,
		"candidates": [c for c in candidates if not c["is_framework"]],
		"observations": [c for c in candidates if c["is_framework"]],
		"line_profiler_available": _lp_capture.is_line_profiler_available(),
		"default_auto_expand": default_auto_expand,
		"diagnostic": {
			"action_count": action_count,
			"actions_with_call_tree_json": actions_with_tree,
			"trees_parsed_ok": trees_parsed_ok,
			"raw_candidates_before_filter": raw_candidate_count,
			"ignored_apps_filtered": ignored_apps_filtered,
			"hint": _picker_empty_hint(
				action_count,
				actions_with_tree,
				trees_parsed_ok,
				raw_candidate_count,
				ignored_apps_filtered=ignored_apps_filtered,
			),
		},
	}


@frappe.whitelist(methods=["POST"])
def start_line_profile_pass(session_uuid: str, picks: str | list, auto_expand: bool = True) -> dict:
	"""Begin a phase-2 line-profile run on a finished session.

	``picks`` is a JSON-encoded (or parsed) list of ``{dotted_path, source}``
	entries. When ``auto_expand`` is true (default), each curated pick is
	walked down phase-1's call tree (``picker.expand_hot_chain``) so the run
	instruments the full hot chain; free-form picks pass through unchanged.

	Gated by ``_session_action_gate`` (Ready session; owner, System Manager or write-sharee). The
	run instruments the caller's own requests, so ``user`` is the caller.
	"""
	import json as _json
	import uuid as _uuid

	from optimus.line_profile import capture as _lp_capture
	from optimus.line_profile import picker as _lp_picker

	ref = _session_action_gate(session_uuid, action="start_line_profile_pass")
	user = frappe.session.user
	parent_docname = ref.docname

	# The picks arg often arrives as a string from JS accept both shapes.
	if isinstance(picks, str):
		try:
			picks_list = _json.loads(picks)
		except _json.JSONDecodeError:
			frappe.throw(_("picks must be a JSON list of {dotted_path, source} entries."), frappe.ValidationError, title=_("Optimus"))
	else:
		picks_list = picks
	if not isinstance(picks_list, list) or not picks_list:
		frappe.throw(_("Provide at least one function to line-profile."), frappe.ValidationError, title=_("Optimus"))

	# Coerce auto_expand from the JS payload (frappe.call sends "true"/"false"
	# strings; whitelisted view fns accept Python types when available).
	if isinstance(auto_expand, str):
		auto_expand = auto_expand.lower() in ("true", "1", "yes")
	auto_expand = bool(auto_expand)

	# Phase-1 must not be active for the same user phase 1 and phase 2
	# read separate Redis flags but only one can be active at a time.
	if session.get_active_session_for(user):
		frappe.throw(
			_("You currently have a phase-1 session recording. Stop it before starting a phase-2 line-profile run."),
			frappe.ValidationError,
			title=_("Optimus"),
		)


	# Reject if the user already has a phase-2 run in flight elsewhere.
	if _lp_capture.is_active(user):
		frappe.throw(_("You already have a phase-2 line-profile run active."), frappe.ValidationError, title=_("Optimus"))

	# Auto-expand curated picks via phase-1's call tree. Curated picks come
	# in as {dotted_path, source: "curated"}; the expansion adds their hot
	# user-code descendants up to the framework boundary. Free-form picks
	# (source != "curated") aren't expanded we don't know if they appeared
	# in phase 1 at all.
	expansions: list[dict] = []
	if auto_expand:
		# Load the phase-1 call trees once so expand_hot_chain can search
		# across all action recordings for the hottest match.
		parent_doc = frappe.get_doc("Optimus Session", parent_docname)
		trees: list[dict] = []
		for action in (parent_doc.actions or []):
			raw = action.call_tree_json
			if not raw:
				continue
			try:
				tree = _json.loads(raw)
			except (TypeError, ValueError):
				continue
			if isinstance(tree, dict) and "root" in tree:
				tree = tree["root"]
			trees.append(tree)

		seen: set[str] = set()
		# v0.6.0 Round 6: auto-expand depth + min-ms thresholds now read
		# from Optimus Settings (cached) rather than baked into the
		# helper's defaults. Resolved once outside the loop.
		from optimus.settings import get_config as _get_config
		try:
			_cfg = _get_config()
			# v0.13.x: 0 is the "unlimited" sentinel (depth = walk to
			# leaves; min_ms = no minimum to follow). The ``or DEFAULT``
			# fallback only fires when the cfg attr is genuinely missing
			# (legacy pickled OptimusConfig from a pre-v0.13.x bench), so
			# read with a sentinel and check explicitly.
			_max_depth = getattr(_cfg, "auto_expand_max_depth", None)
			_max_depth = int(_max_depth) if _max_depth is not None else 10
			_min_ms = getattr(_cfg, "auto_expand_min_ms", None)
			_min_ms = float(_min_ms) if _min_ms is not None else 50.0
		except Exception:
			_max_depth, _min_ms = 10, 50.0

		expanded_picks: list[dict] = []
		for entry in picks_list:
			dotted = entry.get("dotted_path") or ""
			source = entry.get("source", "freeform")
			# Free-form picks pass through unchanged; we keep them as-is so
			# the resolver can still flag import errors inline.
			if source != "curated":
				if dotted and dotted not in seen:
					seen.add(dotted)
					expanded_picks.append(entry)
				continue
			chain = _lp_picker.expand_hot_chain(
				trees, dotted, max_depth=_max_depth, min_ms=_min_ms,
			)
			if not chain:
				# Picked function wasn't in any phase-1 call tree (rare
				# the picker UI sources from those same trees). Pass it
				# through so the resolver can still attempt it.
				if dotted and dotted not in seen:
					seen.add(dotted)
					expanded_picks.append(entry)
				continue
			# Track that the chain came from this curated pick so the form
			# can show "instrumented N functions: validate → ... → ..."
			expansions.append({
				"original": dotted,
				"chain": [c["dotted_path"] for c in chain],
			})
			for chain_entry in chain:
				cdp = chain_entry["dotted_path"]
				if cdp and cdp not in seen:
					seen.add(cdp)
					expanded_picks.append({
						"dotted_path": cdp,
						"source": "curated" if chain_entry["depth"] == 0 else "auto_expand",
					})
		picks_list = expanded_picks
		if not picks_list:
			frappe.throw(_("Provide at least one function to line-profile."), frappe.ValidationError, title=_("Optimus"))

	run_uuid = _uuid.uuid4().hex

	# Resolve picks + persist Redis state. Raises CaptureError if no pick
	# is eligible.
	try:
		resolved = _lp_capture.start_line_profile_pass(
			session_uuid=session_uuid,
			run_uuid=run_uuid,
			user=user,
			picks=picks_list,
		)
	except _lp_capture.CaptureError as exc:
		frappe.throw(str(exc), frappe.ValidationError, title=_("Optimus"))

	# Append the Phase 2 Run row in Recording status.
	parent = frappe.get_doc("Optimus Session", parent_docname)
	parent.append("phase_2_runs", {
		"run_uuid": run_uuid,
		"status": "Recording",
		"started_at": now_datetime(),
		"picks_json": frappe.as_json([
			{"dotted_path": r["dotted_path"], "source": r.get("source", "freeform")}
			for r in resolved if r.get("eligible")
		]),
	})
	_save_parent_bypassing_perms(parent)

	frappe.publish_realtime("phase_2_run_recording", {
		"session_uuid": session_uuid,
		"run_uuid": run_uuid,
	}, user=user)

	return {
		"run_uuid": run_uuid,
		"session_uuid": session_uuid,
		"docname": parent_docname,
		"resolved_picks": resolved,
		"expansions": expansions,
		"auto_expanded": bool(auto_expand and expansions),
	}


@frappe.whitelist(methods=["POST"])
def force_stop_phase2() -> dict:
	"""Recovery endpoint: clears the calling user's phase-2 active flag and
	marks any of their in-flight Phase 2 Run rows as Failed.

	Idempotent safe to call when nothing is stuck. Use this when the
	form rejects ``start_line_profile_pass`` with "phase-2 already
	active" and the previous run never reached Stop (worker crash, tab
	close, or interrupted reproduction).
	"""
	from optimus.line_profile import capture as _lp_capture

	user = _require_profiler_user()

	cleared_run = _lp_capture.is_active(user)
	# Always clear the flag, even if is_active returned None (defensive
	# against stale frappe.local caches mid-test or after worker recycle).
	_lp_capture.stop_line_profile_pass(cleared_run or "_unknown_", user)

	# Mark any Recording rows the user owns as Failed so the form's child
	# table reflects the recovery. We scope to rows where parent.user ==
	# the calling user so a System Manager hitting this doesn't sweep
	# other users' active runs.
	# Portable, no join: the user's session names first, then their Recording
	# Phase-2 Run child rows (parent == session name). Works on MariaDB + PG.
	session_names = frappe.get_all(
		"Optimus Session", filters={"user": user}, pluck="name"
	)
	stuck_rows = (
		frappe.get_all(
			"Optimus Phase Two Run",
			filters={"status": "Recording", "parent": ["in", session_names]},
			fields=["name", "parent", "run_uuid"],
		)
		if session_names
		else []
	)

	# v0.6.x: group stuck rows by their parent Optimus Session so each
	# parent doc is loaded + saved EXACTLY ONCE per batch (was N loads + N
	# saves when one session held multiple stuck runs the common case
	# for a user spamming the picker).
	rows_by_parent: dict[str, list[dict]] = {}
	for row in stuck_rows:
		rows_by_parent.setdefault(row["parent"], []).append(row)

	failed = 0
	for parent_name, rows in rows_by_parent.items():
		try:
			parent = frappe.get_doc("Optimus Session", parent_name)
			matched_in_parent = 0
			wanted_uuids = {r["run_uuid"] for r in rows}
			for child in (parent.phase_2_runs or []):
				if child.run_uuid in wanted_uuids:
					child.status = "Failed"
					child.warnings_json = frappe.as_json([
						"Force-stopped by user via api.force_stop_phase2.",
					])
					child.ended_at = now_datetime()
					try:
						_lp_capture.cleanup_run(child.run_uuid)
					except Exception:
						frappe.log_error(
							title="force_stop_phase2 redis cleanup",
							message=f"{parent_name}/{child.run_uuid}",
						)
					matched_in_parent += 1
			if matched_in_parent:
				parent.flags.ignore_validate_update_after_submit = True
				parent.save(ignore_permissions=True)
				failed += matched_in_parent
		except Exception as exc:
			frappe.log_error(
				title="force_stop_phase2 parent save",
				message=f"{parent_name}: {exc}",
			)
	safe_commit()

	return {
		"cleared_active_flag": bool(cleared_run),
		"prior_run_uuid": cleared_run,
		"rows_marked_failed": failed,
	}


@frappe.whitelist(methods=["POST"])
def stop_line_profile_pass(run_uuid: str) -> dict:
	"""End a phase-2 run, mark it Analyzing, enqueue the analyzer. Gated by ``_phase2_run_gate``:
	the caller must be allowed to act on the run's parent session and the run must be Recording."""
	from optimus.line_profile import capture as _lp_capture

	ref, _run = _phase2_run_gate(run_uuid, action="stop_line_profile_pass", run_statuses=("Recording",))
	user = frappe.session.user
	session_uuid = ref.session_uuid
	parent_docname = ref.docname

	# Clear the active flag (capture won't instrument further requests).
	_lp_capture.stop_line_profile_pass(run_uuid, user)

	# Mark run Analyzing.
	parent = frappe.get_doc("Optimus Session", parent_docname)
	for child in (parent.phase_2_runs or []):
		if child.run_uuid == run_uuid:
			child.status = "Analyzing"
			child.ended_at = now_datetime()
			break
	_save_parent_bypassing_perms(parent)

	frappe.publish_realtime("phase_2_run_analyzing", {
		"session_uuid": session_uuid,
		"run_uuid": run_uuid,
	}, user=user)

	# When the scheduler is disabled (e.g. dev sites without
	# `bench start`), no RQ worker will pick up the long-queue job and
	# the run gets stuck in Analyzing. Mirror api.stop's inline fallback:
	# run the analyzer in-process so the request completes with results.
	from frappe.utils.scheduler import is_scheduler_disabled

	run_inline = False
	try:
		run_inline = bool(is_scheduler_disabled())
	except Exception:
		frappe.log_error(title="optimus phase-2 scheduler check")

	if run_inline:
		frappe.logger().warning(
			f"optimus: scheduler disabled; running phase-2 "
			f"analyze inline for run {run_uuid}. Caller will block."
		)
		from optimus.line_profile import analyzer as _lp_analyzer

		try:
			_lp_analyzer.run_analyze(session_uuid, run_uuid)
		except Exception as exc:
			# run_analyze marks the run Failed itself; surface the error
			# in the API response so the caller isn't silently puzzled.
			return {
				"run_uuid": run_uuid,
				"session_uuid": session_uuid,
				"status": "Failed",
				"error": str(exc),
				"ran_inline": True,
			}
		return {
			"run_uuid": run_uuid,
			"session_uuid": session_uuid,
			"status": "Ready",
			"ran_inline": True,
		}

	# Otherwise enqueue normally.
	frappe.enqueue(
		"optimus.line_profile.analyzer.run_analyze",
		queue="long",
		timeout=25 * 60,
		session_uuid=session_uuid,
		run_uuid=run_uuid,
	)

	return {
		"run_uuid": run_uuid,
		"session_uuid": session_uuid,
		"status": "Analyzing",
	}


@frappe.whitelist(methods=["POST"])
def retry_phase2_analyze(run_uuid: str) -> dict:
	"""Re-trigger run_analyze for a Phase 2 Run row stuck in Analyzing or Failed. Useful when the
	original RQ enqueue never landed (no worker) or the analyzer crashed.

	Resets the row to Analyzing, then runs inline so the response carries the final status (Ready
	or Failed). Gated by ``_phase2_run_gate`` on the run's parent session.
	"""
	from optimus.line_profile import analyzer as _lp_analyzer

	ref, _run = _phase2_run_gate(run_uuid, action="retry_phase2_analyze")
	session_uuid = ref.session_uuid
	parent_docname = ref.docname

	# Reset to Analyzing so the realtime event flow still makes sense.
	parent = frappe.get_doc("Optimus Session", parent_docname)
	for child in (parent.phase_2_runs or []):
		if child.run_uuid == run_uuid:
			child.status = "Analyzing"
			break
	_save_parent_bypassing_perms(parent)

	try:
		_lp_analyzer.run_analyze(session_uuid, run_uuid)
	except Exception as exc:
		return {
			"run_uuid": run_uuid,
			"session_uuid": session_uuid,
			"status": "Failed",
			"error": str(exc),
		}
	return {
		"run_uuid": run_uuid,
		"session_uuid": session_uuid,
		"status": "Ready",
	}


@frappe.whitelist(methods=["POST"])
def retry_phase2_analyzes_batch(run_uuids: str | list) -> dict:
	"""Batch variant of ``retry_phase2_analyze``: retries a list of
	``run_uuid``s in a single server round-trip instead of N client calls.

	Per-run failures are isolated (one bad retry doesn't abort the rest).
	The response carries a per-run status list plus an aggregate tally."""
	import json as _json

	_require_profiler_user()

	# Accept JSON-encoded list (Frappe's whitelisted-API arg marshalling
	# stringifies lists when they cross the request boundary) OR a real
	# Python list when called from another server-side helper.
	if isinstance(run_uuids, str):
		try:
			run_uuids = _json.loads(run_uuids)
		except (TypeError, ValueError):
			frappe.throw(_("run_uuids must be a JSON array of run-uuid strings."), frappe.ValidationError, title=_("Optimus"))
	if not isinstance(run_uuids, (list, tuple)) or not run_uuids:
		frappe.throw(_("run_uuids must be a non-empty list of run-uuid strings."), frappe.ValidationError, title=_("Optimus"))

	results: list[dict] = []
	for run_uuid in run_uuids:
		if not isinstance(run_uuid, str) or not run_uuid.strip():
			results.append({"run_uuid": run_uuid, "status": "Skipped",
			                "error": "empty / non-string run_uuid"})
			continue
		try:
			results.append(retry_phase2_analyze(run_uuid))
		except Exception as exc:
			# Don't let one bad row abort the rest of the batch.
			results.append({
				"run_uuid": run_uuid,
				"status": "Failed",
				"error": str(exc),
			})

	# Quick aggregate for the UI to render a single message.
	tallies = {"Ready": 0, "Failed": 0, "Analyzing": 0, "Skipped": 0}
	for r in results:
		st = r.get("status") or "Failed"
		tallies[st] = tallies.get(st, 0) + 1

	return {
		"count": len(results),
		"tallies": tallies,
		"results": results,
	}
