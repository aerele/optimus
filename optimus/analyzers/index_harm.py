# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Would a suggested index hurt other flows? Compares session-wide write counts with helped reads."""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from optimus.analyzers.index_where import STRING_RE

HOT_COLUMN_MIN_UPDATES = 5
WRITE_HEAVY_MIN_WRITES = 50
WRITE_HEAVY_RATIO = 10
CAUTION_MIN_WRITES = 10
LOW_CARDINALITY_MAX = 10
MAX_FLOWS_NAMED = 3

_TABLE = r"(?:`([^`]+)`|(tab\w+))"
_LEAD = r"^\s*(?:/\*.*?\*/\s*)*"
_UPDATE_RE = re.compile(_LEAD + r"UPDATE\s+(?:LOW_PRIORITY\s+|IGNORE\s+)*" + _TABLE + r"\s+SET\s+(.*)", re.I | re.S)
_INSERT_RE = re.compile(_LEAD + r"(?:INSERT|REPLACE)\s+(?:IGNORE\s+)?INTO\s+" + _TABLE, re.I | re.S)
_DELETE_RE = re.compile(_LEAD + r"DELETE\s+FROM\s+" + _TABLE, re.I | re.S)
_SET_END_RE = re.compile(r"\bWHERE\b|\bORDER\s+BY\b|\bLIMIT\b", re.I)
_SET_COL_RE = re.compile(r"^\s*(?:`?\w+`?\.)?`?(\w+)`?\s*=")


@dataclass
class TableWrites:
	inserts: int = 0
	updates: int = 0
	deletes: int = 0
	updated_columns: Counter = field(default_factory=Counter)
	flows: Counter = field(default_factory=Counter)  # writing flow -> statements
	column_flows: dict = field(default_factory=lambda: defaultdict(Counter))

	@property
	def total(self) -> int:
		return self.inserts + self.updates + self.deletes


@dataclass
class HarmAssessment:
	suppress_reason: str | None = None
	cautions: list = field(default_factory=list)
	stats: dict = field(default_factory=dict)


def collect_writes(recordings: list[dict]) -> dict[str, TableWrites]:
	"""Per-table write counts over every call of every recording."""
	out: dict[str, TableWrites] = defaultdict(TableWrites)
	for rec in recordings:
		flow = str(rec.get("cmd") or rec.get("path") or rec.get("uuid") or "unknown")[:80]
		for call in rec.get("calls") or []:
			sql = call.get("query") or call.get("normalized_query") or ""
			_record_statement(sql, flow, out)
	return dict(out)


def assess_harm(table: str, column: str, benefit_count: int, writes: dict, cardinality=None) -> HarmAssessment:
	"""Suppress or caution an index on ``table.column`` by its write cost."""
	tw = writes.get(table) or TableWrites()
	col = (column or "").lower()
	col_updates = tw.updated_columns.get(col, 0)
	result = HarmAssessment(
		stats={
			"table_writes": tw.total,
			"table_inserts": tw.inserts,
			"table_updates": tw.updates,
			"table_deletes": tw.deletes,
			"column_updates": col_updates,
			"helped_queries": benefit_count,
			"cardinality": cardinality,
		}
	)
	if col_updates >= HOT_COLUMN_MIN_UPDATES and col_updates >= benefit_count:
		result.suppress_reason = (
			f"`{column}` on `{table}` is UPDATEd by {col_updates} statement(s) in this session "
			f"({_flows(tw.column_flows.get(col))}) versus {benefit_count} read(s) the index would "
			"speed up. Every one of those updates would also maintain the index."
		)
	elif tw.total >= WRITE_HEAVY_MIN_WRITES and tw.total >= WRITE_HEAVY_RATIO * benefit_count:
		result.suppress_reason = (
			f"`{table}` is written {tw.total} time(s) in this session ({_flows(tw.flows)}) versus "
			f"{benefit_count} read(s) the index would speed up. Every insert and delete would also "
			"maintain the index."
		)
	else:
		result.cautions = _cautions(table, column, tw, col_updates, cardinality)
	return result


def _cautions(table, column, tw: TableWrites, col_updates: int, cardinality) -> list[str]:
	cautions = []
	if col_updates:
		cautions.append(
			f"{col_updates} statement(s) in this session UPDATE `{column}` "
			f"({_flows(tw.column_flows.get(column.lower()))}); each would also maintain this index."
		)
	if tw.total >= CAUTION_MIN_WRITES:
		cautions.append(
			f"`{table}` is written {tw.total} time(s) in this session "
			f"({tw.inserts} insert, {tw.updates} update, {tw.deletes} delete; {_flows(tw.flows)}); "
			"each insert and delete would also maintain this index."
		)
	if isinstance(cardinality, int) and 0 < cardinality <= LOW_CARDINALITY_MAX:
		cautions.append(
			f"`{column}` has only {cardinality} distinct value(s), so the index may not narrow rows "
			"much; check the value distribution before adding it."
		)
	return cautions


def _flows(counter) -> str:
	if not counter:
		return "no named flow"
	return "flows: " + ", ".join(f"{name} x{n}" for name, n in counter.most_common(MAX_FLOWS_NAMED))


def _record_statement(sql: str, flow: str, out: dict) -> None:
	if m := _UPDATE_RE.match(sql):
		tw = out[(m.group(1) or m.group(2))]
		tw.updates += 1
		tw.flows[flow] += 1
		for col in _set_columns(m.group(3)):
			tw.updated_columns[col] += 1
			tw.column_flows[col][flow] += 1
	elif m := _INSERT_RE.match(sql):
		tw = out[(m.group(1) or m.group(2))]
		tw.inserts += 1
		tw.flows[flow] += 1
	elif m := _DELETE_RE.match(sql):
		tw = out[(m.group(1) or m.group(2))]
		tw.deletes += 1
		tw.flows[flow] += 1


def _set_columns(set_clause: str) -> list[str]:
	set_clause = STRING_RE.sub("'?'", set_clause)
	end = _SET_END_RE.search(set_clause)
	clause = set_clause[: end.start()] if end else set_clause
	cols, depth, start = [], 0, 0
	for i, ch in enumerate(clause + ","):
		depth += (ch == "(") - (ch == ")")
		if ch == "," and depth == 0:
			if m := _SET_COL_RE.match(clause[start:i]):
				cols.append(m.group(1).lower())
			start = i + 1
	return cols
