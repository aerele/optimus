# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""PostgresDialect adapter + the dialect-blind proof.

The plan-tree walk and catalog-row mapping are unit-tested here with fixtures
(the raw introspection SQL is validated on a real --db-type postgres bench in
phase 5). The final test feeds a Postgres-derived plan through explain_flags and
asserts the SAME finding types come out i.e. the analyzer is dialect-blind.
"""

from __future__ import annotations

import types
from dataclasses import asdict

import pytest

from optimus.dbdialect.base import PlanTable
from optimus.dbdialect.postgres import PostgresDialect


def _install_db(monkeypatch, sql_fn):
	import frappe

	# _safe_sql wraps every query in savepoint / release / rollback (Postgres
	# aborts the txn on a failed query) provide no-ops for them.
	monkeypatch.setattr(frappe, "db", types.SimpleNamespace(
		sql=sql_fn, db_type="postgres",
		savepoint=lambda *a, **k: None,
		release_savepoint=lambda *a, **k: None,
		rollback=lambda *a, **k: None,
	), raising=False)


# --- EXPLAIN (FORMAT JSON) plan-tree walk -----------------------------------

def _seq_scan_under(node_type, rel="tabGL Entry", rows=12000):
	"""A scan wrapped under one parent node (Sort / HashAggregate / …)."""
	return [{"Plan": {
		"Node Type": node_type,
		"Plans": [{"Node Type": "Seq Scan", "Relation Name": rel, "Alias": "g", "Plan Rows": rows}],
	}}]


class TestRunExplain:
	def test_seq_scan_is_full_scan(self, monkeypatch):
		plan = [{"Plan": {"Node Type": "Seq Scan", "Relation Name": "tabGL Entry", "Plan Rows": 12000}}]
		_install_db(monkeypatch, lambda q, *a, **k: [(plan,)])   # psycopg auto-parses json
		p = PostgresDialect().run_explain("SELECT ...")
		assert p.ok and len(p.tables) == 1
		t = p.tables[0]
		assert t.table == "tabGL Entry" and t.full_scan is True
		assert t.rows_examined == 12000 and t.selectivity_pct is None
		assert t.raw["filtered"] is None  # kept so downstream stays dialect-agnostic

	def test_sort_over_scan_sets_sort_flag(self, monkeypatch):
		_install_db(monkeypatch, lambda q, *a, **k: [(_seq_scan_under("Sort"),)])
		t = PostgresDialect().run_explain("q").tables[0]
		assert t.full_scan is True and t.sort_without_index is True and t.temp_used is False

	def test_hashaggregate_sets_temp_flag(self, monkeypatch):
		_install_db(monkeypatch, lambda q, *a, **k: [(_seq_scan_under("HashAggregate", rows=500),)])
		t = PostgresDialect().run_explain("q").tables[0]
		assert t.temp_used is True and t.sort_without_index is False

	def test_index_scan_is_not_full_scan(self, monkeypatch):
		plan = [{"Plan": {"Node Type": "Index Scan", "Relation Name": "tabAccount",
		                  "Index Name": "tabAccount_party_index", "Plan Rows": 1}}]
		_install_db(monkeypatch, lambda q, *a, **k: [(plan,)])
		t = PostgresDialect().run_explain("q").tables[0]
		assert t.full_scan is False and t.used_index == "tabAccount_party_index"

	def test_json_string_result_is_parsed(self, monkeypatch):
		import json
		plan = [{"Plan": {"Node Type": "Seq Scan", "Relation Name": "tabX", "Plan Rows": 9}}]
		_install_db(monkeypatch, lambda q, *a, **k: [(json.dumps(plan),)])  # some drivers return text
		t = PostgresDialect().run_explain("q").tables[0]
		assert t.table == "tabX" and t.rows_examined == 9

	def test_explain_error_returns_not_ok(self, monkeypatch):
		def boom(*a, **k):
			raise RuntimeError("syntax error at or near …")
		_install_db(monkeypatch, boom)
		p = PostgresDialect().run_explain("bad")
		assert p.ok is False and p.tables == []


# --- catalog introspection ---------------------------------------------------

class TestIntrospection:
	def test_existing_indexes_groups_ordered_columns(self, monkeypatch):
		rows = [
			{"index_name": "tabUser_pkey", "column_name": "name", "seq": 1, "is_unique": True},
			{"index_name": "idx_ab", "column_name": "b", "seq": 2, "is_unique": False},
			{"index_name": "idx_ab", "column_name": "a", "seq": 1, "is_unique": False},
		]
		_install_db(monkeypatch, lambda q, *a, **k: rows)
		by = {i.name: i for i in PostgresDialect().existing_indexes("tabUser")}
		assert by["idx_ab"].columns == ["a", "b"] and by["idx_ab"].leftmost == "a"
		assert by["idx_ab"].unique is False
		assert by["tabUser_pkey"].unique is True and by["tabUser_pkey"].leftmost == "name"

	def test_column_types_lowercased(self, monkeypatch):
		rows = [{"column_name": "party", "data_type": "character varying"},
				{"column_name": "amount", "data_type": "numeric"}]
		_install_db(monkeypatch, lambda q, *a, **k: rows)
		assert PostgresDialect().column_types("tabGL Entry") == {
			"party": "character varying", "amount": "numeric"}

	def test_index_ddl_is_create_index_no_prefix(self):
		d = PostgresDialect()
		assert d.index_ddl("tabUser", "email", False) == (
			'CREATE INDEX IF NOT EXISTS "tabUser_email_index" ON "public"."tabUser" ("email");'
		)
		# is_text_col makes no difference on Postgres (no prefix syntax)
		assert d.index_ddl("tabUser", "bio", True) == (
			'CREATE INDEX IF NOT EXISTS "tabUser_bio_index" ON "public"."tabUser" ("bio");'
		)

	def test_infra_snapshot(self, monkeypatch):
		def fake_sql(query, *a, **k):
			if "state = 'active'" in query:
				return [(3,)]
			if "pg_stat_activity" in query:
				return [(12,)]
			if "max_connections" in query:
				return [("100",)]
			return []
		_install_db(monkeypatch, fake_sql)
		snap = PostgresDialect().infra_snapshot()
		assert snap.threads_connected == 12 and snap.threads_running == 3
		assert snap.max_connections == 100
		assert snap.slow_queries is None  # no global counter without pg_stat_statements


# --- dialect-blind: a Postgres plan yields the same finding types -----------

def test_explain_flags_derives_findings_from_postgres_plan(monkeypatch, empty_context):
	"""A PG Seq Scan over 12k rows must surface a Full Table Scan finding
	proving explain_flags reads the normalized fields, not raw EXPLAIN rows."""
	from optimus.analyzers import explain_flags

	pt = PlanTable(table="tabGL Entry", full_scan=True, rows_examined=12000,
	               raw={"Node Type": "Seq Scan", "filtered": None})
	recording = {
		"uuid": "pg1", "path": "/", "cmd": None, "method": "GET",
		"event_type": "HTTP Request", "duration": 100,
		"calls": [{
			"query": "SELECT * FROM \"tabGL Entry\"",
			"normalized_query": "SELECT * FROM \"tabGL Entry\"",
			"duration": 100,
			"explain_result": [asdict(pt)],   # normalized PlanTable dict (what the runner stores)
			"stack": [],
		}],
	}
	result = explain_flags.analyze([recording], empty_context)
	scans = [f for f in result.findings if f["finding_type"] == "Full Table Scan"]
	assert len(scans) == 1 and "tabGL Entry" in scans[0]["title"]


def test_ai_finding_hint_is_dialect_aware(monkeypatch):
	"""The four EXPLAIN-based AI hints use MariaDB EXPLAIN-column wording on
	MariaDB and Postgres plan-node wording on Postgres; neutral types are the
	same on both."""
	import frappe

	from optimus import ai_fix

	monkeypatch.setattr(frappe, "db", types.SimpleNamespace(db_type="mariadb"), raising=False)
	assert "type=ALL" in ai_fix._finding_type_hint("Full Table Scan")
	assert "Using filesort" in ai_fix._finding_type_hint("Filesort")

	monkeypatch.setattr(frappe, "db", types.SimpleNamespace(db_type="postgres"), raising=False)
	assert "Seq Scan" in ai_fix._finding_type_hint("Full Table Scan")
	assert "`Sort` node" in ai_fix._finding_type_hint("Filesort")

	# A dialect-neutral finding type is identical regardless of dialect.
	assert ai_fix._finding_type_hint("N+1 Query") == ai_fix._FINDING_TYPE_HINTS["N+1 Query"]


# --- _safe_sql transaction-safety hardening -----------------------------------

class TestSafeSql:
	def test_original_error_survives_a_failing_rollback(self, monkeypatch):
		"""If the savepoint rollback itself raises, the ORIGINAL query error must
		still propagate (not be masked by the rollback error) the caller's
		try/except turns the original into its safe default."""
		import frappe

		def sql_fn(*a, **k):
			raise ValueError("original query error")

		def rollback(*a, **k):
			raise RuntimeError("rollback also failed")

		monkeypatch.setattr(frappe, "db", types.SimpleNamespace(
			sql=sql_fn, db_type="postgres",
			savepoint=lambda *a, **k: None,
			release_savepoint=lambda *a, **k: None,
			rollback=rollback,
		), raising=False)
		with pytest.raises(ValueError, match="original query error"):
			PostgresDialect._safe_sql("EXPLAIN (FORMAT JSON) SELECT 1")

	def test_savepoint_names_are_unique_per_call(self, monkeypatch):
		"""Each _safe_sql call uses a distinct savepoint name, so a future nested
		caller can't RELEASE/ROLLBACK the wrong savepoint."""
		import frappe

		names: list[str] = []
		monkeypatch.setattr(frappe, "db", types.SimpleNamespace(
			sql=lambda *a, **k: [(1,)], db_type="postgres",
			savepoint=lambda name, *a, **k: names.append(name),
			release_savepoint=lambda *a, **k: None,
			rollback=lambda *a, **k: None,
		), raising=False)
		PostgresDialect._safe_sql("SELECT 1")
		PostgresDialect._safe_sql("SELECT 2")
		assert len(names) == 2
		assert names[0] != names[1]
		assert all(n.startswith("optimus_pg_q_") for n in names)
