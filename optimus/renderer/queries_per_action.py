# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Rows for the "Queries per action" section, scoped by Tracked Apps."""

from __future__ import annotations

from optimus.analyzers.base import (
	is_framework_callsite_str,
	is_profiler_own_query,
	walk_callsite_str,
)

PER_ACTION_TOP = 20
SECTION_ROW_LIMIT = 40


def build_queries_per_action(actions, recordings_by_uuid, tracked_apps=()) -> dict:
	"""Return the slowest queries across actions plus the count hidden by Tracked Apps."""
	rows: list[dict] = []
	hidden = 0
	for action in actions or []:
		recording = (recordings_by_uuid or {}).get(_get(action, "recording_uuid"))
		if not recording:
			continue
		kept, untracked = _visible_calls(recording.get("calls"), tracked_apps)
		hidden += untracked
		for call, callsite in sorted(kept, key=_duration, reverse=True)[:PER_ACTION_TOP]:
			rows.append(_row(_get(action, "action_label"), call, callsite))
	rows.sort(key=lambda r: r["duration"], reverse=True)
	return {"rows": rows[:SECTION_ROW_LIMIT], "total": len(rows), "hidden_untracked": hidden}


def _visible_calls(calls, tracked_apps) -> tuple[list, int]:
	"""Return ((call, callsite) pairs to show, count hidden as not in Tracked Apps)."""
	kept: list = []
	untracked = 0
	for call in calls or []:
		if not isinstance(call, dict):
			continue
		stack = call.get("stack")
		if is_profiler_own_query(stack):
			continue
		callsite = walk_callsite_str(stack) or _innermost(stack)
		if tracked_apps and is_framework_callsite_str(callsite, tracked_apps):
			untracked += 1
			continue
		kept.append((call, callsite))
	return kept, untracked


def _row(label, call, callsite) -> dict:
	return {
		"action": label,
		"duration": call.get("duration") or 0,
		"copies": call.get("normalized_copies") or 0,
		"callsite": callsite or "",
	}


def _duration(pair) -> float:
	return pair[0].get("duration") or 0


def _innermost(stack) -> str:
	frame = stack[-1] if stack and isinstance(stack[-1], dict) else None
	return f"{frame.get('filename')}:{frame.get('lineno')}" if frame else ""


def _get(obj, key):
	return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
