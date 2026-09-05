# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Database-dialect abstraction for the Optimus analysis engine.

A profiler's value slow-query / index / plan findings comes from running
EXPLAIN and inspecting the database's plan output, which is MariaDB-specific.
To support PostgreSQL too, every dialect-specific operation lives behind the
``Dialect`` interface and the analyzers consume the *normalized* dataclasses
below instead of touching raw EXPLAIN rows. ``optimus.dbdialect.get_dialect()``
returns the right adapter for the active ``frappe.db.db_type``.

This module is dialect-NEUTRAL no SQL here. The MariaDB / Postgres SQL lives
in ``mariadb.py`` / ``postgres.py``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Coercion helpers (shared by the adapters). Lifted from explain_flags so the
# adapters and the analyzer agree on how to read driver-variant numerics:
# certain drivers / EXPLAIN FORMAT variants return Decimal/str/None where an
# int/float is expected and a bare `>` comparison would crash.
# ---------------------------------------------------------------------------

def to_int(val) -> int:
	"""Coerce a plan numeric (MariaDB `rows`, PG `Plan Rows`) to int. Returns
	0 on any failure (None, unparseable string, unexpected type)."""
	if val is None:
		return 0
	if isinstance(val, bool):
		return int(val)
	if isinstance(val, int):
		return val
	if isinstance(val, float):
		return int(val)
	try:
		return int(val)
	except (TypeError, ValueError):
		try:
			return int(float(val))
		except (TypeError, ValueError):
			return 0


def to_float(val):
	"""Coerce a plan float (MariaDB `filtered`, PG-derived selectivity) to
	float. Returns None on failure so a threshold check can cleanly skip."""
	if val is None:
		return None
	if isinstance(val, bool):
		return float(val)
	if isinstance(val, (int, float)):
		return float(val)
	try:
		return float(val)
	except (TypeError, ValueError):
		return None


def to_int_or_none(val):
	"""Coerce an infra metric (a SHOW GLOBAL STATUS value) to int, returning
	None for an absent/unparseable value so a missing metric stays None (the
	InfraSnapshot 'absent' sentinel) rather than silently reading as 0."""
	if val is None:
		return None
	try:
		return int(val)
	except (TypeError, ValueError):
		return None


# ---------------------------------------------------------------------------
# Normalized shapes the analyzers consume (dialect-blind).
# ---------------------------------------------------------------------------

@dataclass
class PlanTable:
	"""One table's access pattern, normalized across dialects. Maps to a
	MariaDB EXPLAIN row OR a Postgres plan-tree scan node."""

	table: str
	full_scan: bool = False               # MariaDB type=='ALL'; PG 'Seq Scan'
	sort_without_index: bool = False      # MariaDB 'Using filesort'; PG a 'Sort' node
	temp_used: bool = False               # MariaDB 'Using temporary'; PG HashAggregate/Materialize
	used_index: str | None = None         # MariaDB `key`; PG Index Scan 'Index Name'
	rows_examined: int = 0                # MariaDB `rows`; PG 'Plan Rows'
	selectivity_pct: float | None = None  # MariaDB `filtered`; PG-derived, else None
	raw: dict = field(default_factory=dict)  # dialect blob: report's explain detail + LLM context


@dataclass
class NormalizedPlan:
	tables: list = field(default_factory=list)   # list[PlanTable]
	ok: bool = True                              # False → EXPLAIN failed/unsupported; analyzers skip
	raw: object = None                           # full raw EXPLAIN output for the report


@dataclass
class IndexInfo:
	name: str
	columns: list                  # ordered by sequence-in-index
	unique: bool = False
	leftmost: str | None = None    # columns[0] if any the col a b-tree accelerates a single-col filter on


@dataclass
class InfraSnapshot:
	threads_connected: int | None = None
	threads_running: int | None = None
	slow_queries: int | None = None       # None on Postgres without pg_stat_statements
	max_connections: int | None = None


# ---------------------------------------------------------------------------
# The dialect interface.
# ---------------------------------------------------------------------------

class Dialect(ABC):
	"""Per-database adapter. Analyzers call these; they never see raw SQL."""

	name: str = "base"

	# Whether Frappe's own query optimizer (``recorder._optimize_query`` /
	# ``DBOptimizer``) can run on this database. That optimizer fetches table
	# stats with MariaDB-only introspection (``DESCRIBE`` / ``SHOW INDEX FROM``),
	# so on Postgres it raises a syntax error that ABORTS the whole transaction
	# (Postgres poisons the txn on any failed statement every later query then
	# fails until rollback). The index-suggestions analyzer gates on this flag so
	# it never invokes the optimizer on a dialect that can't run it. True by
	# default (MariaDB); Postgres overrides to False until a PG-native table-stats
	# provider exists.
	supports_query_optimizer: bool = True

	@abstractmethod
	def run_explain(self, query: str) -> NormalizedPlan:
		"""Run a plan-only EXPLAIN (NEVER one that executes the statement) and
		return the normalized plan. ``ok=False`` on any failure."""

	@abstractmethod
	def existing_indexes(self, table: str) -> list:
		"""``list[IndexInfo]`` for ``table`` (empty on failure / no DB)."""

	@abstractmethod
	def column_types(self, table: str) -> dict:
		"""``{column: data_type_lower}`` for ``table`` (empty on failure)."""

	@abstractmethod
	def index_ddl(self, table: str, column: str, is_text_col: bool) -> str:
		"""A single-column ADD-INDEX / CREATE INDEX statement (the operator-
		facing suggestion text)."""

	@abstractmethod
	def infra_snapshot(self) -> InfraSnapshot:
		"""Live DB connection / load counters; any field may be ``None``."""

	# Shared, non-abstract type classification (overridable per dialect).
	def prefix_required(self, data_type: str) -> bool:
		"""True when indexing this column type needs a key-length prefix
		(MariaDB TEXT/BLOB). False on Postgres (no prefix syntax)."""
		return False

	def unindexable(self, data_type: str) -> bool:
		"""True when a plain b-tree index can't be suggested for this type
		(json / geometry and friends)."""
		return False
