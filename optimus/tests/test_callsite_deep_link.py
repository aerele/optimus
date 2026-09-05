# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the clickable callsite -> editor deep-link.

A finding's absolute-path callsite renders as a ``vscode://file`` anchor
(honoured by VS Code, VS Code Insiders and Cursor). Bench-relative paths
render as plain text, since the scheme requires an absolute path.
"""

import json
import types


def _fake_session_doc(callsite_filename="/abs/path/apps/myapp/foo.py",
                      callsite_lineno=42):
	doc = types.SimpleNamespace()
	doc.title = "Test"
	doc.session_uuid = "t"
	doc.user = "a"
	doc.status = "Ready"
	doc.started_at = "2026-04-14"
	doc.stopped_at = "2026-04-14"
	doc.notes = None
	doc.top_severity = "High"
	doc.total_duration_ms = 2000
	doc.total_query_time_ms = 0
	doc.total_queries = 50
	doc.total_requests = 5
	doc.summary_html = None
	doc.top_queries_json = "[]"
	doc.table_breakdown_json = "[]"
	doc.hot_frames_json = "[]"
	doc.session_time_breakdown_json = "{}"
	doc.total_python_ms = 0
	doc.total_sql_ms = 0
	doc.analyzer_warnings = None
	doc.v5_aggregate_json = "{}"
	doc.actions = []

	row = types.SimpleNamespace()
	row.finding_type = "N+1 Query"
	row.severity = "High"
	row.title = "Same query ran 50× at foo.py:42"
	row.customer_description = "desc"
	row.estimated_impact_ms = 500.0
	row.affected_count = 50
	row.action_ref = "0"
	row.technical_detail_json = json.dumps({
		"callsite": {
			"filename": callsite_filename,
			"lineno": callsite_lineno,
			"function": "bulk_process",
		},
	})
	doc.findings = [row]
	return doc


def test_raw_mode_wraps_callsite_in_vscode_link():
	"""Absolute path -> clickable ``vscode://file`` anchor, shaped
	``vscode://file{absolute_path}:{lineno}`` (the absolute path itself starts
	with ``/``).
	"""
	from optimus import renderer

	doc = _fake_session_doc(callsite_filename="/abs/path/apps/myapp/foo.py",
	                        callsite_lineno=42)
	html = renderer.render(doc, recordings=[])

	# The vscode:// href is present with the absolute path + line.
	assert 'href="vscode://file/abs/path/apps/myapp/foo.py:42"' in html, (
		"Raw mode must emit a vscode://file{path}:{line} deep-link"
	)
	# Class marker for the link (used for CSS + future JS hooks).
	assert 'class="callsite-link"' in html
	# Link wraps a <code> block the actual visible callsite text.
	assert "apps/myapp/foo.py:42" in html


def test_bench_relative_path_does_not_emit_link():
	"""A bench-relative (non-absolute) callsite can't form a working vscode://
	URL, so it renders as plain code instead of a broken link."""
	from unittest.mock import patch as _patch

	from optimus import renderer

	doc = _fake_session_doc(callsite_filename="frappe/handler.py",
	                        callsite_lineno=10)
	# v0.13.x: default ignored_apps=("frappe", "erpnext") would filter the
	# fixture finding out. Override to () so this test exercises its
	# actual intent (link rendering, not the filter).
	with _patch("optimus.settings.get_ignored_apps", return_value=()):
		html = renderer.render(doc, recordings=[])

	assert 'vscode://file/frappe/handler.py' not in html, (
		"Non-absolute path must NOT be linked as vscode:// the URL "
		"scheme requires an absolute filesystem path"
	)
	assert "frappe/handler.py:10" in html  # still shown, just as plain code


def test_link_points_to_correct_file_and_line():
	"""Sanity: the URL lineno matches the callsite lineno exactly."""
	from optimus import renderer

	doc = _fake_session_doc(callsite_filename="/home/frappe/bench/apps/myapp/x.py",
	                        callsite_lineno=777)
	html = renderer.render(doc, recordings=[])
	assert 'href="vscode://file/home/frappe/bench/apps/myapp/x.py:777"' in html
