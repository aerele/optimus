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

from optimus import ai_fix

_PKG = Path(__file__).resolve().parents[1]
_DOCS = _PKG.parent / "docs" / "AI-FIXING.md"
_JS = _PKG / "optimus" / "doctype" / "optimus_session" / "optimus_session.js"
_MODULES = ("analyze.py", "api.py", "ai_fix.py", "maintenance.py", "line_profile/analyzer.py")
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
			if name in ("log_ai_failure", "_log_ai_step_failure") and node.args:
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
	for expected in ("optimus ai_fix", "optimus ai backfill", "optimus ai auto-suggest", "optimus ai humanize_steps"):
		assert expected in titles, expected


def test_every_ai_error_log_title_starts_with_the_one_prefix():
	titles = _code_titles()
	assert not [t for t in titles if not t.startswith(PREFIX)], titles


def test_the_docs_list_exactly_the_titles_the_code_writes():
	assert _documented_titles() == set(_code_titles())


def test_the_docs_say_to_search_for_the_prefix():
	text = _DOCS.read_text(encoding="utf-8")
	section = text[text.index("### 5.3 Request failures and retries"):text.index("## 6. Keep data on-box")]
	assert f"a title that starts with `{PREFIX}`" in section and f"search the Error Log for `{PREFIX}`" in section


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


def test_the_runbook_spells_fatal_as_the_rows_do():
	"""The rows say ``fatal=True`` / ``fatal=False``; the runbook used to say yes and no."""
	text = _DOCS.read_text(encoding="utf-8")
	cells = re.findall(r"^\| `kind=([a-z_]+)` \| ([^|]+) \|", text, re.M)
	assert cells
	for kind, fatal in cells:
		assert fatal.strip() == str(kind in ai_fix.AI_FATAL_KINDS), kind
	section = text[text.index("### 6.5 Troubleshooting"):text.index("## 7. Threat model")]
	assert "`fatal=True`" in section and "is `yes`" not in section
