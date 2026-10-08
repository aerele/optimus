# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""AI eligibility helpers that still live here until Task 4 moves them (no site access or I/O).

- ``hot_line_gate``: a note explaining why a Hot Line gets no AI call.
- ``analyzed_before_callsite_fix``: Redundant Call findings built by the older walk.
"""

from __future__ import annotations

import ast
import json
import re

from optimus.analyzers.base import is_framework_callsite


def _detail(finding: dict) -> dict:
	detail = finding.get("technical_detail")
	if isinstance(detail, dict):
		return detail
	try:
		parsed = json.loads(finding.get("technical_detail_json") or "{}")
	except (TypeError, ValueError):
		return {}
	return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------------------
# Hot Line gate
# ---------------------------------------------------------------------------

_CHEAP_CALLS: frozenset[str] = frozenset({
	# Python builtins that cost nothing worth profiling on their own
	"abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "float", "format",
	"frozenset", "getattr", "hasattr", "hash", "int", "isinstance", "issubclass", "iter", "len",
	"list", "map", "max", "min", "next", "print", "range", "repr", "reversed", "round", "set",
	"setattr", "slice", "sorted", "str", "sum", "tuple", "type", "zip",
	# frappe.utils converters, frappe._ and frappe._dict
	"_", "_dict", "cint", "cstr", "flt", "sbool", "getdate", "get_datetime", "nowdate", "now",
	"today", "add_days", "add_months", "date_diff", "rounded",
	# str / list / dict / set methods
	"append", "extend", "get", "items", "keys", "values", "strip", "lstrip", "rstrip", "split",
	"join", "lower", "upper", "startswith", "endswith", "replace", "add", "update", "pop",
	"setdefault", "copy", "count", "index", "insert", "remove", "sort", "discard",
})
_NO_CALL_OPENERS = re.compile(r"^(else|finally|try|except)\b")


def _strip_comment(line: str) -> str:
	in_str: str | None = None
	for i, ch in enumerate(line):
		if in_str:
			if ch == in_str and line[i - 1 : i] != "\\":
				in_str = None
			continue
		if ch in ("'", '"'):
			in_str = ch
		elif ch == "#":
			return line[:i]
	return line


def _call_name(func: ast.AST) -> str | None:
	"""Render a call target: ``frappe.db.get_value``, ``self.run``,
	``super().validate``. None for shapes that are not a name chain."""
	parts: list[str] = []
	node = func
	while isinstance(node, ast.Attribute):
		parts.append(node.attr)
		node = node.value
	if isinstance(node, ast.Name):
		parts.append(node.id)
	elif isinstance(node, ast.Call):
		inner = _call_name(node.func)
		if not inner:
			return None
		parts.append(inner + "()")
	else:
		return None
	return ".".join(reversed(parts))


def _parse_line(line: str) -> ast.AST | None:
	src = _strip_comment(line).strip()
	if not src or _NO_CALL_OPENERS.match(src):
		return None
	if src.startswith("elif "):
		src = src[2:]
	candidates = [src] + ([src + " pass"] if src.endswith(":") else [])
	for candidate in candidates:
		try:
			return ast.parse(candidate)
		except SyntaxError:
			continue
	return None


def _first_callee(line: str) -> str | None:
	"""The outermost call on ``line`` that is not a cheap builtin, converter or
	container method, or None."""
	tree = _parse_line(line)
	if tree is None:
		return None
	for node in ast.walk(tree):
		if isinstance(node, ast.Call):
			name = _call_name(node.func)
			if name and name.rsplit(".", 1)[-1] not in _CHEAP_CALLS:
				return name
	return None


def _short_path(filename: str) -> str:
	norm = filename.replace("\\", "/")
	if "/apps/" in norm:
		return norm.rsplit("/apps/", 1)[1]
	if norm.startswith("apps/"):
		return norm[len("apps/"):]
	return norm.rsplit("/", 1)[-1]


def hot_line_gate(
	finding: dict,
	*,
	tracked_apps: tuple[str, ...] = (),
	installed_apps: frozenset[str] | None = None,
) -> str | None:
	"""Why a Hot Line must not go to the AI, as a report-ready note, or None.

	Gated when the line sits in framework or library code (the report's own
	classification, ``is_framework_callsite``) or when its time is spent inside
	a function it calls (Phase 1 named the hot callee, or the line calls
	something other than a cheap builtin, converter or container method)."""
	if (finding.get("finding_type") or "") != "Hot Line":
		return None
	detail = _detail(finding)
	callsite = detail.get("callsite") if isinstance(detail.get("callsite"), dict) else {}
	filename = str(callsite.get("filename") or detail.get("file") or "")
	if filename and is_framework_callsite(filename, tracked_apps or None, installed_apps):
		return (
			f"This line is in framework or library code ({_short_path(filename)}), which your app "
			"cannot change, so Optimus does not ask the AI about it. Follow the call chain above to "
			"the first line in your own app and reduce how often that path runs."
		)
	hint = detail.get("phase1_hint") if isinstance(detail.get("phase1_hint"), dict) else {}
	callee = str(hint.get("next_hot_callee") or "") or _first_callee(str(detail.get("line_content") or ""))
	if callee:
		return (
			f"Most of this line's time is spent inside {callee}, which the line calls, so Optimus "
			"does not ask the AI about the line itself. Re-run Phase 2 with that function picked to "
			"see which of its lines is slow, or reduce how often this line calls it."
		)
	return None


# ---------------------------------------------------------------------------
# Redundant Call findings analyzed before the callsite fix
# ---------------------------------------------------------------------------

# The corrected redundant_calls analyzer writes
# technical_detail[CALLSITE_WALK_KEY] = CALLSITE_WALK_FIXED into every Redundant
# Call finding it builds. A finding without that exact stamp was analyzed by the
# old walk, whose callsite may be the outer hook entry instead of the loop.
CALLSITE_WALK_KEY = "callsite_walk"
CALLSITE_WALK_FIXED = "outermost_first"

PRE_L5_REDUNDANT_CALL_NOTE = (
	"This Redundant Call finding was analyzed before the callsite fix, so its line may point at "
	"the outer hook call instead of the loop, and an AI suggestion made for it may change the "
	"wrong loop. Optimus no longer asks the AI about it: re-record the flow to get a corrected "
	"finding."
)


def analyzed_before_callsite_fix(finding: dict) -> bool:
	"""True for a Redundant Call finding (render dict or row-shaped dict) whose
	technical detail lacks the fixed analyzer's callsite stamp."""
	if (finding.get("finding_type") or "") != "Redundant Call":
		return False
	return _detail(finding).get(CALLSITE_WALK_KEY) != CALLSITE_WALK_FIXED
