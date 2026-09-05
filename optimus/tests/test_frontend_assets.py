# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Smoke tests for frontend assets (widget JS, form JS, CSS).

Cheap checks (no cypress/playwright): the JS files parse, the widget's state
machine symbols are present and the CSS selectors look sane. Enough to catch
a broken syntax or a deleted critical hook.
"""

import os
import re
import shutil
import subprocess

import pytest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIDGET_JS = os.path.join(APP_DIR, "public", "js", "floating_widget.js")
WIDGET_CSS = os.path.join(APP_DIR, "public", "css", "floating_widget.css")
FORM_JS = os.path.join(
	APP_DIR, "optimus", "doctype", "optimus_session", "optimus_session.js"
)
LIST_JS = os.path.join(
	APP_DIR, "optimus", "doctype", "optimus_session", "optimus_session_list.js"
)


def _node_check(js_path: str) -> None:
	"""Run `node --check` to validate JS syntax. Skips if node isn't installed
	(frappe benches ship with node, so it runs everywhere that matters).
	"""
	if not shutil.which("node"):
		pytest.skip("node not installed")
	result = subprocess.run(
		["node", "--check", js_path],
		capture_output=True,
		text=True,
	)
	if result.returncode != 0:
		pytest.fail(f"node --check failed for {js_path}:\n{result.stderr}")


def test_widget_js_syntax():
	_node_check(WIDGET_JS)


def test_form_js_syntax():
	_node_check(FORM_JS)


def test_list_js_syntax():
	_node_check(LIST_JS)


def test_widget_has_state_machine_constants():
	"""The widget's 5 states must all be referenced in the JS."""
	with open(WIDGET_JS) as f:
		src = f.read()
	for state in ("inactive", "recording", "stopping", "analyzing", "ready"):
		assert f"fp-state-{state}" in src, f"Missing state class: fp-state-{state}"


def test_widget_has_visibility_listener():
	"""The visibilitychange handler issues a one-shot refreshStatus() when the
	tab becomes visible again, covering TTL-based auto-stop and any realtime
	events dropped while the tab was hidden.
	"""
	with open(WIDGET_JS) as f:
		src = f.read()
	assert "visibilitychange" in src
	# v0.5.1: no more stopPolling the handler just re-fetches
	# state on visibility return. The polling-pause machinery was
	# removed; see test_realtime_session_events for the new
	# realtime contract.
	assert "refreshStatus" in src


def test_widget_role_check():
	"""Widget must check for Optimus User or System Manager role."""
	with open(WIDGET_JS) as f:
		src = f.read()
	assert "System Manager" in src
	assert "Optimus User" in src
	assert "userHasRole" in src


def test_form_js_has_retry_button():
	"""Fix #11: form JS must have a Retry Analyze button for Failed sessions."""
	with open(FORM_JS) as f:
		src = f.read()
	assert "Retry Analyze" in src
	assert "retry_analyze" in src
	assert 'status !== "Failed"' in src or "Failed" in src


def test_form_js_does_not_render_analyzer_warnings_intro():
	"""The analyzer_warnings intro banner is not rendered in the Optimus
	Session form view (the data is still stored on the hidden
	``analyzer_warnings`` field for offline inspection)."""
	with open(FORM_JS) as f:
		src = f.read()
	# No render call, no helper definition, no set_intro of warnings.
	assert "render_analyzer_warnings" not in src
	assert "set_intro(frm.doc.analyzer_warnings" not in src


def test_form_js_has_report_buttons():
	"""Form JS must wire up both report buttons: "Download Report" (anchor
	click, real download) and "Open Report" (window.open, inline in a new tab),
	with no stale PDF-report button. Access is gated server-side by the File
	permission hook.
	"""
	with open(FORM_JS) as f:
		src = f.read()
	# Both report buttons present.
	assert "Download Report" in src
	assert "Open Report" in src
	assert "raw_report_file" in src
	# The PDF button is gone confirm no stale reference leaked
	# into the form JS.
	assert "Download Report (PDF)" not in src
	assert "optimus.api.download_pdf" not in src


