"""Counts-only refresh views and fixed user-facing admission explanations.

No journal writes, provider calls or queue access belong in this module.
"""

from datetime import datetime, timezone

from frappe import _

from optimus import ai_refresh_store as store


def public_state(run, *, now):
	"""A counts-only view; SQL remains readable during a Redis outage.

	An active run whose lease or deadline has passed shows as it will be recorded:
	``interrupted``, with ``stalled`` True and the expiry as its ``end_reason``, before any
	recovery has written it. The session's next admission takes it over."""

	def count(key):
		value = run.get(key) or 0
		return value if type(value) is int and value >= 0 else 0

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


def refresh_plan(doc, *, now):
	"""Count the same eligible rows the worker selects, without source or file I/O."""
	from optimus import analyze
	from optimus.settings import get_config

	cfg = get_config()
	fixes = bool(cfg.ai_enabled and cfg.ai_suggest_findings)
	cutoff = datetime.fromtimestamp(now, timezone.utc)
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


def admission_message(reason, *, queue=None):
	"""Fixed translated explanations, never provider text or exception detail."""
	if reason == "no_worker":
		return _("No background worker is listening on the AI queue {0}.").format(queue)
	return {
		"queue_unavailable": _("The background queue is unavailable. Try again after it recovers."),
		"busy": _("The session is busy. Wait a moment and try again."),
		"not_running": _("This refresh has already ended. Reload its current progress."),
		"phase2": _("Wait for the active Phase 2 run to finish."),
		"disabled": _("AI suggestions are disabled or the provider is not configured."),
		"not_ready": _("The session must be Ready before AI suggestions can run."),
		"permission": _("The requesting user can no longer update this session."),
		"site_cap": _("The site already has the maximum number of active AI refreshes."),
		"user_cap": _("You already have the maximum number of active AI refreshes."),
		"retry_limit": _("This refresh has reached its resume limit. Start a new refresh to try again."),
		"already_resumed": _("This run has already been resumed. Reload its current progress."),
		"not_resumable": _("This run cannot be resumed."),
	}.get(reason, _("AI enrichment could not start. Search the Error Log for titles starting with optimus ai."))
