# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Would a suggested index hurt other flows? Compares session-wide write counts with helped reads."""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from optimus.analyzers.index_where import mask_strings, strip_leading_comments

HOT_COLUMN_MIN_UPDATES = 5
WRITE_HEAVY_MIN_WRITES = 50
WRITE_HEAVY_RATIO = 10
CAUTION_MIN_WRITES = 10
LOW_CARDINALITY_MAX = 10
MAX_FLOWS_NAMED = 3

_TABLE = r"(?:`([^`]+)`|\"([^\"]+)\"|(tab\w+))"
_UPDATE_RE = re.compile(r"^UPDATE\s+(?:LOW_PRIORITY\s+|IGNORE\s+)*" + _TABLE + r"(?:\s+(?:AS\s+)?(?!SET\b)\w+)?\s+SET\s+(.*)", re.I | re.S)
_INSERT_RE = re.compile(r"^(?:INSERT|REPLACE)\s+(?:IGNORE\s+)?INTO\s+" + _TABLE, re.I | re.S)
_DELETE_RE = re.compile(r"^DELETE\s+FROM\s+" + _TABLE, re.I | re.S)
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
			if not isinstance(call, dict):
				continue
			sql = call.get("query") or call.get("normalized_query") or ""
			if isinstance(sql, str):
				_record_statement(strip_leading_comments(sql), flow, out)
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
			f"`{column}` on `{table}` is updated by {col_updates} statement(s) in this session "
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
			f"{col_updates} statement(s) in this session update `{column}` "
			f"({_flows(tw.column_flows.get(column.lower()))}); each would also have to maintain this index."
		)
	if tw.total >= CAUTION_MIN_WRITES:
		cautions.append(
			f"`{table}` is written {tw.total} time(s) in this session "
			f"({tw.inserts} insert, {tw.updates} update, {tw.deletes} delete; {_flows(tw.flows)}); "
			"each insert and delete would also maintain this index."
		)
	distinct = _as_number(cardinality)
	if distinct is not None and 0 < distinct <= LOW_CARDINALITY_MAX:
		cautions.append(
			f"`{column}` has only {int(distinct)} distinct value(s), so the index may not narrow rows "
			"much; check the value distribution before adding it."
		)
	return cautions


def _as_number(value) -> float | None:
	"""``value`` as a float if it is numeric (int, float, Decimal or numeric string), else None."""
	try:
		return None if value is None else float(value)
	except (TypeError, ValueError):
		return None


def _flows(counter) -> str:
	if not counter:
		return "no named flow"
	return "flows: " + ", ".join(f"{name} ({n})" for name, n in counter.most_common(MAX_FLOWS_NAMED))


def _table_of(m: re.Match) -> str:
	return m.group(1) or m.group(2) or m.group(3)


def _record_statement(sql: str, flow: str, out: dict) -> None:
	if m := _UPDATE_RE.match(sql):
		tw = out[_table_of(m)]
		tw.updates += 1
		tw.flows[flow] += 1
		for col in _set_columns(m.group(4)):
			tw.updated_columns[col] += 1
			tw.column_flows[col][flow] += 1
	elif m := _INSERT_RE.match(sql):
		tw = out[_table_of(m)]
		tw.inserts += 1
		tw.flows[flow] += 1
	elif m := _DELETE_RE.match(sql):
		tw = out[_table_of(m)]
		tw.deletes += 1
		tw.flows[flow] += 1


def _set_columns(set_clause: str) -> list[str]:
	set_clause = mask_strings(set_clause, lambda _: "'?'")
	clause = set_clause[: _set_end(set_clause)]
	cols, depth, start = [], 0, 0
	for i, ch in enumerate(clause + ","):
		depth += (ch == "(") - (ch == ")")
		if ch == "," and depth == 0:
			if m := _SET_COL_RE.match(clause[start:i]):
				cols.append(m.group(1).lower())
			start = i + 1
	return cols


def _set_end(text: str) -> int:
	"""Index where the SET list ends: the first WHERE/ORDER BY/LIMIT outside any parentheses."""
	depth, depths = 0, []
	for ch in text:
		depth -= ch == ")"
		depths.append(depth)
		depth += ch == "("
	for m in _SET_END_RE.finditer(text):
		if depths[m.start()] == 0:
			return m.start()
	return len(text)
