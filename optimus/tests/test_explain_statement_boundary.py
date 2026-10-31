"""A recorded compound query must never replay its trailing writes."""

from types import SimpleNamespace

import frappe
import pytest

from optimus.dbdialect.mariadb import MariaDBDialect
from optimus.dbdialect.postgres import PostgresDialect


@pytest.fixture
def explain_db(monkeypatch):
	operations = []
	def sql(query, **kwargs):
		operations.append(("sql", query))
		if query.startswith("EXPLAIN (FORMAT JSON)"):
			return [([{"Plan": {"Node Type": "Result"}}],)]
		return []
	monkeypatch.setattr(frappe, "db", SimpleNamespace(
		sql=sql, savepoint=lambda name: operations.append(("savepoint", name)),
		rollback=lambda save_point: operations.append(("rollback", save_point)),
		release_savepoint=lambda name: operations.append(("release", name)),
	), raising=False)
	return operations


@pytest.mark.parametrize("dialect", [MariaDBDialect, PostgresDialect])
@pytest.mark.parametrize("query", [
	"SELECT 1; UPDATE tabCounter SET value = value + 1",
	"SELECT 1; DELETE FROM tabCounter",
	"SELECT 1; COMMIT; UPDATE tabCounter SET value = value + 1;",
	"SELECT 1;;", "SELECT ';'", 'SELECT "semi;colon" FROM tabCounter',
	"SELECT $$semi;colon$$", "SELECT $tag$semi;colon$tag$",
	"SELECT E'back\\\\slash;string'", "SELECT 1 /* ; */", "SELECT 1 -- ; comment\n", "",
])
def test_declined_query_never_reaches_database(dialect, query, explain_db):
	plan = dialect().run_explain(query)
	assert not plan.ok and not plan.tables
	assert "EXPLAIN skipped" in plan.reason
	assert explain_db == []


@pytest.mark.parametrize("dialect", [MariaDBDialect, PostgresDialect])
@pytest.mark.parametrize("query", [
	"SELECT 1", " SELECT 1; \n", "SELECT 1 /* comment */;",
	"UPDATE tabCounter SET value = 2 WHERE value = 1;",
	"DELETE FROM tabCounter WHERE value = 1;",
])
def test_single_statement_still_has_plan(dialect, query, explain_db):
	assert dialect().run_explain(query).ok
	sql = [value for op, value in explain_db if op == "sql"]
	assert len(sql) == 1 and ";" not in sql[0]
	if dialect is PostgresDialect:
		assert [op for op, _ in explain_db] == ["savepoint", "sql", "rollback", "release"]
		assert explain_db[0][1] == explain_db[2][1] == explain_db[3][1]


def test_enrichment_surfaces_decline_even_with_cached_empty_result(monkeypatch):
	from optimus import analyze, settings
	monkeypatch.setattr(analyze, "safe_commit", lambda: None)
	monkeypatch.setattr(analyze, "is_query_type", lambda query, _types: query.lstrip().upper().startswith("SELECT"))
	monkeypatch.setattr(frappe, "conf", {"optimus_explain_cache_ttl_seconds": 60}, raising=False)
	monkeypatch.setattr(settings, "get_config", lambda: SimpleNamespace(max_queries_per_recording=100))
	cache_reads = []
	monkeypatch.setattr(frappe, "cache", SimpleNamespace(get_value=lambda key: cache_reads.append(key) or []), raising=False)
	calls = [{"query": "SELECT 1; UPDATE tabCounter SET value = 2"}, {"query": "SELECT ';'"}]
	warnings = analyze._enrich_recordings([{"calls": calls}])
	assert len(warnings) == 1 and "EXPLAIN skipped" in warnings[0]
	assert all(call["explain_warning"] == warnings[0] and call["explain_result"] == [] for call in calls)
	assert cache_reads == []
