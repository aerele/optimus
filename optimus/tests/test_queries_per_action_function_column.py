# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the Function column in the Queries-per-action table."""

import os
import re
from types import SimpleNamespace

from optimus import renderer
from optimus.renderer.queries_per_action import build_queries_per_action


def _frame(filename, function, lineno=10):
	return {"filename": filename, "lineno": lineno, "function": function}


def _call(stack, duration=5):
	return {"duration": duration, "normalized_copies": 1, "stack": stack}


def _row(stack):
	actions = [{"recording_uuid": "u1", "action_label": "POST /x"}]
	rows = build_queries_per_action(actions, {"u1": {"calls": [_call(stack)]}})["rows"]
	return rows[0]


class TestRowFunction:
	def test_function_and_callsite_come_from_the_same_frame(self):
		stack = [
			_frame("apps/shop/shop/api.py", "get_items", 42),
			_frame("frappe/database/database.py", "sql", 9),
		]
		row = _row(stack)
		assert row["callsite"] == "apps/shop/shop/api.py:42"
		assert row["function"] == "get_items"

	def test_no_stack_is_blank(self):
		for stack in (None, []):
			row = _row(stack)
			assert row["callsite"] == ""
			assert row["function"] == ""

	def test_framework_only_stack_uses_innermost_frame(self):
		stack = [_frame("frappe/a.py", "outer"), _frame("frappe/database/database.py", "sql", 9)]
		row = _row(stack)
		assert row["function"] == "sql"
		assert row["callsite"] == "frappe/database/database.py:9"

	def test_missing_function_key_is_blank_but_callsite_kept(self):
		row = _row([{"filename": "apps/shop/shop/x.py", "lineno": 3}])
		assert row["callsite"] == "apps/shop/shop/x.py:3"
		assert row["function"] == ""

	def test_malformed_frame_does_not_raise(self):
		assert _row([{"filename": 123, "lineno": 1, "function": "f"}])["function"] == "f"
		assert _row(["not a frame"])["function"] == ""

	def test_non_string_function_is_blank(self):
		assert _row([_frame("apps/s/s/x.py", 7, 3)])["function"] == ""

	def test_huge_function_name_is_capped(self):
		assert len(_row([_frame("apps/s/s/x.py", "f" * 5000)])["function"]) == 200

	def test_server_script_frame(self):
		assert _row([_frame("<serverscript>", "<module>", 2)])["function"] == "<module>"


def _template() -> str:
	path = os.path.join(os.path.dirname(__file__), "..", "templates", "report.html")
	with open(path) as f:
		return f.read()


class TestFunctionCellCss:
	def test_screen_rule_truncates_without_clipping_underscores(self):
		m = re.search(r"table\.queries-flat-table td\.fn-cell code\s*\{([^}]*)\}", _template())
		assert m, "fn-cell rule missing"
		rule = m.group(1)
		assert "text-overflow: ellipsis" in rule
		assert "padding-bottom" in rule  # overflow:hidden would clip "_" descenders

	def test_print_rule_wraps_instead_of_truncating(self):
		assert re.search(
			r"@media print \{\s*table\.queries-flat-table td\.fn-cell code \{[^}]*white-space: normal",
			_template(),
		)


def _render(calls):
	"""Render a full report whose one action carries ``calls``."""
	doc = SimpleNamespace(
		name="PS-fn", session_uuid="fn-uuid", title="fn test", user="t@example.com",
		status="Ready", started_at="2026-05-22T00:00:00", stopped_at="2026-05-22T00:00:05",
		notes=None, top_severity="High", summary_html=None, total_duration_ms=5000,
		total_query_time_ms=0, total_queries=len(calls), total_requests=1,
		top_queries_json="[]", table_breakdown_json="[]", hot_frames_json=None,
		session_time_breakdown_json=None, total_python_ms=None, total_sql_ms=None,
		analyzer_warnings=None, v5_aggregate_json="{}", findings=[], phase_2_runs=[],
		actions=[SimpleNamespace(
			action_label="POST /x", event_type="HTTP Request", http_method="POST",
			path="/x", recording_uuid="u1", duration_ms=900.0, queries_count=len(calls),
			query_time_ms=0, slowest_query_ms=0,
		)],
	)
	return renderer.render_raw(doc, recordings=[{"uuid": "u1", "calls": calls}])


class TestRenderedColumn:
	def test_function_is_its_own_cell_before_callsite(self):
		html = _render([_call([_frame("apps/shop/shop/api.py", "get_items", 42)])])
		assert "<th>Function</th>" in html
		assert html.index("<th>Function</th>") < html.index("<th>Callsite</th>")
		assert 'class="small" title="get_items">get_items</code></td>' in html
		assert "apps/shop/shop/api.py:42" in html

	def test_missing_function_shows_dash(self):
		html = _render([_call(None)])
		assert '<td class="fn-cell"><span class="muted">&mdash;</span></td>' in html

	def test_long_name_full_text_and_title_kept(self):
		name = "very_long_function_name_" * 8
		html = _render([_call([_frame("apps/shop/shop/api.py", name)])])
		assert f'title="{name}">{name}</code>' in html

	def test_name_is_escaped(self):
		html = _render([_call([_frame("<serverscript>", "<lambda>")])])
		assert "&lt;lambda&gt;" in html
		assert 'title="<lambda>"' not in html
