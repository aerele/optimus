# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Scenario matrix for index-suggestion safety checks, run through a fake dialect (no site)."""

import contextlib
import json
import sys
import time
import types
from collections import Counter
from decimal import Decimal

import pytest

from optimus.analyzers import index_harm, index_suggestions
from optimus.analyzers.index_where import is_served, is_weak_only, parse_where
from optimus.dbdialect.base import IndexInfo
from optimus.dbdialect.mariadb import MariaDBDialect

TABLE = "tabT"
COLUMNS = ("a", "b", "c", "x", "y")


def ix(name, cols, unique=False, eligible=True, usable_prefix=()):
	return IndexInfo(
		name=name, columns=list(cols), unique=unique, leftmost=cols[0], eligible=eligible,
		usable_prefix=tuple(usable_prefix),
	)


def unusable(name, cols, usable_prefix=()):
	"""An index the optimizer can't use for lookups (FULLTEXT, HASH, ignored, prefix part)."""
	return ix(name, cols, eligible=False, usable_prefix=usable_prefix)


class _Suggested:
	def __init__(self, table, column, cardinality=None):
		self.table, self.column, self.cardinality = table, column, cardinality


class _FakeDialect(MariaDBDialect):
	def __init__(self, indexes=(), with_schema=True):
		self._indexes = list(indexes)
		self._schema = {c: "varchar" for c in COLUMNS} if with_schema else {}

	def existing_indexes(self, table):
		return list(self._indexes)

	def column_types(self, table):
		return dict(self._schema)

	def statement_time_limit(self, seconds):
		return contextlib.nullcontext()


def _install(monkeypatch, column, indexes=(), with_schema=True, cardinality=None):
	for name in (
		"frappe",
		"frappe.core",
		"frappe.core.doctype",
		"frappe.core.doctype.recorder",
		"frappe.core.doctype.recorder.recorder",
	):
		if name not in sys.modules:
			monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
	recorder = sys.modules["frappe.core.doctype.recorder.recorder"]
	monkeypatch.setattr(
		recorder, "_optimize_query", lambda q: _Suggested(TABLE, column, cardinality), raising=False
	)
	monkeypatch.setattr(sys.modules["frappe"], "log_error", lambda **kw: None, raising=False)
	dialect = _FakeDialect(indexes, with_schema)
	monkeypatch.setattr(index_suggestions, "get_dialect", lambda: dialect)


def _select(where, n=1, table=TABLE):
	sql = f"SELECT * FROM `{table}` WHERE {where}"
	return [{"query": sql, "normalized_query": sql, "duration": 30.0, "stack": []} for _ in range(n)]


def _writes(**counts):
	"""Write calls: ``inserts``, ``deletes`` and ``update_<col>`` counts."""
	calls = []
	sql_for = {"inserts": f"INSERT INTO `{TABLE}` (a, b) VALUES (?, ?)", "deletes": f"DELETE FROM `{TABLE}` WHERE a = ?"}
	for key, n in counts.items():
		sql = sql_for.get(key) or f"UPDATE `{TABLE}` SET {key.removeprefix('update_')} = ? WHERE a = ?"
		calls += [{"query": sql, "normalized_query": sql, "duration": 1.0, "stack": []} for _ in range(n)]
	return calls


def _analyze(empty_context, *call_groups, flow="/api/method/reads"):
	calls = [c for group in call_groups for c in group]
	recording = {"uuid": "r1", "path": flow, "cmd": None, "calls": calls}
	return index_suggestions.analyze([recording], empty_context)


def _missing(result):
	return [f for f in result.findings if f["finding_type"] == "Missing Index"]


def _detail(finding):
	return json.loads(finding["technical_detail_json"])


# ---------------------------------------------------------------------------
# 1. Existing-index awareness (single, composite, types, query shapes)
# ---------------------------------------------------------------------------

