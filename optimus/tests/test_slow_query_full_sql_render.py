# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Real-render checks that a Slow Query finding card shows the complete SQL."""

from __future__ import annotations

import json
import re

import pytest

from optimus import renderer
from optimus.analyzers import top_queries
from optimus.analyzers.base import MAX_SQL_CHARS, AnalyzeContext, cap_sql
from optimus.renderer import _internal
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


def _print_rules(html: str) -> list[tuple[str, str]]:
	"""(selector list, declarations) of every rule inside an @media print block, in order."""
	css = re.sub(r"/\*.*?\*/", "", "".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S)), flags=re.S)
	rules = []
	for m in re.finditer(r"@media\s+print\s*\{", css):
		depth, i = 1, m.end()
		start = i
		while depth and i < len(css):
			depth += {"{": 1, "}": -1}.get(css[i], 0)
			i += 1
		block = css[start : i - 1]
		rules += [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", block)]
	return rules


def test_slow_query_card_shows_entire_long_query_redacted_and_escaped():
	sql = _long_sql()
	assert len(sql) > 50_000
	html = _render("Slow Query", sql)
	assert '<pre class="sql-full" tabindex="0">' in html
	assert "END_OF_QUERY_MARKER" in html  # the tail of a 50 KB statement is present
	assert "`col_4999`" in html  # backticks are not HTML-escaped by Jinja
	assert _SECRET not in html  # literal on a sensitive column stays redacted
	assert "&lt; 5" in html  # SQL is HTML-escaped, not injected as markup


def test_long_sql_is_collapsed_with_a_character_count_and_short_sql_is_open():
	long_html = _render("Slow Query", _long_sql())
	assert '<details class="sql-full-box">' in long_html
	assert re.search(r"Full SQL, sensitive values hidden &middot; [\d,]+ characters", long_html)
	short_html = _render("Slow Query", "SELECT 1 FROM `tabUser`")
	assert "sql-full-box" not in short_html.split("</style>")[-1]
	assert '<span class="label">Full SQL, sensitive values hidden</span>' in short_html
	assert "Query (complete, literals normalized)" not in short_html


def test_other_finding_types_do_not_get_the_sql_block():
	html = _render("N+1 Query", "SELECT 1 FROM `tabX` -- END_OF_QUERY_MARKER")
	assert "END_OF_QUERY_MARKER" not in html


def test_capped_query_is_flagged_in_the_label_and_the_note_is_visible():
	html = _render("Slow Query", cap_sql("SELECT " + "x" * (MAX_SQL_CHARS + 10)))
	assert "query truncated at 200000 of" in html
	assert "(truncated, see the note at the end)" in html


def test_print_css_unclips_sql_blocks_and_lets_a_long_card_break():
	html = _render("Slow Query", "SELECT 1")
	css = "".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))
	rules = _print_rules(html)
	unclip = [body for sel, body in rules if "pre.sql-full" in sel and "pre.sql-inline" in sel]
	assert unclip and "max-height: none" in unclip[-1] and "overflow: visible" in unclip[-1]
	# Equal specificity: the print override has to come after every screen max-height rule.
	print_at = css.index("pre.sql-full, pre.sql-inline, table.query-table pre.sql-inline")
	assert print_at > css.index("table.query-table pre.sql-inline {")
	assert print_at > css.index("pre.sql-full {")
	breaks = [body for sel, body in rules if sel == ".finding.finding-long-sql"]
	assert breaks and "page-break-inside: auto" in breaks[-1]


def test_long_sql_card_gets_the_page_break_class_and_short_one_does_not():
	assert "finding-long-sql" in re.findall(r'<div class="finding [^"]*"', _render("Slow Query", _long_sql()))[0]
	assert "finding-long-sql" not in re.findall(r'<div class="finding [^"]*"', _render("Slow Query", "SELECT 1"))[0]


