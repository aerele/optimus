# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the Function column in the Queries-per-action table."""

import os
import re

from optimus.renderer._internal import _get_jinja_env
from optimus.renderer.query_callsite import query_callsite


def _call(stack, duration=5):
	return {"duration": duration, "normalized_copies": 1, "stack": stack}


def _frame(filename, function, lineno=10):
	return {"filename": filename, "lineno": lineno, "function": function}


def _render_section(calls):
	path = os.path.join(os.path.dirname(__file__), "..", "templates", "report.html")
	with open(path) as f:
		tpl = f.read()
	m = re.search(r"\{# -+ QUERY BREAKDOWN PER ACTION -+ #\}.*?</section>\s*\{% endif %\}", tpl, re.S)
	assert m, "QUERY BREAKDOWN PER ACTION section not found in report.html"
	template = _get_jinja_env().from_string(m.group(0))
	return template.render(
		recordings_by_uuid={"u1": {"calls": calls}},
		report_data={"actions": [{"recording_uuid": "u1", "action_label": "POST /x"}]},
		fmt_ms=lambda v, d=0: f"{v}ms",
	)


class TestQueryCallsite:
	def test_user_frame_function_and_callsite(self):
		stack = [
			_frame("apps/shop/shop/api.py", "get_items", 42),
			_frame("frappe/database/database.py", "sql", 9),
		]
		assert query_callsite(_call(stack)) == {
			"callsite": "apps/shop/shop/api.py:42",
			"function": "get_items",
		}

	def test_no_stack_is_blank(self):
		assert query_callsite(_call(None)) == {"callsite": "", "function": ""}
		assert query_callsite(_call([])) == {"callsite": "", "function": ""}
		assert query_callsite(None) == {"callsite": "", "function": ""}

	def test_framework_only_stack_uses_innermost_frame(self):
		stack = [_frame("frappe/a.py", "outer"), _frame("frappe/database/database.py", "sql", 9)]
		assert query_callsite(_call(stack))["function"] == "sql"

	def test_missing_function_key_is_blank_but_callsite_kept(self):
		res = query_callsite(_call([{"filename": "apps/shop/shop/x.py", "lineno": 3}]))
		assert res == {"callsite": "apps/shop/shop/x.py:3", "function": ""}

	def test_malformed_frame_blanks_instead_of_raising(self):
		assert query_callsite(_call([{"filename": 123, "lineno": 1, "function": "f"}])) == {
			"callsite": "",
			"function": "",
		}
		assert query_callsite(_call(["not a frame"]))["function"] == ""

	def test_non_string_function_is_blank(self):
		res = query_callsite(_call([{"filename": "apps/s/s/x.py", "lineno": 3, "function": 7}]))
		assert res["function"] == ""

	def test_huge_function_name_is_capped(self):
		res = query_callsite(_call([_frame("apps/s/s/x.py", "f" * 5000)]))
		assert len(res["function"]) == 200

	def test_server_script_frame(self):
		res = query_callsite(_call([_frame("<serverscript>", "<module>", 2)]))
		assert res["function"] == "<module>"


class TestFunctionCellCss:
	def _tpl(self):
		path = os.path.join(os.path.dirname(__file__), "..", "templates", "report.html")
		with open(path) as f:
			return f.read()

	def test_screen_rule_truncates_without_clipping_underscores(self):
		m = re.search(r"table\.queries-flat-table td\.fn-cell code\s*\{([^}]*)\}", self._tpl())
		assert m, "fn-cell rule missing"
		rule = m.group(1)
		assert "text-overflow: ellipsis" in rule
		assert "padding-bottom" in rule  # overflow:hidden would clip "_" descenders

	def test_print_rule_wraps_instead_of_truncating(self):
		assert re.search(
			r"@media print \{\s*table\.queries-flat-table td\.fn-cell code \{[^}]*white-space: normal",
			self._tpl(),
		)


class TestRenderedColumn:
	def test_function_is_its_own_cell_before_callsite(self):
		html = _render_section([_call([_frame("apps/shop/shop/api.py", "get_items", 42)])])
		assert "<th>Function</th>" in html
		assert html.index("<th>Function</th>") < html.index("<th>Callsite</th>")
		assert 'class="small" title="get_items">get_items</code></td>' in html
		assert "apps/shop/shop/api.py:42" in html

	def test_missing_function_shows_dash(self):
		html = _render_section([_call(None)])
		assert '<td class="fn-cell"><span class="muted">&mdash;</span></td>' in html

	def test_long_name_full_text_and_title_kept(self):
		name = "very_long_function_name_" * 8
		html = _render_section([_call([_frame("apps/shop/shop/api.py", name)])])
		assert f'title="{name}">{name}</code>' in html

	def test_name_is_escaped(self):
		html = _render_section([_call([_frame("<serverscript>", "<lambda>")])])
		assert "&lt;lambda&gt;" in html
		assert "<lambda>" not in html