# (scenario id, suggested column, existing indexes, WHERE clause, should the suggestion survive)
COVERAGE_SCENARIOS = [
	("no_index_plain_equality", "b", [], "b = 'x'", True),
	("existing_single_index_on_same_column", "b", [ix("idx_b", ["b"])], "b = 'x'", False),
	("existing_single_index_on_other_column", "b", [ix("idx_a", ["a"])], "a = 'x' AND b = 'y'", True),
	("composite_ab_query_filters_a_and_b_by_equality", "b", [ix("ab", ["a", "b"])], "a = 'x' AND b = 'y'", False),
	("composite_ab_query_filters_only_b", "b", [ix("ab", ["a", "b"])], "b = 'y'", True),
	("composite_ab_with_range_on_leading_a", "b", [ix("ab", ["a", "b"])], "a > 5 AND b = 'y'", True),
	("composite_ab_with_in_on_leading_a", "b", [ix("ab", ["a", "b"])], "a IN ('p', 'q') AND b = 'y'", False),
	("composite_ab_with_range_on_trailing_b", "b", [ix("ab", ["a", "b"])], "a = 'x' AND b BETWEEN 1 AND 5", False),
	("composite_ab_with_not_equal_on_leading_a", "b", [ix("ab", ["a", "b"])], "a != 'x' AND b = 'y'", True),
	("composite_ab_with_is_null_on_leading_a", "b", [ix("ab", ["a", "b"])], "a IS NULL AND b = 'y'", False),
	("composite_abc_suggest_c_when_a_and_b_equal", "c", [ix("abc", ["a", "b", "c"])], "a = 1 AND b = 2 AND c = 3", False),
	("composite_abc_suggest_c_when_b_missing", "c", [ix("abc", ["a", "b", "c"])], "a = 1 AND c = 3", True),
	("composite_abc_suggest_b_when_a_equal", "b", [ix("abc", ["a", "b", "c"])], "a = 1 AND b = 2", False),
	("composite_with_suggested_column_leading", "b", [ix("bc", ["b", "c"])], "b = 'y'", False),
	("unique_composite_counts_like_btree", "b", [ix("uq", ["a", "b"], unique=True)], "a = 1 AND b = 2", False),
	("primary_key_composite_counts_like_btree", "b", [ix("PRIMARY", ["a", "b"], unique=True)], "a = 1 AND b = 2", False),
	("fulltext_index_on_column_does_not_serve_equality", "b", [unusable("ft_b", ["b"])], "b = 'y'", True),
	("fulltext_leading_composite_does_not_serve", "b", [unusable("ft", ["a", "b"])], "a = 1 AND b = 2", True),
	("hash_index_is_not_trusted", "b", [unusable("h", ["b"])], "b = 'y'", True),
	("or_between_leading_and_suggested_column", "b", [ix("ab", ["a", "b"])], "a = 'x' OR b = 'y'", True),
	("or_group_elsewhere_does_not_block_equality", "b", [ix("ab", ["a", "b"])], "a = 'x' AND b = 'y' AND (x = 1 OR y = 2)", False),
	("or_group_on_the_leading_column_blocks_composite", "b", [ix("ab", ["a", "b"])], "(a = 'x' OR a = 'z') AND b = 'y'", True),
	("function_wrapped_column_only", "b", [], "DATE(b) = '2026-01-01'", False),
	("function_wrapped_column_on_leading_a", "b", [ix("ab", ["a", "b"])], "LOWER(a) = 'x' AND b = 'y'", True),
	("function_and_plain_use_of_same_column", "b", [], "DATE(b) = '2026-01-01' AND b = 'y'", True),
	("parenthesised_conjuncts_as_frappe_emits", "b", [ix("ab", ["a", "b"])], "(a = 'x') AND (b = 'y')", False),
	("backticked_identifiers", "b", [ix("ab", ["a", "b"])], "`a` = 'x' AND `b` = 'y'", False),
	("table_qualified_columns", "b", [ix("ab", ["a", "b"])], "`tabT`.`a` = 'x' AND `tabT`.`b` = 'y'", False),
	("leading_column_belongs_to_a_joined_table", "b", [ix("ab", ["a", "b"])], "other.a = 'x' AND `tabT`.`b` = 'y'", True),
	("column_name_case_differs_from_index", "b", [ix("AB", ["A", "B"])], "a = 'x' AND b = 'y'", False),
	("order_by_column_served_by_equality_prefix", "b", [ix("ab", ["a", "b"])], "a = 'x' ORDER BY b LIMIT 5", False),
	("order_by_column_without_equality_prefix", "b", [ix("ab", ["a", "b"])], "x = 'x' ORDER BY b LIMIT 5", True),
	("literal_containing_and_does_not_confuse_parser", "b", [ix("ab", ["a", "b"])], "a = 'p AND q' AND b = 'y'", False),
]


@pytest.mark.parametrize(
	"column, indexes, where, survives",
	[s[1:] for s in COVERAGE_SCENARIOS],
	ids=[s[0] for s in COVERAGE_SCENARIOS],
)
def test_existing_index_coverage_matrix(monkeypatch, empty_context, column, indexes, where, survives):
	_install(monkeypatch, column, indexes)
	result = _analyze(empty_context, _select(where, n=3))
	assert bool(_missing(result)) is survives, result.warnings


def test_mixed_queries_keep_only_the_unserved_ones(monkeypatch, empty_context):
	"""Only the queries the composite does not serve are counted."""
	_install(monkeypatch, "b", [ix("ab", ["a", "b"])])
	result = _analyze(empty_context, _select("a = 'x' AND b = 'y'", n=4), _select("b = 'y'", n=2))
	(finding,) = _missing(result)
	assert finding["affected_count"] == 2
	assert _detail(finding)["affected_query_count"] == 2


def test_composite_holding_the_column_at_a_later_position_is_named_in_a_caution(monkeypatch, empty_context):
	_install(monkeypatch, "b", [ix("ab_idx", ["a", "b"])])
	(finding,) = _missing(_analyze(empty_context, _select("b = 'y'", n=3)))
	assert "Before adding, check:" in finding["customer_description"]
	assert "ab_idx" in finding["customer_description"]


