# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Rows for the "Queries per action" section, scoped by Tracked Apps."""

from __future__ import annotations

from optimus.analyzers.base import (
	is_framework_callsite_str,
	is_profiler_own_query,
	walk_callsite,
)

PER_ACTION_TOP = 20
SECTION_ROW_LIMIT = 40
# The name is emitted twice (cell and title), so cap it.
MAX_FUNCTION_CHARS = 200


def build_queries_per_action(actions, recordings_by_uuid, tracked_apps=()) -> dict:
	"""Return the slowest queries across actions plus the count hidden by Tracked Apps."""
	rows: list[dict] = []
	hidden = 0
	total = 0
	for action in actions or []:
		recording = (recordings_by_uuid or {}).get(_get(action, "recording_uuid"))
		if not recording:
			continue
		kept, untracked = _visible_calls(recording.get("calls"), tracked_apps)
		hidden += untracked
		total += len(kept)
		for call, callsite in sorted(kept, key=_duration, reverse=True)[:PER_ACTION_TOP]:
			rows.append(_row(_get(action, "action_label"), call, callsite))
	rows.sort(key=lambda r: r["duration"], reverse=True)
	return {"rows": rows[:SECTION_ROW_LIMIT], "total": total, "hidden_untracked": hidden}


def _visible_calls(calls, tracked_apps) -> tuple[list, int]:
	"""Return ((call, callsite) pairs to show, count hidden as not in Tracked Apps)."""
	kept: list = []
	untracked = 0
	for call in calls or []:
		if not isinstance(call, dict):
			continue
		stack = call.get("stack")
		if _is_own_query(stack):
			continue
		callsite, function = _callsite_and_function(stack)
		if tracked_apps and is_framework_callsite_str(callsite, tracked_apps):
			untracked += 1
			continue
		kept.append((call, (callsite, function)))
	return kept, untracked


def _row(label, call, site) -> dict:
	callsite, function = site
	return {
		"action": label,
		"duration": call.get("duration") or 0,
		"copies": call.get("normalized_copies") or 0,
		"callsite": callsite or "",
		"function": function,
	}


def _duration(pair) -> float:
	return pair[0].get("duration") or 0


def _callsite_and_function(stack) -> tuple[str, str]:
	"""Return ("file:line", function name) from the one frame the callsite names."""
	frame = _safe_walk(stack) or _innermost(stack)
	if not frame:
		return "", ""
	name = frame.get("function")
	return (
		f"{frame.get('filename', '?')}:{frame.get('lineno', '?')}",
		name.strip()[:MAX_FUNCTION_CHARS] if isinstance(name, str) else "",
	)


def _is_own_query(stack) -> bool:
	"""True for Optimus's own query; a malformed stack counts as not its own."""
	try:
		return is_profiler_own_query(stack)
	except (AttributeError, TypeError):
		return False


def _safe_walk(stack) -> dict | None:
	"""Return the callsite frame, or None for a missing or malformed stack."""
	try:
		return walk_callsite(stack)
	except (AttributeError, TypeError):
		return None


def _innermost(stack) -> dict | None:
	frame = stack[-1] if isinstance(stack, list) and stack else None
	return frame if isinstance(frame, dict) else None


def _get(obj, key):
	return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
