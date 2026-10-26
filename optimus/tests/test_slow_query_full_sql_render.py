# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Real-render checks that a Slow Query finding card shows the complete SQL."""

from __future__ import annotations

import json

from optimus import renderer
from optimus.analyzers import top_queries
from optimus.tests.test_renderer_structure_snapshot import _finding, _snapshot_doc

_SECRET = "hunter2-do-not-leak"


def _long_sql() -> str:
	cols = ", ".join(f"`col_{i}`" for i in range(5000))
	return (
		f"SELECT {cols} FROM `tabUser` WHERE password ='{_SECRET}' AND `age` < 5 "
		"AND `name` IN (?) -- END_OF_QUERY_MARKER"
	)


def _render(finding_type: str, sql: str) -> str:
	doc = _snapshot_doc()
	f = _finding(finding_type, "High", 900.0, 1)
	detail = json.loads(f.technical_detail_json)
	detail["normalized_query"] = sql
	f.technical_detail_json = json.dumps(detail)
	doc.findings = [f]
	return renderer.render_raw(doc, recordings=[])


def test_slow_query_card_shows_entire_long_query_redacted_and_escaped():
	sql = _long_sql()
	assert len(sql) > 50_000
	html = _render("Slow Query", sql)
	assert '<pre class="sql-full">' in html
	assert "END_OF_QUERY_MARKER" in html  # the tail of a 50 KB statement is present
	assert "`col_4999`" in html or "&#96;col_4999&#96;" in html
	assert _SECRET not in html  # literal on a sensitive column stays redacted
	assert "&lt; 5" in html  # SQL is HTML-escaped, not injected as markup


def test_other_finding_types_do_not_get_the_sql_block():
	html = _render("N+1 Query", "SELECT 1 FROM `tabX` -- END_OF_QUERY_MARKER")
	assert "END_OF_QUERY_MARKER" not in html


def test_capped_query_note_is_visible_in_card():
	sql = "SELECT " + "x" * (top_queries.MAX_SQL_CHARS + 10)
	html = _render("Slow Query", top_queries.cap_sql(sql))
	assert "query truncated at 200000 of" in html


def test_print_css_unclips_sql_blocks():
	html = _render("Slow Query", "SELECT 1")
	print_rule = html.index("pre.sql-full, pre.sql-inline, table.query-table pre.sql-inline")
	# Equal specificity: the print override must come AFTER every screen max-height rule.
	assert print_rule > html.index("table.query-table pre.sql-inline {")
	assert print_rule > html.index("pre.sql-full {")
	assert "max-height: none" in html[print_rule : print_rule + 200]