def test_suppressed_by_existing_index_is_explained_in_a_warning(monkeypatch, empty_context):
	_install(monkeypatch, "b", [ix("ab", ["a", "b"])])
	result = _analyze(empty_context, _select("a = 'x' AND b = 'y'", n=3))
	assert any("already served by an existing index" in w and "tabT.b" in w for w in result.warnings)


def test_function_wrapped_suppression_is_explained_in_a_warning(monkeypatch, empty_context):
	_install(monkeypatch, "b", [])
	result = _analyze(empty_context, _select("DATE(b) = '2026-01-01'", n=3))
	assert any("only used inside a function" in w for w in result.warnings)


def test_single_show_index_call_per_table(monkeypatch, empty_context):
	_install(monkeypatch, "b", [ix("ab", ["a", "b"])])
	calls = []
	dialect = index_suggestions.get_dialect()
	dialect.existing_indexes = lambda table: calls.append(table) or [ix("ab", ["a", "b"])]
	_analyze(empty_context, _select("b = 'y'", n=2), _select("b = 'z' AND a = 'q'", n=2))
	assert calls == [TABLE]


# ---------------------------------------------------------------------------
# 2. Unknown DB and Postgres behaviour (kept from before)
# ---------------------------------------------------------------------------


def test_unknown_db_fallback_keeps_the_suggestion_with_plain_ddl(monkeypatch, empty_context):
	"""With no readable indexes or column types the suggestion stays, unverified."""
	_install(monkeypatch, "b", [], with_schema=False)
	(finding,) = _missing(_analyze(empty_context, _select("b = 'y'", n=3)))
	detail = _detail(finding)
	assert "ALTER TABLE" in detail["suggested_ddl"]
	assert detail["verified_not_indexed"] is False


