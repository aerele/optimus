# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Every Error Log row the AI surface writes has a title that starts with ``optimus ai``, so one
search finds them all; AI-FIXING lists exactly the titles the code writes, and every pointer to
the Error Log (the analyze warnings, the Refresh toast, the docs) names that prefix.

Titles are read from the code, not copied: the literal ``title`` of every ``_run_ai_step``,
``log_ai_failure``, ``_log_ai_step_failure`` and ``_increment_session_counter`` call."""

import ast
import re
from pathlib import Path

_PKG = Path(__file__).resolve().parents[1]
_DOCS = _PKG.parent / "docs" / "AI-FIXING.md"
_JS = _PKG / "optimus" / "doctype" / "optimus_session" / "optimus_session.js"
_MODULES = ("analyze.py", "api.py", "ai_fix.py", "maintenance.py", "line_profile/analyzer.py", "ai_jobs.py", "line_profile/jobs.py")
PREFIX = "optimus ai"


def _callee(call):
	if isinstance(call.func, ast.Attribute):
		return call.func.attr
	return getattr(call.func, "id", None)


def _code_titles() -> dict[str, set[str]]:
	found: dict[str, set[str]] = {}
	for module in _MODULES:
		for node in ast.walk(ast.parse((_PKG / module).read_text(encoding="utf-8"))):
			if not isinstance(node, ast.Call):
				continue
			name = _callee(node)
			title = None
			if name in ("log_ai_failure", "_log_ai_step_failure", "_log") and node.args:
				title = node.args[0]
			elif name in ("_run_ai_step", "_increment_session_counter", "log_ai_failure"):
				title = next((kw.value for kw in node.keywords if kw.arg == "title"), None)
			if isinstance(title, ast.Constant) and isinstance(title.value, str):
				found.setdefault(title.value, set()).add(module)
	return found


def _documented_titles() -> set[str]:
	return {t for t in re.findall(r"`(optimus ai[^`]*)`", _DOCS.read_text(encoding="utf-8")) if t != PREFIX}


def test_the_code_titles_are_found():
	titles = _code_titles()
	for expected in ("optimus ai_fix", "optimus ai refresh item", "optimus ai admission after analyze", "optimus ai refresh report"):
		assert expected in titles, expected


def test_every_ai_error_log_title_starts_with_the_one_prefix():
	titles = _code_titles()
	assert not [t for t in titles if not t.startswith(PREFIX)], titles


def test_the_docs_list_exactly_the_titles_the_code_writes():
	assert _documented_titles() == set(_code_titles())


def test_every_pointer_names_the_prefix():
	"""A pointer that named only one title (``optimus ai_fix``, ``optimus ai backfill``) found
	only some rows: the Steps rewrite, the recording reads and the re-renders have their own."""
	pointers = [
		*re.findall(r"Error Log[^\n]*", (_PKG / "analyze.py").read_text(encoding="utf-8")),
		*re.findall(r"Error Log[^\n]*", _JS.read_text(encoding="utf-8")),
	]
	pointers = [p for p in pointers if "title" in p and "(outer)" not in p]
	assert pointers
	for pointer in pointers:
		assert f"titles starting with {PREFIX}" in pointer or f'titles starting with \\"{PREFIX}\\"' in pointer, pointer
	# The Refresh toast sends the operator to an administrator rather than to the log itself.
	assert "Ask your administrator" in (_PKG / "ai_refresh_view.py").read_text(encoding="utf-8")