def test_analyzer_output_renders_end_to_end_with_the_truncation_note():
	secret_tail = f"'{_SECRET}"
	sql = "SELECT 1 FROM `tabUser` WHERE " + "a = 1 AND " * (MAX_SQL_CHARS // 10)
	sql = sql[: MAX_SQL_CHARS - 30] + f" password = {secret_tail}' AND x = 2"
	recording = {
		"uuid": "r1",
		"calls": [{
			"query": sql, "normalized_query": sql, "duration": 900.0,
			"stack": [{"filename": "acme_app/acme_app/api.py", "lineno": 12}],
		}],
	}
	result = top_queries.analyze([recording], AnalyzeContext(session_uuid="u", docname="d"))
	found = result.findings[0]
	doc = _snapshot_doc()
	f = _finding("Slow Query", "High", 900.0, 1)
	f.technical_detail_json = found["technical_detail_json"]
	doc.findings = [f]
	html = renderer.render_raw(doc, recordings=[])
	assert "query truncated at 200000 of" in html
	assert "(truncated, see the note at the end)" in html
	assert _SECRET not in html


class TestRedactFindingSql:
	def _f(self, sql, ftype="Slow Query"):
		return {"finding_type": ftype, "technical_detail": {"normalized_query": sql}}

	def test_redacts_sensitive_literal(self):
		f = self._f(f"SELECT 1 FROM `tabUser` WHERE password = '{_SECRET}'")
		_internal.redact_finding_sql([f])
		assert _SECRET not in f["technical_detail"]["normalized_query"]

	def test_ignores_other_types_and_missing_detail(self):
		other = self._f(f"password = '{_SECRET}'", "N+1 Query")
		_internal.redact_finding_sql([other, {"finding_type": "Slow Query", "technical_detail": None}, {}])
		assert _SECRET in other["technical_detail"]["normalized_query"]

	def test_regex_failure_drops_the_sql_and_keeps_the_other_findings(self, monkeypatch):
		boom = self._f("SELECT boom")
		fine = self._f(f"SELECT 1 WHERE password = '{_SECRET}'")

		def fake(sql, **kw):
			if "boom" in sql:
				raise RuntimeError("regex blew up")
			return "REDACTED"

		monkeypatch.setattr(_internal, "_redact_sql_literals_base", fake)
		_internal.redact_finding_sql([boom, fine])
		assert "normalized_query" not in boom["technical_detail"]
		assert fine["technical_detail"]["normalized_query"] == "REDACTED"

	def test_non_string_sql_is_dropped(self):
		f = self._f(["not", "a", "string"])
		_internal.redact_finding_sql([f])
		assert "normalized_query" not in f["technical_detail"]


class TestCapSql:
	def test_exactly_the_limit_is_untouched_and_one_over_is_cut(self):
		at = "a" * MAX_SQL_CHARS
		assert cap_sql(at) == at
		over = cap_sql(at + "b")
		assert over.startswith(at) and over.endswith(f"of {MAX_SQL_CHARS + 1} characters]")

	@pytest.mark.parametrize("value", [None, 5, ["x"], b"SELECT"])
	def test_non_str_values_pass_through(self, value):
		assert cap_sql(value) is value

	def test_cut_inside_a_quoted_literal_drops_the_open_literal(self):
		sql = "x" * (MAX_SQL_CHARS - 60) + " WHERE password = 'hunter2-secret-value-and-more" + "y" * 100
		out = cap_sql(sql)
		assert "hunter2" not in out and "password = " in out
		assert "query truncated at" in out

	def test_closed_literals_before_the_cut_are_kept(self):
		sql = "x" * (MAX_SQL_CHARS - 30) + " WHERE a = 'ok' AND b = 5 " + "y" * 100
		assert "a = 'ok'" in cap_sql(sql)

	def test_escaped_quote_does_not_confuse_the_scan(self):
		sql = "x" * (MAX_SQL_CHARS - 40) + " WHERE a = 'it\\'s' AND b = " + "y" * 100
		assert "it\\'s" in cap_sql(sql)

	def test_truncation_is_logged(self, caplog):
		with caplog.at_level("INFO", logger="optimus.analyzers.base"):
			cap_sql("a" * (MAX_SQL_CHARS + 1))
		assert "capped an oversized query" in caplog.text


def test_is_cut_query_recognises_the_marker_and_the_old_length_limit():
	from optimus.analyzers.base import QUERY_TEXT_LIMIT, is_cut_query

	assert is_cut_query("a" * QUERY_TEXT_LIMIT)
	assert not is_cut_query("a" * (QUERY_TEXT_LIMIT - 1))
	assert is_cut_query("SELECT 1\n-- [Optimus: query truncated at 9 of 12 characters]")
	assert not is_cut_query(None)
