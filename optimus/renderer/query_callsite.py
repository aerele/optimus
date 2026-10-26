# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""File:line and function name for the frame that issued a query."""

from optimus.analyzers.base import walk_callsite

# The name is emitted twice (cell and title), so cap it.
MAX_FUNCTION_CHARS = 200


def query_callsite(call: dict | None) -> dict[str, str]:
	"""Return {"callsite": "file:line", "function": "name"}, "" when unknown."""
	frame = _safe_frame(call)
	if not frame:
		return {"callsite": "", "function": ""}
	name = frame.get("function")
	return {
		"callsite": f"{frame.get('filename', '')}:{frame.get('lineno', '')}",
		"function": name.strip()[:MAX_FUNCTION_CHARS] if isinstance(name, str) else "",
	}


def _safe_frame(call: dict | None) -> dict | None:
	"""Return the callsite frame, or None for a missing or malformed stack."""
	if not isinstance(call, dict):
		return None
	try:
		return walk_callsite(call.get("stack"))
	except (AttributeError, TypeError):
		return None
