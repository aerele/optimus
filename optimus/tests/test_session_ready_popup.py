# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the session-complete popup."""

import inspect
import os
import re

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIDGET_JS = os.path.join(APP_DIR, "public", "js", "floating_widget.js")


def _popup_src() -> str:
	with open(WIDGET_JS) as f:
		src = f.read()
	start = src.index("function showSessionPopup")
	return src[start : src.index("function subscribeRealtime")]


def test_empty_session_publishes_ready_with_empty_flag():
	"""An empty session publishes ready with empty=True."""
	from optimus import analyze

	src = inspect.getsource(analyze.run)
	m = re.search(r"if not recordings:(.*?)return\n", src, re.S)
	assert m, "empty-recordings branch not found"
	assert "optimus_session_ready" in m.group(1)
	assert "empty=True" in m.group(1)


def test_popup_wired_to_ready_and_failed_events():
	with open(WIDGET_JS) as f:
		src = f.read()
	assert 'showSessionPopup(data, "failed")' in src
	assert 'data && data.empty ? "empty" : "ready"' in src


def test_popup_dedupes_escapes_translates_and_routes():
	body = _popup_src()
	assert "localStorage" in body and "optimus_popup_shown:" in body
	assert "frappe.utils.escape_html(docname)" in body
	assert "__(" in body
	assert 'frappe.set_route("Form", "Optimus Session", docname)' in body


def test_popup_does_not_steal_focus_while_typing():
	body = _popup_src()
	assert "activeElement" in body and '"blur"' in body
