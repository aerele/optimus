"""Over-budget captured SQL is omitted before a superlinear parser runs."""

import pytest

from optimus import ai_fix_checks, ai_privacy
from optimus.analyzers.table_breakdown import _parse_query
from optimus.renderer.index_recipes import table_aliases
from optimus.sql_budget import within_budget


@pytest.mark.parametrize(
	"query",
	[
		"SELECT " + "(" * 101 + "1" + ")" * 101,
		"SELECT " + "COALESCE(" * 101 + "1" + ",0)" * 101,
		"SELECT " + "?," * 5_001,
		"SELECT '" + "(" * 101 + "'",
		"SELECT " + "a" * 16_384,
	],
)
def test_all_optional_parsers_refuse_before_parsing(monkeypatch, query):
	import sql_metadata
	import sqlparse

	def unexpected(*args, **kwargs):
		raise AssertionError("over-budget SQL reached a parser")

	monkeypatch.setattr(sqlparse, "parse", unexpected)
	monkeypatch.setattr(sql_metadata, "Parser", unexpected)
	assert not within_budget(query)
	assert ai_privacy.query_text(query, send_raw=False) == ""
	assert ai_fix_checks._sql_clauses(query) is None
	assert _parse_query(query)["tables"] == []
	assert table_aliases(query) == {}


def test_ordinary_query_retains_its_privacy_and_index_metadata():
	query = "SELECT si.name FROM `tabSales Invoice` si WHERE si.customer = 'Alice' ORDER BY si.posting_date LIMIT 20"
	assert within_budget(query)
	assert "Alice" not in ai_privacy.query_text(query, send_raw=False)
	assert table_aliases(query).get("si") == "tabSales Invoice"
	assert _parse_query(query)["tables"] == ["tabSales Invoice"]


def test_exact_cost_boundaries():
	assert within_budget("SELECT " + "(" * 100 + "1" + ")" * 100)
	assert not within_budget("SELECT " + "(" * 101 + "1" + ")" * 101)
	assert within_budget("#" * 10_000)
	assert not within_budget("#" * 10_001)
	assert not within_budget("SELECT " + "1," * 1001 + "1")
	assert within_budget("x" * 16_384)
	assert not within_budget("x" * 16_385)
