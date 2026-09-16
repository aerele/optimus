# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""MariaDB dialect adapter (EXPLAIN, index introspection, infra metrics) behind the Dialect interface."""

from __future__ import annotations

import re

from optimus.dbdialect.base import (
	Dialect,
	IndexInfo,
	InfraSnapshot,
	NormalizedPlan,
	PlanTable,
	to_float,
	to_int,
	to_int_or_none,
)

# MariaDB TEXT/BLOB columns need a key-length prefix to index; JSON/GEOMETRY
# can't take a plain b-tree index. (Mirrors index_suggestions de-duplicated
# when that analyzer is rewired to call the dialect.)
_PREFIX_REQUIRED_TYPES = frozenset({
	"text", "tinytext", "mediumtext", "longtext",
	"blob", "tinyblob", "mediumblob", "longblob",
})
_UNINDEXABLE_TYPES = frozenset({"json", "geometry"})
_TEXT_INDEX_PREFIX_LENGTH = 255  # index_suggestions.TEXT_INDEX_PREFIX_LENGTH

# MariaDB can't parameterise an identifier in ``SHOW INDEX FROM ?``: the table
# name is interpolated, so it must pass this whitelist first (index_suggestions
# ._SAFE_TAB_TABLE_RE / _SAFE_INFOSCHEMA_RE / _is_safe_table_name).
_SAFE_TAB_TABLE_RE = re.compile(r"^tab[A-Za-z0-9 _\-]+$")
_SAFE_INFOSCHEMA_RE = re.compile(r"^information_schema\.[A-Za-z0-9_]+$")


def _is_safe_table_name(name) -> bool:
	if not isinstance(name, str) or not name:
		return False
	if any(c in name for c in ("`", "'", '"', ";", "\\", "\n", "\r", "\x00")):
		return False
	return bool(_SAFE_TAB_TABLE_RE.match(name) or _SAFE_INFOSCHEMA_RE.match(name))


class MariaDBDialect(Dialect):
	name = "mariadb"

	def __init__(self) -> None:
		# max_connections is a server config cached for the adapter's life
		# (the factory keeps one adapter per process). Mirrors the old
		# module-level infra_capture._db_max_connections_cached.
		self._max_connections_cached: int | None = None

	# -- EXPLAIN ---------------------------------------------------------

	def run_explain(self, query: str) -> NormalizedPlan:
		import frappe

		try:
			rows = frappe.db.sql(f"EXPLAIN {query}", as_dict=True) or []
		except Exception:
			return NormalizedPlan(ok=False, tables=[], raw=[])
		return NormalizedPlan(
			ok=True, tables=[self._row_to_plan_table(r) for r in rows], raw=rows
		)

	@staticmethod
	def _row_to_plan_table(row: dict) -> PlanTable:
		"""Map one MariaDB EXPLAIN row to a normalized PlanTable."""
		extra = (row.get("Extra") or row.get("extra") or "").lower()
		return PlanTable(
			table=row.get("table") or "?",
			full_scan=(row.get("type") or "").lower() == "all",
			sort_without_index="using filesort" in extra,
			temp_used="using temporary" in extra,
			used_index=row.get("key"),
			rows_examined=to_int(row.get("rows")),
			selectivity_pct=to_float(row.get("filtered")),
			raw=dict(row),
		)

	# -- Index introspection --------------------------------------------

	def existing_indexes(self, table: str) -> list:
		"""``SHOW INDEX FROM `table`` → ``[IndexInfo(...)]``. Empty on any error or unsafe name."""
		if not _is_safe_table_name(table):
			return []
		import frappe

		try:
			rows = frappe.db.sql(f"SHOW INDEX FROM `{table}`", as_dict=True) or []
		except Exception:
			return []

		by_name: dict[str, dict] = {}
		for r in rows:
			key_name = r.get("Key_name")
			if not key_name:
				continue
			entry = by_name.setdefault(
				key_name, {"_cols": [], "unique": not r.get("Non_unique")}
			)
			try:
				seq = int(r.get("Seq_in_index") or r.get("seq_in_index") or 0)
			except (TypeError, ValueError):
				seq = 0
			entry["_cols"].append((seq, r.get("Column_name") or r.get("column_name")))

		out: list = []
		for key_name, e in by_name.items():
			cols = [c for _seq, c in sorted(e["_cols"]) if c]
			out.append(
				IndexInfo(
					name=key_name,
					columns=cols,
					unique=bool(e["unique"]),
					leftmost=(cols[0] if cols else None),
				)
			)
		return out

	def column_types(self, table: str) -> dict:
		"""``{column: data_type_lower}`` for the table's columns."""
		import frappe

		try:
			rows = frappe.db.sql(
				"""
				SELECT column_name, data_type
				FROM information_schema.columns
				WHERE table_schema = DATABASE() AND table_name = %s
				""",
				(table,),
				as_dict=True,
			) or []
		except Exception:
			return {}

		out: dict = {}
		for r in rows:
			col = r.get("column_name") or r.get("COLUMN_NAME")
			dtype = (r.get("data_type") or r.get("DATA_TYPE") or "").lower()
			if col:
				out[col] = dtype
		return out

	def index_ddl(self, table: str, column: str, is_text_col: bool) -> str:
		"""ADD INDEX statement for ``column`` on ``table``. TEXT/BLOB columns get the (255)-prefix form."""
		if is_text_col:
			return (
				f"ALTER TABLE `{table}` ADD INDEX IF NOT EXISTS `{column}_index` "
				f"(`{column}`({_TEXT_INDEX_PREFIX_LENGTH}));"
			)
		return f"ALTER TABLE `{table}` ADD INDEX IF NOT EXISTS `{column}_index` (`{column}`);"

	# -- Infra metrics ---------------------------------------------------

	def infra_snapshot(self) -> InfraSnapshot:
		"""Connection / load counters (lifted from infra_capture._read_db)."""
		import frappe

		snap = InfraSnapshot()
		try:
			rows = frappe.db.sql(
				"SHOW GLOBAL STATUS WHERE Variable_name IN "
				"('Threads_connected', 'Threads_running', 'Slow_queries')"
			)
			status = {nm: val for nm, val in rows}
			# Absent/unparseable metric → None (matches the old _read_db),
			# never a misleading 0.
			snap.threads_connected = to_int_or_none(status.get("Threads_connected"))
			snap.threads_running = to_int_or_none(status.get("Threads_running"))
			snap.slow_queries = to_int_or_none(status.get("Slow_queries"))
		except Exception:
			pass

		if self._max_connections_cached is None:
			try:
				var_rows = frappe.db.sql(
					"SHOW VARIABLES WHERE Variable_name = 'max_connections'"
				)
				if var_rows:
					self._max_connections_cached = to_int_or_none(var_rows[0][1])
			except Exception:
				pass
		snap.max_connections = self._max_connections_cached
		return snap

	# -- Type classification --------------------------------------------

	def prefix_required(self, data_type: str) -> bool:
		return (data_type or "").lower() in _PREFIX_REQUIRED_TYPES

	def unindexable(self, data_type: str) -> bool:
		return (data_type or "").lower() in _UNINDEXABLE_TYPES
