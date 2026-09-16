# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""PostgreSQL dialect adapter.

Maps Postgres plan/catalog output onto the normalized shapes the analyzers
consume, so the analysis engine is dialect-blind. Plan analysis uses
``EXPLAIN (FORMAT JSON)`` (plan-only, NEVER ``EXPLAIN ANALYZE``, which would
execute the user's statement). Sort / temp-table flags are plan-only
heuristics: a ``Sort`` node is the filesort analog, ``HashAggregate`` /
``Materialize`` the temporary-table analog. ``selectivity_pct`` has no
plan-only equivalent (it needs ANALYZE row counts), so it's left None and the
Low Filter Ratio finding simply doesn't fire.
"""

from __future__ import annotations

import itertools
import json

from optimus.dbdialect.base import (
	Dialect,
	IndexInfo,
	InfraSnapshot,
	NormalizedPlan,
	PlanTable,
	to_int,
	to_int_or_none,
)

# json / jsonb / geometry can't take a plain b-tree (need GIN/GiST); don't suggest one.
_UNINDEXABLE_TYPES = frozenset({"json", "jsonb", "geometry"})

# Plan node types (EXPLAIN FORMAT JSON "Node Type").
_SCAN_NODES = frozenset({
	"Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan", "Tid Scan",
})
_SORT_NODES = frozenset({"Sort", "Incremental Sort"})          # filesort analog
_TEMP_NODES = frozenset({"HashAggregate", "Materialize"})      # temporary-table analog

# Monotonic sequence for unique per-call savepoint names (see _safe_sql).
# next() on an itertools.count() is atomic in CPython, so it's thread-safe.
_savepoint_seq = itertools.count()

# Ordered-column index introspection. ``indkey`` is an int2vector of attnums;
# unnest it WITH ORDINALITY (via text → int[]) to recover column order. attnum 0
# = an expression (functional index) → no pg_attribute match → dropped.
_PG_INDEX_SQL = """
	SELECT i.relname AS index_name, a.attname AS column_name,
	       k.ord AS seq, ix.indisunique AS is_unique
	FROM pg_index ix
	JOIN pg_class t ON t.oid = ix.indrelid
	JOIN pg_class i ON i.oid = ix.indexrelid
	JOIN pg_namespace n ON n.oid = t.relnamespace
	CROSS JOIN LATERAL unnest(string_to_array(ix.indkey::text, ' ')::int[])
	     WITH ORDINALITY AS k(attnum, ord)
	JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
	WHERE t.relname = %s AND n.nspname = %s
	ORDER BY i.relname, k.ord
"""


def _db_schema() -> str:
	"""The schema Frappe pins on the Postgres connection (default 'public')."""
	try:
		import frappe
		return frappe.conf.get("db_schema") or "public"
	except Exception:
		return "public"


def _walk_plan(root: dict) -> list:
	"""Walk the EXPLAIN plan tree, one PlanTable per scanned relation. Sort /
	temp flags from ancestor nodes propagate down to the scans they cover.

	BY DESIGN an ancestor Sort/temp flag attaches to EVERY scan beneath it (in
	``Sort → Join → (Scan A, Scan B)`` both A and B are tagged
	``sort_without_index``): Postgres's plan tree doesn't decompose a sort back
	to a single relation. The over-attribution is bounded: ``selectivity_pct``
	is None on PG so Low Filter Ratio never fires and the Filesort finding still
	needs a large ``rows_examined``."""
	tables: list = []

	def visit(node, sort_above, temp_above):
		nt = node.get("Node Type", "")
		sort_here = sort_above or nt in _SORT_NODES
		temp_here = temp_above or nt in _TEMP_NODES
		if nt in _SCAN_NODES:
			raw = dict(node)
			raw["filtered"] = None  # no plan-only selectivity; keep key for dialect-agnostic downstream
			tables.append(PlanTable(
				table=node.get("Relation Name") or node.get("Alias") or "?",
				full_scan=(nt == "Seq Scan"),
				sort_without_index=sort_here,
				temp_used=temp_here,
				used_index=node.get("Index Name"),
				rows_examined=to_int(node.get("Plan Rows")),
				selectivity_pct=None,
				raw=raw,
			))
		for child in (node.get("Plans") or []):
			visit(child, sort_here, temp_here)

	visit(root, False, False)
	return tables


class PostgresDialect(Dialect):
	name = "postgres"

	# Frappe's DBOptimizer (recorder._optimize_query) introspects table stats
	# with DESCRIBE / SHOW INDEX FROM, which don't exist on Postgres (and a
	# failed statement aborts the whole PG transaction). Until a PG-native
	# table-stats provider exists, the index-suggestions analyzer skips the
	# optimizer on Postgres rather than crash the analyze. The plan/EXPLAIN-based
	# findings (Seq Scan → Full Table Scan, Sort, …) are unaffected.
	supports_query_optimizer = False

	def __init__(self) -> None:
		self._max_connections_cached: int | None = None

	@staticmethod
	def _safe_sql(*args, **kwargs):
		"""Run a query under a savepoint and roll back to it on failure.

		CRITICAL on Postgres: a failed query aborts the WHOLE transaction, so
		catching the Python exception is not enough. The savepoint rollback
		restores the transaction so analyze and the request survive a query the
		database rejects. Re-raises so the caller's try/except still returns its
		safe default."""
		import frappe

		# Unique savepoint name per call: no two _safe_sql invocations share a
		# name, so if a future caller ever nests one inside another's open
		# savepoint the RELEASE/ROLLBACK can't target the wrong (inner) savepoint.
		sp = f"optimus_pg_q_{next(_savepoint_seq)}"
		frappe.db.savepoint(sp)
		try:
			result = frappe.db.sql(*args, **kwargs)
		except Exception:
			# Roll back to the savepoint so the failed statement doesn't poison
			# the rest of the transaction but never let a rollback error mask
			# the ORIGINAL exception (the caller's try/except turns the re-raised
			# original into its safe default).
			try:
				frappe.db.rollback(save_point=sp)
			except Exception:
				pass
			raise
		frappe.db.release_savepoint(sp)
		return result

	def run_explain(self, query: str) -> NormalizedPlan:
		try:
			rows = self._safe_sql(f"EXPLAIN (FORMAT JSON) {query}")
		except Exception:
			return NormalizedPlan(ok=False, tables=[], raw=None)
		try:
			cell = rows[0][0] if rows and rows[0] else None
			plan_json = json.loads(cell) if isinstance(cell, (str, bytes)) else cell
			root = plan_json[0]["Plan"] if plan_json else None
		except Exception:
			return NormalizedPlan(ok=False, tables=[], raw=rows)
		if not isinstance(root, dict):
			return NormalizedPlan(ok=False, tables=[], raw=plan_json)
		return NormalizedPlan(ok=True, tables=_walk_plan(root), raw=plan_json)

	def existing_indexes(self, table: str) -> list:
		try:
			rows = self._safe_sql(_PG_INDEX_SQL, (table, _db_schema()), as_dict=True) or []
		except Exception:
			return []
		by_name: dict[str, dict] = {}
		for r in rows:
			name = r.get("index_name")
			if not name:
				continue
			entry = by_name.setdefault(name, {"cols": [], "unique": bool(r.get("is_unique"))})
			entry["cols"].append((to_int(r.get("seq")), r.get("column_name")))
		out: list = []
		for name, e in by_name.items():
			cols = [c for _s, c in sorted(e["cols"]) if c]
			out.append(IndexInfo(name=name, columns=cols, unique=e["unique"],
			                     leftmost=(cols[0] if cols else None)))
		return out

	def column_types(self, table: str) -> dict:
		try:
			rows = self._safe_sql(
				"""
				SELECT column_name, data_type
				FROM information_schema.columns
				WHERE table_schema = %s AND table_name = %s
				""",
				(_db_schema(), table),
				as_dict=True,
			) or []
		except Exception:
			return {}
		out: dict = {}
		for r in rows:
			col = r.get("column_name")
			dtype = (r.get("data_type") or "").lower()
			if col:
				out[col] = dtype
		return out

	def index_ddl(self, table: str, column: str, is_text_col: bool) -> str:
		# Postgres b-trees index the full value no prefix length. Long/searched
		# text may want an expression or GIN/trigram index instead.
		schema = _db_schema()
		return (
			f'CREATE INDEX IF NOT EXISTS "{table}_{column}_index" '
			f'ON "{schema}"."{table}" ("{column}");'
		)

	def infra_snapshot(self) -> InfraSnapshot:
		snap = InfraSnapshot()
		try:
			r = self._safe_sql(
				"SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
			)
			snap.threads_connected = to_int_or_none(r[0][0]) if r else None
		except Exception:
			pass
		try:
			r = self._safe_sql(
				"SELECT count(*) FROM pg_stat_activity "
				"WHERE datname = current_database() AND state = 'active'"
			)
			snap.threads_running = to_int_or_none(r[0][0]) if r else None
		except Exception:
			pass
		# slow_queries: no global counter without pg_stat_statements → stays None.
		if self._max_connections_cached is None:
			try:
				r = self._safe_sql("SELECT setting FROM pg_settings WHERE name = 'max_connections'")
				if r:
					self._max_connections_cached = to_int_or_none(r[0][0])
			except Exception:
				pass
		snap.max_connections = self._max_connections_cached
		return snap

	def prefix_required(self, data_type: str) -> bool:
		return False  # Postgres b-tree indexes the full value no prefix syntax

	def unindexable(self, data_type: str) -> bool:
		return (data_type or "").lower() in _UNINDEXABLE_TYPES
