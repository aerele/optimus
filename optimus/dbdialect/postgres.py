# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""PostgreSQL dialect adapter.

Type-classification is in place (no prefix syntax; json/jsonb/geometry need a
GIN/GiST index, not a plain b-tree). The EXPLAIN (FORMAT JSON) plan-tree walk,
``pg_index``/``pg_stat_activity`` introspection, and ``CREATE INDEX`` DDL are
implemented in Phase 3.
"""

from __future__ import annotations

from optimus.dbdialect.base import Dialect, InfraSnapshot, NormalizedPlan

# Postgres has no index-prefix syntax; jsonb/geometry need GIN/GiST (a plain
# b-tree CREATE INDEX is useless or errors), so don't suggest one.
_UNINDEXABLE_TYPES = frozenset({"json", "jsonb", "geometry"})

_TODO = "PostgresDialect.{} — implemented in Phase 3 (Postgres adapter)"


class PostgresDialect(Dialect):
	name = "postgres"

	def run_explain(self, query: str) -> NormalizedPlan:
		raise NotImplementedError(_TODO.format("run_explain"))

	def existing_indexes(self, table: str) -> list:
		raise NotImplementedError(_TODO.format("existing_indexes"))

	def column_types(self, table: str) -> dict:
		raise NotImplementedError(_TODO.format("column_types"))

	def index_ddl(self, table: str, column: str, is_text_col: bool) -> str:
		raise NotImplementedError(_TODO.format("index_ddl"))

	def infra_snapshot(self) -> InfraSnapshot:
		raise NotImplementedError(_TODO.format("infra_snapshot"))

	def prefix_required(self, data_type: str) -> bool:
		return False  # Postgres b-tree indexes the full value — no prefix syntax

	def unindexable(self, data_type: str) -> bool:
		return (data_type or "").lower() in _UNINDEXABLE_TYPES