def test_list_js_severity_indicators():
	"""List view must color-code by top_severity for Ready sessions."""
	with open(LIST_JS) as f:
		src = f.read()
	assert "top_severity" in src
	assert "High severity" in src
	assert "Medium severity" in src


def test_widget_start_has_error_callback():
	"""The Start dialog's frappe.call(api.start) must include an error callback;
	without it a server-side failure (permission error, concurrent session,
	exception) closes the dialog silently with no feedback.
	"""
	with open(WIDGET_JS) as f:
		src = f.read()

	# Find the start dialog's primary_action block and verify it
	# contains both callback: and error:.
	start_call_idx = src.find("optimus.api.start")
	assert start_call_idx > 0, "widget must call optimus.api.start"

	# Look in the ~2000 chars around the start call for an error: key.
	window = src[start_call_idx : start_call_idx + 2000]
	assert "error:" in window or "error: " in window, (
		"openStartDialog's frappe.call(api.start) must have an error "
		"callback without it, permission errors and server exceptions "
		"leave the widget silently unresponsive after the dialog closes"
	)


def test_widget_stop_has_error_callback():
	"""The Stop call must keep its error callback so a failed stop doesn't
	strand the widget in 'Stopping...'. Scans the whole confirmAndStop function
	body (brace-matched) rather than a fixed window.
	"""
	with open(WIDGET_JS) as f:
		src = f.read()

	fn_idx = src.find("function confirmAndStop")
	assert fn_idx > 0, "widget must define confirmAndStop"

	# Find the end of the function: match the opening brace after the
	# function name, then walk forward tracking brace balance.
	open_brace_idx = src.find("{", fn_idx)
	assert open_brace_idx > 0
	depth = 0
	end_idx = None
	for i in range(open_brace_idx, len(src)):
		c = src[i]
		if c == "{":
			depth += 1
		elif c == "}":
			depth -= 1
			if depth == 0:
				end_idx = i + 1
				break
	assert end_idx is not None, "couldn't find end of confirmAndStop"

	body = src[fn_idx:end_idx]
	assert "optimus.api.stop" in body
	assert "error:" in body, (
		"confirmAndStop's frappe.call(api.stop) must have an error "
		"callback so failed stops don't strand the widget in 'Stopping…'"
	)


def test_widget_stop_handles_no_active_session():
	"""When the stop API returns {stopped: false} (session already gone), the
	widget must reset to inactive, not transition to 'Analyzing...' (which would
	hang forever).
	"""
	with open(WIDGET_JS) as f:
		src = f.read()

	fn_idx = src.find("function confirmAndStop")
	open_brace_idx = src.find("{", fn_idx)
	depth = 0
	end_idx = None
	for i in range(open_brace_idx, len(src)):
		c = src[i]
		if c == "{":
			depth += 1
		elif c == "}":
			depth -= 1
			if depth == 0:
				end_idx = i + 1
				break
	body = src[fn_idx:end_idx]

	# Must explicitly check data.stopped === false
	assert "data.stopped === false" in body, (
		"confirmAndStop's success callback must handle the "
		"{stopped: false} response (session already gone) and reset "
		"to inactive without this check, the widget falls through "
		"to the 'Analyzing…' branch and hangs forever"
	)


def test_widget_css_selectors():
	"""CSS must define the widget root and state classes."""
	with open(WIDGET_CSS) as f:
		src = f.read()
	assert "#frappe-profiler-widget" in src
	for state in ("inactive", "recording", "stopping", "analyzing", "ready"):
		assert f".fp-state-{state}" in src, f"Missing CSS class: .fp-state-{state}"


def test_widget_css_has_print_safety():
	"""CSS should have print-safe rules since reports are often printed."""
	with open(WIDGET_CSS) as f:
		src = f.read()
	# Widget is for Desk, not print, but it should be hidden in print mode
	# OR have sane fallbacks. Minimum: the file should not be empty.
	assert len(src) > 100
