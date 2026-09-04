# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Phase 2 foundation: the dialect contract + factory.

Covers the dialect-neutral pieces coercion helpers, normalized dataclass
defaults, db_type detection, factory dispatch + memoization, and the
type-classification each adapter ships with. The EXPLAIN/index/infra method
bodies are exercised by dialect-specific tests once they're filled.
"""

from __future__ import annotations

import pytest

from optimus import dbdialect
from optimus.dbdialect import base
from optimus.dbdialect.mariadb import MariaDBDialect
from optimus.dbdialect.postgres import PostgresDialect

# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------

class TestCoercion:
	def test_to_int(self):
		assert base.to_int(None) == 0
		assert base.to_int(5) == 5
		assert base.to_int(5.9) == 5
		assert base.to_int(True) == 1
		assert base.to_int("42") == 42
		assert base.to_int("42.7") == 42
		assert base.to_int("nope") == 0

	def test_to_float(self):
		assert base.to_float(None) is None
		assert base.to_float(3) == 3.0
		assert base.to_float("2.5") == 2.5
		assert base.to_float("nope") is None

	def test_to_int_or_none(self):
		# Infra metrics: absent / unparseable → None (not 0), so a missing
		# SHOW GLOBAL STATUS metric stays the InfraSnapshot 'absent' sentinel.
		assert base.to_int_or_none(None) is None
		assert base.to_int_or_none("12") == 12
		assert base.to_int_or_none(5) == 5
		assert base.to_int_or_none("nope") is None


# ---------------------------------------------------------------------------
# Normalized dataclasses
# ---------------------------------------------------------------------------

class TestShapes:
	def test_plantable_defaults(self):
		t = base.PlanTable(table="tabUser")
		assert t.full_scan is False and t.temp_used is False
		assert t.rows_examined == 0 and t.selectivity_pct is None
		assert t.raw == {}  # independent per-instance default

	def test_normalized_plan_defaults(self):
		p = base.NormalizedPlan()
		assert p.ok is True and p.tables == []


# ---------------------------------------------------------------------------
# Factory dispatch + memoization
# ---------------------------------------------------------------------------

class TestFactory:
	@pytest.fixture(autouse=True)
	def _clear_cache(self):
		dbdialect._cache.clear()
		yield
		dbdialect._cache.clear()

	def _set_db_type(self, monkeypatch, value):
		import types as _t

		import frappe
		monkeypatch.setattr(frappe, "db", _t.SimpleNamespace(db_type=value), raising=False)

	def test_mariadb_default(self, monkeypatch):
		self._set_db_type(monkeypatch, "mariadb")
		assert dbdialect.active_db_type() == "mariadb"
		assert isinstance(dbdialect.get_dialect(), MariaDBDialect)

	def test_postgres(self, monkeypatch):
		self._set_db_type(monkeypatch, "postgres")
		assert dbdialect.active_db_type() == "postgres"
		assert isinstance(dbdialect.get_dialect(), PostgresDialect)

	def test_unknown_falls_back_to_mariadb(self, monkeypatch):
		self._set_db_type(monkeypatch, "oracle")
		assert isinstance(dbdialect.get_dialect(), MariaDBDialect)

	def test_memoized_per_db_type(self, monkeypatch):
		self._set_db_type(monkeypatch, "mariadb")
		assert dbdialect.get_dialect() is dbdialect.get_dialect()


# ---------------------------------------------------------------------------
# Per-adapter type classification (the part already implemented)
# ---------------------------------------------------------------------------

class TestTypeClassification:
	def test_mariadb_prefix_and_unindexable(self):
		d = MariaDBDialect()
		assert d.prefix_required("text") and d.prefix_required("LONGTEXT")
		assert not d.prefix_required("varchar")
		assert d.unindexable("json") and not d.unindexable("int")

	def test_postgres_no_prefix_jsonb_unindexable(self):
		d = PostgresDialect()
		assert not d.prefix_required("text")  # PG has no prefix syntax
		assert d.unindexable("jsonb") and d.unindexable("json")
		assert not d.unindexable("text")


class TestQueryOptimizerCapability:
	"""Frappe's DBOptimizer (recorder._optimize_query) uses DESCRIBE / SHOW
	INDEX FROM, which only exist on MariaDB and abort the whole transaction on
	Postgres. index_suggestions gates on this flag so it never invokes the
	optimizer where it can't run."""

	def test_mariadb_supports_optimizer(self):
		assert MariaDBDialect().supports_query_optimizer is True

	def test_postgres_does_not_support_optimizer(self):
		assert PostgresDialect().supports_query_optimizer is False
