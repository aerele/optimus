# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""MariaDBDialect adapter the verbatim-lifted EXPLAIN / index / infra logic,
exercised with a fake ``frappe.db`` (no real site). Asserts the normalized
mappings match what the analyzers used to read off raw rows.
"""

from __future__ import annotations

import types

from optimus.dbdialect.mariadb import MariaDBDialect


def _install_db(monkeypatch, sql_fn):
	import frappe

	# frappe.db is a Werkzeug Local proxy replace it wholesale.
	monkeypatch.setattr(frappe, "db", types.SimpleNamespace(sql=sql_fn), raising=False)


class TestRunExplain:
	def test_full_scan_and_flags(self, monkeypatch):
		rows = [
			{"table": "tabGL Entry", "type": "ALL", "key": None, "rows": 12000,
			 "filtered": 5.0, "Extra": "Using where; Using filesort"},
			{"table": "tabAccount", "type": "ref", "key": "party", "rows": 1,
			 "filtered": 100.0, "Extra": ""},
		]
		_install_db(monkeypatch, lambda q, *a, **k: rows)
		plan = MariaDBDialect().run_explain("SELECT ...")
		assert plan.ok is True and len(plan.tables) == 2
		t0 = plan.tables[0]
		assert t0.table == "tabGL Entry" and t0.full_scan is True
		assert t0.sort_without_index is True and t0.temp_used is False
		assert t0.rows_examined == 12000 and t0.selectivity_pct == 5.0
		assert t0.raw["Extra"].startswith("Using where")  # raw kept for the report/LLM
		t1 = plan.tables[1]
		assert t1.full_scan is False and t1.used_index == "party"

	def test_temporary_table_flag(self, monkeypatch):
		rows = [{"table": "tabX", "type": "index", "rows": 500, "filtered": 100.0,
				 "Extra": "Using temporary; Using filesort"}]
		_install_db(monkeypatch, lambda q, *a, **k: rows)
		t = MariaDBDialect().run_explain("q").tables[0]
		assert t.temp_used is True and t.sort_without_index is True

	def test_explain_error_returns_not_ok(self, monkeypatch):
		def boom(*a, **k):
			raise RuntimeError("You have an error in your SQL syntax")
		_install_db(monkeypatch, boom)
		plan = MariaDBDialect().run_explain("bad")
		assert plan.ok is False and plan.tables == []


class TestExistingIndexes:
	def test_groups_orders_and_flags(self, monkeypatch):
		rows = [
			{"Key_name": "idx_ab", "Seq_in_index": 2, "Column_name": "b", "Non_unique": 1},
			{"Key_name": "idx_ab", "Seq_in_index": 1, "Column_name": "a", "Non_unique": 1},
			{"Key_name": "PRIMARY", "Seq_in_index": 1, "Column_name": "name", "Non_unique": 0},
		]
		_install_db(monkeypatch, lambda q, *a, **k: rows)
		by = {i.name: i for i in MariaDBDialect().existing_indexes("tabUser")}
		assert by["idx_ab"].columns == ["a", "b"]   # ordered by Seq_in_index
		assert by["idx_ab"].leftmost == "a" and by["idx_ab"].unique is False
		assert by["PRIMARY"].unique is True and by["PRIMARY"].leftmost == "name"

	def test_unsafe_name_never_reaches_db(self, monkeypatch):
		called = []
		_install_db(monkeypatch, lambda q, *a, **k: called.append(q) or [])
		assert MariaDBDialect().existing_indexes("tabUser; DROP TABLE x") == []
		assert called == []


class TestColumnTypesAndDdl:
	def test_column_types_lowercased(self, monkeypatch):
		rows = [{"column_name": "party", "data_type": "VARCHAR"},
				{"column_name": "amount", "data_type": "DECIMAL"}]
		_install_db(monkeypatch, lambda q, *a, **k: rows)
		assert MariaDBDialect().column_types("tabGL Entry") == {"party": "varchar", "amount": "decimal"}

	def test_index_ddl_plain_and_prefixed(self):
		d = MariaDBDialect()
		assert d.index_ddl("tabUser", "email", False) == (
			"ALTER TABLE `tabUser` ADD INDEX IF NOT EXISTS `email_index` (`email`);"
		)
		assert "(`bio`(255))" in d.index_ddl("tabUser", "bio", True)


class TestInfraSnapshot:
	def test_reads_status_and_caches_max_connections(self, monkeypatch):
		def fake_sql(query, *a, **k):
			if "GLOBAL STATUS" in query:
				return [("Threads_connected", "12"), ("Threads_running", "3"), ("Slow_queries", "7")]
			if "max_connections" in query:
				return [("max_connections", "151")]
			return []
		_install_db(monkeypatch, fake_sql)
		snap = MariaDBDialect().infra_snapshot()
		assert snap.threads_connected == 12 and snap.threads_running == 3
		assert snap.slow_queries == 7 and snap.max_connections == 151