def test_postgres_skips_index_suggestions(monkeypatch, empty_context):
	class _Postgres:
		supports_query_optimizer = False

	monkeypatch.setattr(index_suggestions, "get_dialect", lambda: _Postgres())
	result = _analyze(empty_context, _select("b = 'y'", n=3))
	assert result.findings == []
	assert any("PostgreSQL" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 3. Cross-flow harm (write cost seen in the same session)
# ---------------------------------------------------------------------------

# (scenario id, writes, helped reads, cardinality, outcome, text expected in the caution/warning)
HARM_SCENARIOS = [
	("read_only_session", {}, 10, None, "clean", None),
	("column_updated_once_is_a_caution", {"update_b": 1}, 10, None, "caution", "update `b`"),
	("column_updated_often_but_reads_dominate", {"update_b": 5}, 20, None, "caution", "5 statement(s)"),
	("column_updated_as_often_as_it_would_help", {"update_b": 5}, 5, None, "suppress", None),
	("column_updated_more_than_it_would_help", {"update_b": 12}, 3, None, "suppress", None),
	("column_updated_four_times_is_below_the_hot_threshold", {"update_b": 4}, 2, None, "caution", "4 statement(s)"),
	("other_column_updates_do_not_count_against_this_one", {"update_x": 30}, 10, None, "caution", "written 30"),
	("table_written_often_enough_to_warn", {"inserts": 10}, 10, None, "caution", "10 insert"),
	("table_written_a_little_is_clean", {"inserts": 9}, 10, None, "clean", None),
	("write_heavy_table_outweighs_reads", {"inserts": 60}, 5, None, "suppress", None),
	("write_heavy_table_but_reads_are_close", {"inserts": 60}, 10, None, "caution", "60 time"),
	("write_heavy_via_deletes_and_inserts", {"inserts": 30, "deletes": 30}, 5, None, "suppress", None),
	("fewer_than_fifty_writes_never_suppresses_on_table_rule", {"inserts": 49}, 1, None, "caution", "49 time"),
	("low_cardinality_column_is_a_caution", {}, 10, 3, "caution", "3 distinct value"),
	("cardinality_of_eleven_is_not_low", {}, 10, 11, "clean", None),
	("cardinality_of_ten_is_low", {}, 10, 10, "caution", "10 distinct value"),
	("cardinality_of_zero_is_not_a_caution", {}, 10, 0, "clean", None),
	("decimal_cardinality_is_low", {}, 10, Decimal("3"), "caution", "3 distinct value"),
	("exactly_fifty_writes_at_ten_times_reads_suppresses", {"inserts": 50}, 5, None, "suppress", None),
	("fifty_writes_just_under_ten_times_reads_is_a_caution", {"inserts": 50}, 6, None, "caution", "50 time"),
	("unknown_cardinality_is_not_a_caution", {}, 10, None, "clean", None),
]


@pytest.mark.parametrize(
	"writes, reads, cardinality, outcome, expected_text",
	[s[1:] for s in HARM_SCENARIOS],
	ids=[s[0] for s in HARM_SCENARIOS],
)
def test_cross_flow_harm_matrix(monkeypatch, empty_context, writes, reads, cardinality, outcome, expected_text):
	_install(monkeypatch, "b", [], cardinality=cardinality)
	result = _analyze(empty_context, _select("b = 'y'", n=reads), _writes(**writes))
	findings = _missing(result)
	if outcome == "suppress":
		assert findings == []
		assert any("write cost" in w and "tabT" in w for w in result.warnings)
		return
	(finding,) = findings
	has_caution = "Before adding, check:" in finding["customer_description"]
	assert has_caution is (outcome == "caution")
	if expected_text:
		assert expected_text in finding["customer_description"]
	assert _detail(finding)["write_impact"]["helped_queries"] == reads


def test_writes_to_another_table_are_ignored(monkeypatch, empty_context):
	_install(monkeypatch, "b", [])
	other = [
		{"query": "UPDATE `tabOther` SET b = ? WHERE a = ?", "normalized_query": "x", "duration": 1.0, "stack": []}
	] * 40
	(finding,) = _missing(_analyze(empty_context, _select("b = 'y'", n=3), other))
	assert "Before adding, check:" not in finding["customer_description"]


def test_caution_names_the_other_flows_that_write(monkeypatch, empty_context):
	_install(monkeypatch, "b", [])
	writer = {"uuid": "w", "path": "/api/method/save_invoice", "cmd": "save_invoice", "calls": _writes(update_b=2)}
	reader = {"uuid": "r", "path": "/api/method/list", "cmd": None, "calls": _select("b = 'y'", n=30)}
	result = index_suggestions.analyze([reader, writer], empty_context)
	(finding,) = _missing(result)
	assert "save_invoice (2)" in finding["customer_description"]


def test_write_impact_stats_are_exposed_in_technical_detail(monkeypatch, empty_context):
	_install(monkeypatch, "b", [])
	(finding,) = _missing(_analyze(empty_context, _select("b = 'y'", n=40), _writes(inserts=3, update_b=2, deletes=1)))
	stats = _detail(finding)["write_impact"]
	assert (stats["table_inserts"], stats["table_updates"], stats["table_deletes"]) == (3, 2, 1)
	assert stats["column_updates"] == 2


def test_harm_check_runs_even_when_the_column_is_served_nowhere_else(monkeypatch, empty_context):
	"""The composite hint and the write caution can both appear."""
	_install(monkeypatch, "b", [ix("ab", ["a", "b"])])
	(finding,) = _missing(_analyze(empty_context, _select("b = 'y'", n=40), _writes(update_b=1)))
	assert finding["customer_description"].count("Before adding, check:") == 1


# ---------------------------------------------------------------------------
# 4. Pure helper unit tests
# ---------------------------------------------------------------------------


def test_collect_writes_parses_insert_update_delete_replace_and_backticked_names():
	calls = [
		"INSERT INTO `tabSales Invoice` (a) VALUES (?)",
		"INSERT IGNORE INTO tabNote (a) VALUES (?)",
		"REPLACE INTO `tabNote` (a) VALUES (?)",
		"DELETE FROM `tabNote` WHERE name = ?",
		"/* c */ UPDATE `tabNote` SET a = ?, b = IFNULL(b, ?) + 1, `c`=? WHERE name = ?",
		"UPDATE tabNote SET x = 'one, two WHERE y = 1' WHERE name = ?",
		"SELECT * FROM tabNote",
	]
	rec = {"uuid": "u", "calls": [{"query": q} for q in calls]}
	writes = index_harm.collect_writes([rec])
	assert writes["tabSales Invoice"].inserts == 1
	note = writes["tabNote"]
	assert (note.inserts, note.deletes, note.updates) == (2, 1, 2)
	assert note.updated_columns == Counter({"a": 1, "b": 1, "c": 1, "x": 1})


def test_collect_writes_survives_missing_query_text():
	assert index_harm.collect_writes([{"uuid": "u", "calls": [{}, {"query": None}]}]) == {}


@pytest.mark.parametrize(
	"sql, equality, wrapped",
	[
		("SELECT * FROM tabT WHERE a = 1 AND b IN (1, 2)", {"a", "b"}, set()),
		("SELECT * FROM tabT WHERE a = 1 OR b = 2", set(), set()),
		("SELECT * FROM tabT WHERE a = 1 AND (b = 2 OR c = 3)", {"a"}, set()),
		("SELECT * FROM tabT WHERE YEAR(a) = 2026 AND b = 1", {"b"}, {"a"}),
		("SELECT * FROM tabT WHERE a BETWEEN 1 AND 2 AND b = 1", {"b"}, set()),
		("SELECT * FROM tabT WHERE a = (SELECT MAX(x) FROM tabU WHERE y = 1)", {"a"}, set()),
		("SELECT * FROM tabT", set(), set()),
		("", set(), set()),
	],
)
def test_parse_where_shapes(sql, equality, wrapped):
	shape = parse_where(sql, "tabT")
	assert (shape.equality, shape.wrapped) == (equality, wrapped)


def test_is_served_on_empty_index_list_and_empty_column():
	assert not is_served("a", parse_where("SELECT 1 FROM tabT WHERE a = 1", "tabT"), [])
	assert not is_served("", parse_where("SELECT 1 FROM tabT WHERE a = 1", "tabT"), [ix("a", ["a"])])


SI = "tabSales Invoice"


def test_multi_word_doctype_qualified_columns_are_classified():
	sql = f"SELECT * FROM `{SI}` WHERE `{SI}`.`status` = 'x' AND `{SI}`.`customer` = 'c'"
	shape = parse_where(sql, SI)
	assert shape.equality == {"status", "customer"}
	assert is_served("customer", shape, [ix("sc", ["status", "customer"])])


def test_multi_word_doctype_with_backticked_alias_map():
	sql = f"SELECT * FROM `{SI}` si WHERE si.status = 'x' AND si.customer = 'c'"
	shape = parse_where(sql, f"`{SI}`", {"si": f"`{SI}`"})
	assert shape.equality == {"status", "customer"}


def test_multi_word_doctype_ignores_other_doctype_columns():
	sql = f"SELECT * FROM `{SI}` JOIN `tabSales Invoice Item` ON 1 WHERE `tabSales Invoice Item`.`item_code` = 'x'"
	assert parse_where(sql, SI).equality == set()


def test_multi_word_doctype_function_wrapped_column():
	shape = parse_where(f"SELECT * FROM `{SI}` WHERE DATE(`{SI}`.`posting_date`) = '2026-01-01'", SI)
	assert shape.wrapped == {"posting_date"}


def test_exists_subquery_does_not_mark_child_column_as_wrapped():
	sql = "SELECT * FROM `tabT` WHERE EXISTS (SELECT 1 FROM `tabChild` WHERE parent = tabT.name)"
	assert parse_where(sql, "tabChild").wrapped == set()
	assert parse_where(sql, "tabT").wrapped == set()


def test_exists_subquery_keeps_the_child_index_suggestion(monkeypatch, empty_context):
	_install(monkeypatch, "parent", [])
	where = "EXISTS (SELECT 1 FROM `tabChild` WHERE parent = tabT.name)"
	result = _analyze(empty_context, _select(where, n=3))
	assert not any("only used inside a function" in w for w in result.warnings)


@pytest.mark.parametrize(
	"where, wrapped",
	[
		("IFNULL(a, 0) = b", {"a"}),
		("DATE(a) = b", {"a"}),
		("LOWER(a) = LOWER(b)", {"a"}),
		("a = DATE(b)", set()),
	],
)
def test_only_the_function_argument_is_wrapped(where, wrapped):
	assert parse_where(f"SELECT * FROM tabT WHERE {where}", "tabT").wrapped == wrapped


def test_self_join_aliases_keep_the_suggestion():
	sql = "SELECT * FROM tabT a JOIN tabT b ON a.name = b.parent WHERE a.x = 1 AND b.y = 2"
	shape = parse_where(sql, "tabT", {"a": "tabT", "b": "tabT"})
	assert shape.ambiguous
	assert not is_served("y", shape, [ix("xy", ["x", "y"])])


def test_unqualified_column_in_a_join_keeps_the_suggestion():
	sql = "SELECT * FROM tabT JOIN tabU ON tabU.parent = tabT.name WHERE status = 'a'"
	shape = parse_where(sql, "tabT")
	assert shape.ambiguous
	assert not is_served("status", shape, [ix("s", ["status"])])


def test_qualified_columns_in_a_join_stay_attributed():
	sql = "SELECT * FROM tabT JOIN tabU ON tabU.parent = tabT.name WHERE tabT.x = 1 AND tabT.y = 2"
	shape = parse_where(sql, "tabT")
	assert not shape.ambiguous
	assert is_served("y", shape, [ix("xy", ["x", "y"])])


@pytest.mark.parametrize(
	"where, served",
	[("name LIKE '%abc'", False), ("name LIKE 'abc%'", True), ("name NOT LIKE '%abc'", False)],
)
def test_leading_wildcard_like_is_not_served(where, served):
	shape = parse_where(f"SELECT * FROM tabT WHERE {where}", "tabT")
	assert is_served("name", shape, [ix("n", ["name"])]) is served


def test_unusable_index_with_usable_prefix_covers_only_the_prefix():
	shape = parse_where("SELECT * FROM tabT WHERE a = 1 AND b = 2 AND c = 3", "tabT")
	prefixed = unusable("abc", ["a", "b", "c"], usable_prefix=["a", "b"])
	assert is_served("b", shape, [prefixed])
	assert not is_served("c", shape, [prefixed])


def test_assess_harm_is_deterministic_and_pure():
	writes = {"tabT": index_harm.TableWrites(inserts=60)}
	first = index_harm.assess_harm("tabT", "b", 5, writes)
	second = index_harm.assess_harm("tabT", "b", 5, writes)
	assert first == second and first.suppress_reason


# ---------------------------------------------------------------------------
# 5. Dialect adapter: SHOW INDEX exposes the index type
# ---------------------------------------------------------------------------


def test_mariadb_existing_indexes_reports_usability(monkeypatch):
	def row(key, seq, col, kind="BTREE", sub=None, non_unique=1):
		return {
			"Key_name": key, "Non_unique": non_unique, "Seq_in_index": seq, "Column_name": col,
			"Index_type": kind, "Sub_part": sub, "Ignored": "NO",
		}

	rows = [
		row("PRIMARY", 1, "name", non_unique=0),
		row("ft", 1, "title", "FULLTEXT"),
		row("ab", 1, "a"),
		row("ab", 2, "b", sub=20),
		{**row("ig", 1, "c"), "Ignored": "YES"},
	]

	class _DB:
		def sql(self, *a, **k):
			return rows

	frappe = sys.modules.get("frappe") or types.ModuleType("frappe")
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	monkeypatch.setattr(frappe, "db", _DB(), raising=False)
	by_name = {i.name: i for i in MariaDBDialect().existing_indexes("tabT")}
	assert by_name["PRIMARY"].unique and by_name["PRIMARY"].eligible
	assert not by_name["ft"].eligible and not by_name["ft"].usable_prefix
	assert not by_name["ig"].eligible
	assert by_name["ab"].columns == ["a", "b"]
	assert not by_name["ab"].eligible and by_name["ab"].usable_prefix == ("a",)


@pytest.mark.parametrize(
	"where",
	["NOT (status = 'x')", "NOT(status = 'x')", "not (status = 'x')", "NOT (status = 'x' AND y = 1)"],
)
def test_not_group_is_a_plain_predicate_not_a_function(where):
	shape = parse_where(f"SELECT * FROM tabT WHERE {where}", "tabT")
	assert "status" in shape.plain
	assert shape.wrapped == set()
	assert shape.equality == set()


def test_not_group_around_a_real_function_is_still_wrapped():
	shape = parse_where("SELECT * FROM tabT WHERE NOT (DATE(a) = '2026-01-01')", "tabT")
	assert shape.wrapped == {"a"}
	assert shape.plain == set()


@pytest.mark.parametrize(
	"where",
	[
		"NOT EXISTS (SELECT 1 FROM tabU WHERE tabU.p = tabT.a)",
		"EXISTS (SELECT 1 FROM tabU WHERE tabU.p = tabT.a)",
		"a = ANY (SELECT p FROM tabU)",
		"a IN (SELECT p FROM tabU)",
	],
)
def test_keyword_groups_are_not_function_calls(where):
	shape = parse_where(f"SELECT * FROM tabT WHERE {where}", "tabT")
	assert shape.wrapped == set()


def test_not_group_suggestion_is_kept_without_the_function_warning(monkeypatch, empty_context):
	_install(monkeypatch, "c", [])
	result = _analyze(empty_context, _select("NOT (c = 'x')", n=3))
	assert len(_missing(result)) == 1
	assert not any("only used inside a function" in w for w in result.warnings)


@pytest.mark.parametrize(
	"where, weak",
	[
		("status != 'x'", True),
		("status <> 'x'", True),
		("status NOT IN ('x', 'y')", True),
		("status NOT LIKE 'x%'", True),
		("NOT (status = 'x')", True),
		("status != 'x' AND status = 'y'", False),
		("status = 'x'", False),
		("status > 'x'", False),
		("status != 'x' AND status > 'a'", False),
	],
)
def test_weak_only_predicates(where, weak):
	shape = parse_where(f"SELECT * FROM tabT WHERE {where}", "tabT")
	assert is_weak_only("status", shape) is weak


@pytest.mark.parametrize("where", ["c != 'Cancelled'", "c NOT IN ('A', 'B')"])
def test_negation_on_an_indexed_column_stays_served_with_its_own_warning(monkeypatch, empty_context, where):
	_install(monkeypatch, "c", [ix("s", ["c"])])
	result = _analyze(empty_context, _select(where, n=3))
	assert not _missing(result)
	assert any("may not be able to serve" in w and "tabT.c" in w for w in result.warnings)
	assert not any("already served by an existing index" in w for w in result.warnings)


def test_equality_on_an_indexed_column_keeps_the_already_indexed_warning(monkeypatch, empty_context):
	_install(monkeypatch, "c", [ix("s", ["c"])])
	result = _analyze(empty_context, _select("c = 'x'", n=3))
	assert any("already indexed" in w for w in result.warnings)
	assert not any("may not be able to serve" in w for w in result.warnings)


def test_function_warning_mentions_ifnull_and_the_fix(monkeypatch, empty_context):
	_install(monkeypatch, "b", [])
	result = _analyze(empty_context, _select("ifnull(b, '') = 'x'", n=3))
	warning = next(w for w in result.warnings if "only used inside a function" in w)
	assert "ifnull" in warning and "functional index" in warning and "range" in warning


def test_negation_after_an_equality_prefix_of_a_composite_gets_the_weak_warning(monkeypatch, empty_context):
	_install(monkeypatch, "b", [ix("ab", ["a", "b"])])
	result = _analyze(empty_context, _select("a = 'x' AND b != 'y'", n=3))
	assert not _missing(result)
	assert any("may not be able to serve" in w and "tabT.b" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 5. Linear-time parsing, fail-open guards and review follow-ups
# ---------------------------------------------------------------------------

MANY = 5000
ADVERSARIAL_SQL = [
	"/**/ " * MANY + "SELECT 1",
	"/**/ " * MANY,
	"/* " * MANY,
	"/*" * MANY + "*/",
	"SELECT * FROM tabT WHERE a = " + "'" * MANY,
	"SELECT * FROM tabT WHERE a = '" + "\\'" * MANY,
	"SELECT * FROM tabT WHERE " + "(" * MANY + "a = 1" + ")" * MANY,
	"SELECT * FROM tabT WHERE " + "a = 1 AND " * MANY + "b = 2",
	"SELECT * FROM tabT WHERE " + "a = 1 OR " * MANY + "b = 2",
	"SELECT * FROM tabT WHERE a BETWEEN " + "x " * MANY,
	"SELECT * FROM tabT WHERE " + "`a` " * MANY,
	"UPDATE `tabT` SET " + "a = ?, " * MANY + "b = ? WHERE x = 1",
	"UPDATE `tabT` SET " + "(" * MANY,
	"UPDATE " + "LOW_PRIORITY " * MANY + "`tabT` SET a = 1",
	"INSERT " + "IGNORE " * MANY + "INTO",
	" " * 100000 + "DELETE FROM tabT",
	"SELECT * FROM tabT WHERE a LIKE " + "'%x' " * MANY,
	"SELECT * FROM tabT WHERE " + "a = 1 AND (" * 3000 + "b = 1" + ")" * 3000,
	"SELECT * FROM tabT WHERE " + "NOT (" * 3000 + "b = 1" + ")" * 3000,
]


@pytest.mark.parametrize("sql", ADVERSARIAL_SQL, ids=[f"case{i}" for i in range(len(ADVERSARIAL_SQL))])
def test_adversarial_sql_is_parsed_in_linear_time(sql):
	started = time.perf_counter()
	index_harm.collect_writes([{"uuid": "u", "calls": [{"query": sql}]}])
	index_suggestions._get_query_type(sql)
	parse_where(sql, TABLE)
	assert time.perf_counter() - started < 1.0


def test_thousands_of_leading_comments_still_find_the_write():
	sql = "/**/ " * MANY + f"UPDATE `{TABLE}` SET b = ? WHERE a = ?"
	writes = index_harm.collect_writes([{"uuid": "u", "calls": [{"query": sql}]}])
	assert writes[TABLE].updated_columns == Counter({"b": 1})
	assert index_suggestions._get_query_type("/**/ " * MANY + "select 1") == "SELECT"


def test_strip_leading_comments():
	strip = index_suggestions.strip_leading_comments
	assert strip("  /* a */ /* b */\n SELECT 1") == "SELECT 1"
	assert strip("/* open SELECT 1") == "/* open SELECT 1"
	assert strip("SELECT /* c */ 1") == "SELECT /* c */ 1"


def test_non_string_query_text_does_not_drop_findings(monkeypatch, empty_context):
	_install(monkeypatch, "b", [])
	bad = [{"query": b"UPDATE x", "normalized_query": "x", "duration": 1.0, "stack": []}]
	assert index_harm.collect_writes([{"uuid": "u", "calls": bad}]) == {}
	assert _missing(_analyze(empty_context, _select("b = 'y'", n=3), bad))


def test_collect_writes_failure_fails_open(monkeypatch, empty_context):
	_install(monkeypatch, "b", [])

	def boom(recordings):
		raise RuntimeError("boom")

	monkeypatch.setattr(index_suggestions, "collect_writes", boom)
	assert _missing(_analyze(empty_context, _select("b = 'y'", n=3)))


def test_job_timeout_is_not_swallowed_by_alias_parsing(monkeypatch):
	class JobTimeout(Exception):
		pass

	class Boom:
		def __init__(self, sql):
			raise JobTimeout()

	monkeypatch.setitem(sys.modules, "sql_metadata", types.SimpleNamespace(Parser=Boom))
	monkeypatch.setattr(index_suggestions, "job_timeout_types", lambda: (JobTimeout,))
	with pytest.raises(JobTimeout):
		index_suggestions._query_aliases("SELECT 1")


def test_aliases_are_parsed_once_per_entry(monkeypatch):
	calls = []
	monkeypatch.setattr(index_suggestions, "_query_aliases", lambda sql: calls.append(sql) or {})
	entries = [{"raw": f"SELECT * FROM `{TABLE}` WHERE b = 'y'", "duration": 1.0, "count": 1}]
	index_suggestions._partition_queries(TABLE, "b", entries, [])
	index_suggestions._partition_queries(TABLE, "b", entries, [])
	assert len(calls) == 1


def test_wrapped_column_after_an_equality_prefix_is_not_reported_as_served(monkeypatch, empty_context):
	_install(monkeypatch, "b", [ix("ab", ["a", "b"])])
	result = _analyze(empty_context, _select("a = 'x' AND DATE(b) = '2026-01-01'", n=3))
	assert not _missing(result)
	assert any("only used inside a function" in w for w in result.warnings)
	assert not any("already served" in w for w in result.warnings)


def test_ambiguous_join_is_not_treated_as_wrapped():
	shape = parse_where(
		"SELECT * FROM `tabT` JOIN `tabT` x ON x.name = `tabT`.name WHERE ifnull(b, '') = 'y'", TABLE
	)
	assert shape.ambiguous


def test_union_is_ambiguous():
	sql = "SELECT name FROM `tabT` WHERE a = 'x' UNION SELECT name FROM `tabOther` WHERE b = 'y'"
	assert parse_where(sql, TABLE).ambiguous
	assert not is_served("a", parse_where(sql, TABLE), [ix("a", ["a"])])


@pytest.mark.parametrize(
	"where, scan",
	[
		("b LIKE '%x'", True),
		("b LIKE CONCAT('%', c)", True),
		("b LIKE c", True),
		("b LIKE 'x%'", False),
		("b LIKE %s", False),
	],
)
def test_like_is_a_scan_unless_it_is_a_plain_prefix_literal(where, scan):
	shape = parse_where(f"SELECT * FROM `{TABLE}` WHERE {where}", TABLE)
	assert ("b" in shape.scan) is scan


def test_is_not_null_is_not_a_weak_negation():
	shape = parse_where(f"SELECT * FROM `{TABLE}` WHERE b IS NOT NULL", TABLE)
	assert not is_weak_only("b", shape)
	assert "b" in shape.narrow


def test_savings_floor_and_examples_use_only_the_kept_queries(monkeypatch, empty_context):
	_install(monkeypatch, "b", [ix("ab", ["a", "b"])])
	served = "SELECT * FROM `tabT` WHERE a = 'x' AND b = 'y'"
	kept = "SELECT * FROM `tabT` WHERE b = 'y'"
	calls = [{"query": served, "normalized_query": served, "duration": 90.0, "stack": []} for _ in range(5)]
	calls += [{"query": kept, "normalized_query": kept, "duration": 1.0, "stack": []} for _ in range(5)]
	result = _analyze(empty_context, calls)
	assert not _missing(result)
	assert any("average savings below" in w for w in result.warnings)
	calls[-1]["duration"] = 50.0
	(finding,) = _missing(_analyze(empty_context, calls))
	assert _detail(finding)["example_queries"] == [kept]
	assert finding["affected_count"] == 5


def test_first_cardinality_is_kept(monkeypatch, empty_context):
	_install(monkeypatch, "b", [])
	seen = iter([3, None])
	recorder = sys.modules["frappe.core.doctype.recorder.recorder"]
	monkeypatch.setattr(
		recorder, "_optimize_query", lambda q: _Suggested(TABLE, "b", next(seen, None)), raising=False
	)
	calls = _select("b = 'x'", n=3) + _select("b = 'y' AND c = 'z'", n=3)
	(finding,) = _missing(_analyze(empty_context, calls))
	assert _detail(finding)["write_impact"]["cardinality"] == 3


@pytest.mark.parametrize(
	"sql, column",
	[
		('UPDATE "tabT" SET b = %s WHERE a = 1', "b"),
		("UPDATE `tabT` t SET t.b = ? WHERE t.a = 1", "b"),
		("UPDATE `tabT` AS t SET t.b = ? WHERE t.a = 1", "b"),
		("UPDATE `tabT` SET b = (SELECT MAX(x) FROM y WHERE y.a = 1), c = 2 WHERE a = 1", "c"),
	],
)
def test_write_detection_covers_aliases_quoting_and_subselects(sql, column):
	writes = index_harm.collect_writes([{"uuid": "u", "calls": [{"query": sql}]}])
	assert writes[TABLE].updates == 1
	assert column in writes[TABLE].updated_columns


def test_write_cost_warning_keeps_the_counts(monkeypatch, empty_context):
	_install(monkeypatch, "b", [])
	result = _analyze(empty_context, _select("b = 'y'", n=5), _writes(update_b=7))
	assert not _missing(result)
	assert any("7 statement(s)" in w and "tabT" in w for w in result.warnings)


def test_very_deep_nesting_is_ambiguous_not_a_crash():
	shape = parse_where("SELECT * FROM tabT WHERE " + "(" * 500 + "b = 1" + ")" * 500, TABLE)
	assert shape.ambiguous


def test_a_parser_failure_keeps_the_suggestion(monkeypatch, empty_context):
	_install(monkeypatch, "b", [])

	def boom(*args, **kwargs):
		raise RecursionError

	monkeypatch.setattr(index_suggestions, "parse_where", boom)
	assert _missing(_analyze(empty_context, _select("b = 'y'", n=3)))
