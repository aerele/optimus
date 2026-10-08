# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Deterministic index advice for index-family findings and table cards (no site access, no I/O).

``advise`` is the one advisor for both surfaces. It reads only the evidence
``recipe_enrichment`` collects for the table (DocField flags, real column types, existing
indexes). A finding also has its query, so its columns first pass the predicate-shape
check (OR, wildcard LIKE, a function, CASE or arithmetic around the column) and are put
in index order (equality columns first, then either one range column or the sort/group
columns, not both); a table
card has no query and gets neither, so the two can differ when a query's filter shape
rules a column out. The advisor picks one route:

- ``search_index``: one non-text column of a field the developer controls (an app in
  Tracked Apps, a Custom Field, or a DocType created in the UI) on MariaDB. The advice
  is text: tick Search Index.
- ``ensure_indexes``: any other index that can be built. The code is one idempotent
  ``ensure_indexes()`` for the developer's app, run from hooks.py ``after_install``,
  ``after_sync`` and ``after_migrate``. Each entry checks its database (an entry that is
  only right on MariaDB or Postgres says so in ``"db"``), its table, its columns and its
  index first; a failing entry rolls back only its own writes and writes an Error Log row
  instead of stopping bench migrate. Another app's single column gets its index built
  first and a ``search_index`` Property Setter only after that, so a failed build leaves
  nothing that a later sync of the DocType would retry outside the guard. Index builds wait
  at most 300 seconds for a table lock.
- ``no_code``: an explanation and no code. The index exists already when an existing
  index starts with the whole final recipe; a one-column recipe is also refused when its
  column is unique on its own, leads an index, or leads an index EXPLAIN names. The table's
  real index list decides, never the Search Index flag. A no_code that only says Optimus
  could not tell (``unknown``: no evidence for the table, an unread filter, a UNION that
  filters the table in more than one branch, a query too long to parse) is no verdict, so a
  table card never says "Do not add this index." for it.

Frappe v16.18 facts relied on: MariaDB schema sync drops a single-column index its
DocField does not declare but never one that spans several columns or covers a text
column (frappe/database/schema.py:310-315, mariadb/schema.py:142-145,
mariadb/database.py:383-411); Postgres schema sync names a Search Index after the bare
field name and drops by that name (postgres/schema.py:124, :183); frappe.db.add_index
writes column names unquoted on MariaDB (mariadb/database.py:420-425) and strips a
prefix on Postgres (postgres/database.py:400); on install, after_install runs before
fixtures are synced and after_sync right after them (installer.py:332-345), and on
migrate after_migrate runs after them (migrate.py:165-197). The generated module also
relies on these, the same on v15: MariaDB ``get_column_index(table, field, unique=False)``
finds only an index whose one column is the field (mariadb/database.py:383-411), the
check Frappe's own sync makes before it adds ``<field>_index`` (mariadb/schema.py:88-92);
``add_index`` writes no Property Setter during install or migrate (mariadb/database.py:428)
and commits before its DDL, on Postgres too (database.py:451-457); only v16 migrate caps
``lock_wait_timeout`` (migrate.py:214-221) and install never does; and on MariaDB Error Log
indexes ``reference_name`` on v16 and ``reference_doctype`` on v15, never ``method``. On
Postgres a Search Index is named after the bare field and index names are schema-wide
(postgres/schema.py:124 on v16, :115 on v15), so Error Log gets that index only when no other
table took the name first.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from optimus.analyzers.base import (
	FRAMEWORK_APPS,
	FRAPPE_METADATA_COLUMNS,
	INDEX_FINDING_TYPES,
	QUERY_TEXT_LIMIT,
	is_write_hot_table,
)
from optimus.safe_call import best_effort

if TYPE_CHECKING:
	from optimus.renderer.recipe_enrichment import TableEvidence

ROUTE_SEARCH_INDEX = "search_index"
ROUTE_ENSURE_INDEXES = "ensure_indexes"
ROUTE_NO_CODE = "no_code"

# Slow Query gets the same advice as a profiler fact in its AI prompt (P15).
ADVISED_FINDING_TYPES: frozenset[str] = INDEX_FINDING_TYPES | {"Slow Query"}
TEXT_INDEX_PREFIX = 255
MAX_INDEX_COLUMNS = 4
MARIADB_MAX_KEY_BYTES = 3072
# A Postgres btree index row holds at most about 2704 bytes (a third of an 8 KB page);
# a longer value is refused when the row is written, not when the index is created.
POSTGRES_MAX_INDEX_ROW_BYTES = 2704
VARCHAR_DEFAULT_LENGTH = 140
BYTES_PER_CHAR = 4
FIXED_WIDTH_BYTES = 8
TRAILING_METADATA_OK: frozenset[str] = frozenset({"creation", "modified"})
# The query parser is superlinear (cycle 1: 216 ms at 19 KB): a longer query gets an
# explanation instead of a parse.
MAX_QUERY_CHARS = 4096
HOOK_MODULE = "optimus_indexes"
UNKNOWN_APP = "your_app"
# The hooks.py lists ensure_indexes() is registered in (owner decision D4): after_sync
# runs right after the install's fixture sync, so a fixture-shipped Custom Field is
# indexed on a fresh install too.
HOOK_EVENTS: tuple[str, ...] = ("after_install", "after_sync", "after_migrate")
_HOOK_EVENTS_TEXT = "after_install, after_sync and after_migrate"

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PREFIX_SUFFIX_RE = re.compile(r"\(\d+\)$")  # an index prefix length, as in remarks(255)
_APP_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_TAB_TABLE_RE = re.compile(r"^tab[A-Za-z0-9 _\-]+$")
_VARCHAR_TYPES: frozenset[str] = frozenset({"varchar", "char", "character varying", "character"})
_AGGREGATES = ("sum", "count", "avg", "min", "max", "group_concat")

# MariaDB reserved words (mariadb.com/kb/en/reserved-words). frappe.db.add_index writes
# column names without quotes (mariadb/database.py:424), so such a column fails to index.
MARIADB_RESERVED_WORDS: frozenset[str] = frozenset({
	"accessible", "add", "all", "alter", "analyze", "and", "as", "asc", "asensitive", "before",
	"between", "bigint", "binary", "blob", "both", "by", "call", "cascade", "case", "change", "char",
	"character", "check", "collate", "column", "condition", "constraint", "continue", "convert",
	"create", "cross", "current_date", "current_role", "current_time", "current_timestamp",
	"current_user", "cursor", "database", "databases", "day_hour", "day_microsecond", "day_minute",
	"day_second", "dec", "decimal", "declare", "default", "delayed", "delete", "delete_domain_id",
	"desc", "describe", "deterministic", "distinct", "distinctrow", "div", "do_domain_ids", "double",
	"drop", "dual", "each", "else", "elseif", "enclosed", "escaped", "except", "exists", "exit",
	"explain", "false", "fetch", "float", "float4", "float8", "for", "force", "foreign", "from",
	"fulltext", "general", "grant", "group", "having", "high_priority", "hour_microsecond",
	"hour_minute", "hour_second", "if", "ignore", "ignore_domain_ids", "ignore_server_ids", "in",
	"index", "infile", "inner", "inout", "insensitive", "insert", "int", "int1", "int2", "int3",
	"int4", "int8", "integer", "intersect", "interval", "into", "is", "iterate", "join", "key",
	"keys", "kill", "leading", "leave", "left", "like", "limit", "linear", "lines", "load",
	"localtime", "localtimestamp", "lock", "long", "longblob", "longtext", "loop", "low_priority",
	"master_heartbeat_period", "master_ssl_verify_server_cert", "match", "maxvalue", "mediumblob",
	"mediumint", "mediumtext", "middleint", "minute_microsecond", "minute_second", "mod", "modifies",
	"natural", "not", "no_write_to_binlog", "null", "numeric", "offset", "on", "optimize", "option",
	"optionally", "or", "order", "out", "outer", "outfile", "over", "page_checksum",
	"parse_vcol_expr", "partition", "position", "precision", "primary", "procedure", "purge",
	"range", "read", "reads", "read_write", "real", "recursive", "ref_system_id", "references",
	"regexp", "release", "rename", "repeat", "replace", "require", "resignal", "restrict",
	"return", "returning", "revoke", "right", "rlike", "rows", "row_number", "schema", "schemas",
	"second_microsecond", "select", "sensitive", "separator", "set", "show", "signal", "slow",
	"smallint", "spatial", "specific", "sql", "sqlexception", "sqlstate", "sqlwarning",
	"sql_big_result", "sql_calc_found_rows", "sql_small_result", "ssl", "starting",
	"stats_auto_recalc", "stats_persistent", "stats_sample_pages", "straight_join", "table",
	"terminated", "then", "tinyblob", "tinyint", "tinytext", "to", "trailing", "trigger", "true",
	"undo", "union", "unique", "unlock", "unsigned", "update", "usage", "use", "using", "utc_date",
	"utc_time", "utc_timestamp", "values", "varbinary", "varchar", "varcharacter", "varying",
	"when", "where", "while", "window", "with", "write", "xor", "year_month", "zerofill",
})

_CLAUSES_BY_TYPE: dict[str, tuple[tuple[str, ...], ...]] = {
	"Full Table Scan": (("WHERE", "JOIN"),),
	"Low Filter Ratio": (("WHERE", "JOIN"),),
	"Slow Query": (("WHERE", "JOIN"),),
	"Filesort": (("WHERE", "JOIN"), ("ORDER BY",)),
	"Temporary Table": (("WHERE", "JOIN"), ("GROUP BY",)),
}

_TYPE_LEADS: dict[str, str] = {
	"Full Table Scan": "Index the columns this query filters on so it stops reading the whole table.",
	"Filesort": "Index the filter columns followed by the sort column so the rows come back already sorted.",
	"Temporary Table": (
		"Index the filter and GROUP BY columns so the grouping reads the index instead of a temporary table."
	),
	"Low Filter Ratio": (
		"Index the most selective filter column first so fewer rows are read, "
		"then confirm with EXPLAIN that the new index is used."
	),
}
_FILTER_LEAD = _TYPE_LEADS["Full Table Scan"]
# What an index on the sort or group column removes, for the finding types about it.
_SERVES: dict[str, str] = {"Filesort": "the sort", "Temporary Table": "the temporary table"}
# A table card's neutral verdict when its no-code advice is no verdict on the index (U2).
NO_VERDICT = "Optimus cannot say whether this index would help."

_ENSURE_FUNCTION = '''def ensure_indexes():
	"""Create each index in INDEXES once. One failed entry never stops the others."""
	previous = None
	try:
		# commit what ran before, so a rollback below undoes only this function's own work
		frappe.db.commit()
		previous = _lock_wait()
	except Exception:
		_rollback()
	try:
		for entry in INDEXES:
			try:
				# commit what ran before this entry, so the rollback below undoes only this entry
				frappe.db.commit()
				_ensure_index(entry)
				frappe.db.commit()
			except Exception as error:
				_rollback()
				with contextlib.suppress(Exception):
					frappe.log_error(
						title=_title(entry, f"was not created ({type(error).__name__})"),
						reference_doctype="DocType",
						reference_name=entry["doctype"],
					)
					frappe.db.commit()
				# a failed Error Log write must not leave a failed transaction for the next hook
				_rollback()
	finally:
		if previous is not None:
			try:
				_lock_wait(previous)
			except Exception:
				_rollback()


def _lock_wait(value=None):
	"""Cap how long this connection waits for a table lock at 300 seconds and return the old
	setting, so an index build on a busy table gives up instead of holding every later query
	on that table behind it. Called with that old setting, put it back."""
	if frappe.db.db_type == "mariadb":
		read, cap = "select @@session.lock_wait_timeout", 300
		write = "set session lock_wait_timeout = %s"
	elif frappe.db.db_type == "postgres":
		read, cap = "select current_setting('lock_timeout')", "300s"
		write = "select set_config('lock_timeout', %s, false)"
	else:
		return None
	previous = frappe.db.sql(read)[0][0] if value is None else None
	frappe.db.sql(write, (cap if value is None else value,))
	return previous


def _rollback():
	# on Postgres a failed statement aborts the transaction until it is rolled back
	with contextlib.suppress(Exception):
		frappe.db.rollback()


def _title(entry, what):
	# the key and what happened first: a cut to 140 characters (Error Log.method) can only
	# shorten the DocType, which reference_name also holds
	key = entry.get("index_name") or entry.get("search_index_field")
	return f"ensure_indexes: {key} {what} on {entry['doctype']}"[:140]


def _ensure_index(entry):
	doctype = entry["doctype"]
	if entry.get("db", frappe.db.db_type) != frappe.db.db_type:
		title = _title(entry, f"skipped on {frappe.db.db_type} (the entry is for {entry['db']})")
		# one row per entry, looked up by the reference columns: MariaDB indexes one of them,
		# never the title (method); on Postgres Frappe may not have indexed either
		reference = {"reference_doctype": "DocType", "reference_name": doctype}
		if not frappe.db.exists("Error Log", {**reference, "method": title}):
			frappe.log_error(title=title, **reference)
		return
	if not frappe.db.table_exists(doctype, cached=False):
		return
	field = entry.get("search_index_field")
	if field:
		if not frappe.db.has_column(doctype, field):
			return
		# the index first: a failed build leaves no Property Setter that a later sync of
		# this DocType would act on outside this guard
		if not frappe.db.get_column_index(f"tab{doctype}", field, unique=False):
			frappe.db.add_index(doctype, [field], index_name=f"{field}_index")
		# then Search Index on the field, so Frappe's schema sync keeps the index
		if not frappe.db.exists(
			"Property Setter",
			{"doc_type": doctype, "field_name": field, "property": "search_index", "value": "1"},
		):
			from frappe.custom.doctype.property_setter.property_setter import make_property_setter

			make_property_setter(doctype, field, "search_index", 1, "Check", validate_fields_for_doctype=False)
		return
	columns = entry["columns"]
	if not all(frappe.db.has_column(doctype, column.split("(", 1)[0]) for column in columns):
		return
	if frappe.db.has_index(f"tab{doctype}", entry["index_name"]):
		return
	frappe.db.add_index(doctype, columns, index_name=entry["index_name"])
'''


@dataclass(frozen=True)
class IndexAdvice:
	"""One piece of index advice. ``columns`` are what ``frappe.db.add_index`` takes
	(``col(255)`` is a MariaDB text prefix); ``reason`` is the route's explanation;
	``lead`` is the finding type's opening sentence (findings only); ``entry`` is the
	``ensure_indexes()`` entry for ``ROUTE_ENSURE_INDEXES``. ``unknown`` marks a
	``ROUTE_NO_CODE`` that is no verdict on the index, only a sign that Optimus could not
	tell (no evidence for the table, a filter it could not read, a query too long to parse),
	so a table card never says "Do not add this index." for it (U2, E6)."""

	route: str
	doctype: str
	table: str
	columns: tuple[str, ...]
	reason: str
	caveats: tuple[str, ...] = ()
	entry: dict | None = None
	app_name: str = UNKNOWN_APP
	lead: str = ""
	unknown: bool = False

	@property
	def code(self) -> str | None:
		if self.route != ROUTE_ENSURE_INDEXES or not self.entry:
			return None
		return ensure_indexes_code([self.entry], app_name=self.app_name)


def doctype_of(table: str) -> str | None:
	"""``"Sales Invoice"`` for ``tabSales Invoice`` (backticks allowed), else None."""
	name = str(table or "").strip().strip("`")
	if not _TAB_TABLE_RE.fullmatch(name):
		return None
	return name[3:]


def apply_metadata_rule(columns: list[str]) -> list[str]:
	"""Drop Frappe metadata columns, except a trailing run of ``creation`` /
	``modified`` after at least one business column."""
	cols = [c for c in columns if c]
	out: list[str] = []
	for i, col in enumerate(cols):
		low = col.lower()
		if low not in FRAPPE_METADATA_COLUMNS:
			out.append(col)
			continue
		rest_ok = all(c.lower() in TRAILING_METADATA_OK for c in cols[i:])
		if out and low in TRAILING_METADATA_OK and rest_ok:
			out.append(col)
	return out


def optimus_index_name(doctype: str, base_cols) -> str:
	"""``idx_<slug>_<hash8>``: at most 53 characters (MariaDB allows 64, Postgres 63) and
	unique across the schema, because the hash covers the table and the columns."""
	slug = re.sub(r"[^a-z0-9]", "_", str(doctype).lower())[:40]
	key = "tab" + str(doctype) + "|" + ",".join(base_cols)
	return f"idx_{slug}_{hashlib.sha1(key.encode('utf-8'), usedforsecurity=False).hexdigest()[:8]}"


def parse_query(query: str) -> dict:
	"""The table-breakdown analyzer's parser (pure)."""
	from optimus.analyzers.table_breakdown import _parse_query

	return _parse_query(query)


def _detail(finding: dict) -> dict:
	detail = finding.get("technical_detail")
	if isinstance(detail, dict):
		return detail
	try:
		parsed = json.loads(finding.get("technical_detail_json") or "{}")
	except (TypeError, ValueError):
		return {}
	return parsed if isinstance(parsed, dict) else {}


def _cols_text(columns) -> str:
	return "(" + ", ".join(columns) + ")"


def _aggregated(query: str, col: str) -> bool:
	pattern = (
		r"\b(?:" + "|".join(_AGGREGATES) + r")\s*\(\s*(?:distinct\s+)?(?:`[^`]+`\.|\w+\.)?`?"
		+ re.escape(col) + r"`?\s*\)"
	)
	return re.search(pattern, query, re.IGNORECASE) is not None


def _explain_columns(ftype: str, table: str, query: str, parse) -> tuple[str, list[tuple[str, str]]]:
	"""(real table name, ``[(clause, column)]``) for an EXPLAIN-family or Slow Query
	finding, parsed from its normalized query. An ORDER BY column that the query
	aggregates (``ORDER BY total`` with ``total = sum(amount)``) is left out (P9b)."""
	if not query:
		return table, []
	parsed = best_effort(lambda: parse(query), {})
	if not isinstance(parsed, dict):
		return table, []
	by_table = parsed.get("index_cols") or {}
	key = table if table in by_table else next((t for t in by_table if t.lower() == str(table).lower()), None)
	if key is None and not str(table).startswith("tab"):
		tab_tables = [t for t in (parsed.get("tables") or []) if str(t).startswith("tab")]
		if len(tab_tables) == 1:
			key = tab_tables[0]
	if key is None:
		return table, []
	out: list[tuple[str, str]] = []
	for labels in _CLAUSES_BY_TYPE.get(ftype, (("WHERE", "JOIN"),)):
		seen: set[str] = set()  # per clause group: a filter column can also be the sort column
		for label, col in by_table.get(key) or []:
			if label not in labels or col in seen:
				continue
			if label == "ORDER BY" and _aggregated(query, col):
				continue
			seen.add(col)
			out.append((label, col))
	return key, out


def _index_target(ftype: str, detail: dict, parse) -> tuple[str, list[tuple[str, str]]]:
	table = str(detail.get("table") or "").strip().strip("`")
	if ftype == "Missing Index":
		col = str(detail.get("column") or "").strip().strip("`")
		return table, ([("WHERE", col)] if col else [])
	return _explain_columns(ftype, table, str(detail.get("normalized_query") or ""), parse)


def _unindexable(evidence: TableEvidence, col: str) -> bool:
	"""True for a column a plain index cannot cover: its database type says so, or it is a
	JSON field, which MariaDB's information_schema reports as longtext (C4)."""
	field = evidence.fields.get(col)
	return col in evidence.unindexable_columns or (field is not None and field.fieldtype == "JSON")


def _clean_columns(columns, *, cap: bool = True) -> list[str]:
	"""Valid, de-duplicated column names. A text prefix the advisor itself wrote
	(``remarks(255)``) is read back as its bare column, so advising a card's own advice
	again gives the same advice."""
	out: list[str] = []
	seen: set[str] = set()
	names = [_PREFIX_SUFFIX_RE.sub("", c) for c in columns or [] if isinstance(c, str)]
	for col in apply_metadata_rule([c for c in names if _IDENT_RE.fullmatch(c)]):
		if col.lower() in seen:
			continue
		seen.add(col.lower())
		out.append(col)
	return out[:MAX_INDEX_COLUMNS] if cap else out


# --- predicate shape -------------------------------------------------------------
# sql_metadata (the table-breakdown parser) lists a query's WHERE columns but not how
# they combine, and Frappe's normalize_query turns every literal into ?, so a LIKE
# pattern is never visible (frappe/recorder.py:154-179). A conservative token scan of the
# main query's WHERE clause decides which WHERE columns a composite index can use and how
# (equality or range). A column is usable when at least one top-level AND piece compares
# it plainly:
# - not inside a function call (IFNULL(), YEAR(), DATE(), LOWER(), NOT (...), any
#   name( ... ) or a CASE ... END expression;
# - not inside a bracket level that has an OR, unless every branch of that OR compares
#   the same one column by =, IS NULL or IN (also at the top level: Frappe writes a lone
#   "is not set" filter as a bare `col IS NULL OR col = ?`);
# - not by a LIKE / ILIKE / RLIKE / REGEXP whose pattern may start with a wildcard;
# - not next to an arithmetic operator, and not compared with another column of the
#   same table.
# A dotted reference counts only when its qualifier is the target table or one of its
# aliases, and a (SELECT ...) group is skipped whole. Fail closed: a WHERE column the scan
# cannot place is treated as unusable ("unsure"): an unbalanced bracket or quote, a
# clause that ends on an operator, a Slow Query (cut at QUERY_TEXT_LIMIT characters)
# whose WHERE clause did not reach its end keyword, no top-level WHERE, a column it
# cannot find.

_SQL_TOKEN_RE = re.compile(
	r"`[^`]*`|'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.)*\"|/\*.*?\*/|--[^\n]*|#[^\n]*"
	r"|%\([A-Za-z_]\w*\)s|%s|[A-Za-z_][A-Za-z0-9_$]*|\|\||&&|<=>|<>|!=|<=|>=|\S",
	re.DOTALL,
)
_COMMENT_RE = re.compile(r"^(?:/\*|--|#)")
# A double-quoted token is a Postgres identifier; on MariaDB a normalized query has no
# string literals left (every literal is ?), so reading it as a name is safe there too.
_NAME_RE = re.compile(r'^(?:`[^`]*`|"[^"]*"|[A-Za-z_][A-Za-z0-9_$]*)$')
_BARE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_VALUE_RE = re.compile(r"^(?:\?|%s|%\([A-Za-z_]\w*\)s|'.*'|\".*\"|-?\d+(?:\.\d+)?)$", re.DOTALL)
_WHERE_ENDS: frozenset[str] = frozenset({
	"group", "order", "limit", "having", "union", "for", "lock", "window", "into", "procedure",
})
_OR_WORDS: frozenset[str] = frozenset({"or", "xor", "||"})
_AND_WORDS: frozenset[str] = frozenset({"and", "&&"})
_LIKE_WORDS: frozenset[str] = frozenset({"like", "ilike", "rlike", "regexp"})
# A "(" right after one of these only groups; after any other bare name it opens a call.
# NOT is not here on purpose: NOT (...) negates, which an index cannot serve.
_GROUPING_WORDS: frozenset[str] = frozenset({
	"in", "exists", "any", "all", "some", "and", "or", "xor", "between", "is", "as", "on", "where",
	"values", "select", "case", "when", "then", "else",
}) | _LIKE_WORDS
# A clause cut short ends on one of these.
_DANGLING: frozenset[str] = frozenset({
	"and", "or", "xor", "not", "like", "ilike", "rlike", "regexp", "in", "is", "between", "=", "<", ">",
	"<>", "!=", "<=", ">=", "<=>", ",", ".", "&&", "||", "where",
})
_ARITHMETIC: frozenset[str] = frozenset({"+", "-", "*", "/", "%", "div", "mod", "^", "&", "|"})
_COMPARISONS: frozenset[str] = frozenset({"=", "<", ">", "<=", ">=", "<>", "!=", "<=>"})
_EQUALITY: frozenset[str] = frozenset({"=", "<=>"})
# A bare word on the other side of a comparison that is a value or a keyword, not a column.
_VALUE_WORDS: frozenset[str] = frozenset({
	"null", "true", "false", "unknown", "binary", "date", "time", "timestamp", "interval", "any", "all",
	"some", "case", "when", "then", "else", "end", "not", "exists", "select",
})
_VALUE_WORD_PREFIXES: tuple[str, ...] = ("current_", "localtime", "utc_")
_SHAPE_WHY: dict[str, str] = {
	"or": "compared only inside an OR between conditions",
	"like": "compared by a LIKE whose pattern can start with a wildcard",
	"function": "compared through the function {name}(), which an index on the column cannot use",
	"NOT": "compared inside a NOT (...), which an index on the column cannot use",
	"CASE": "compared inside a CASE expression, which an index on the column cannot use",
	"expression": "compared with another column or through arithmetic, which an index on the column cannot use",
	"unsure": "a filter Optimus could not place with certainty",
}
_FUNCTION = "function:"  # a kind "function:IFNULL" records the innermost call's name


def _where_end(tokens: list[str]) -> int | None:
	"""Index of the keyword that ends the main query's WHERE clause, or None when there
	is no top-level WHERE or the text stops before the clause ends."""
	depth = 0
	start = None
	for i, tok in enumerate(tokens):
		depth += {"(": 1, ")": -1}.get(tok, 0)
		if depth or tok in ("(", ")"):
			continue
		low = tok.lower()
		if start is None and low == "where":
			start = i
		elif start is not None and low in _WHERE_ENDS:
			return i
	return None


def _where_tokens(query: str, *, truncated: bool = False) -> list[str] | None:
	"""The tokens of the main query's WHERE clause ([] when it has none), or None when the
	query cannot be scanned with certainty (an unbalanced bracket or quote, or a clause
	that ends on an operator because the query was cut short). A ``truncated`` query
	(possibly cut at QUERY_TEXT_LIMIT characters) counts only when its WHERE clause reached
	its end keyword, and only the text before that keyword is checked."""
	tokens = [tok for tok in _SQL_TOKEN_RE.findall(query or "") if not _COMMENT_RE.match(tok)]
	if truncated:
		stop = _where_end(tokens)
		if stop is None:
			return None
		tokens = tokens[:stop]
	depth = 0
	for tok in tokens:
		if tok in ("`", "'", '"'):
			return None
		depth += {"(": 1, ")": -1}.get(tok, 0)
		if depth < 0:
			return None
	if depth:
		return None
	start = None
	clause = None
	for i, tok in enumerate(tokens):
		depth += {"(": 1, ")": -1}.get(tok, 0)
		if depth or tok in ("(", ")"):
			continue
		low = tok.lower()
		if start is None and low == "where":
			start = i + 1
		elif start is not None and low in _WHERE_ENDS:
			clause = tokens[start:i]
			break
	if start is None:
		return []
	if clause is None:
		clause = tokens[start:]
	if not clause or clause[-1].lower() in _DANGLING:
		return None
	return clause


def _bracket_pairs(tokens: list[str]) -> dict[int, int]:
	"""``{index of "(": index of its ")"}`` (the tokens are balanced)."""
	stack: list[int] = []
	pairs: dict[int, int] = {}
	for i, tok in enumerate(tokens):
		if tok == "(":
			stack.append(i)
		elif tok == ")" and stack:
			pairs[stack.pop()] = i
	return pairs


def _is_case(tok: str) -> bool:
	return tok.lower() == "case" and _BARE_NAME_RE.fullmatch(tok) is not None


def _case_close(tokens: list[str], i: int, end: int, pairs: dict[int, int]) -> int:
	"""Index of the END that closes the CASE at ``i`` (bracket groups jumped over), or
	``end`` when there is none."""
	depth = 0
	k = i
	while k < end:
		tok = tokens[k]
		if tok == "(":
			k = pairs.get(k, end) + 1
			continue
		if _BARE_NAME_RE.fullmatch(tok):
			low = tok.lower()
			if low == "case":
				depth += 1
			elif low == "end":
				depth -= 1
				if depth == 0:
					return k
		k += 1
	return end


def _group_close(tokens: list[str], i: int, end: int, pairs: dict[int, int]) -> int | None:
	"""Index that closes the bracket or CASE group opening at ``i``, or None."""
	if tokens[i] == "(":
		return pairs.get(i, end)
	if _is_case(tokens[i]):
		return _case_close(tokens, i, end, pairs)
	return None


def _conjuncts(tokens: list[str]) -> list[list[str]] | None:
	"""The pieces of a WHERE clause between its top-level ANDs (bracket and CASE groups
	kept whole), or None when an OR sits at its top level (AND binds tighter, so no piece
	is then sure to apply) unless every branch of that OR compares the same one column,
	which makes the whole clause one piece. The AND of a BETWEEN also splits: harmless,
	because a nested OR stays inside its group, so a finer split never moves a column out
	of an OR group."""
	pairs = _bracket_pairs(tokens)
	parts: list[list[str]] = [[]]
	i = 0
	while i < len(tokens):
		close = _group_close(tokens, i, len(tokens), pairs)
		if close is not None:
			parts[-1] += tokens[i : close + 1]
			i = close + 1
			continue
		low = tokens[i].lower()
		if low in _OR_WORDS:
			has_or, key = _level_or(tokens, 0, len(tokens), pairs)
			return [tokens] if has_or and key is not None else None
		if low in _AND_WORDS:
			parts.append([])
		else:
			parts[-1].append(tokens[i])
		i += 1
	return [part for part in parts if part]


def _chain_end(tokens: list[str], i: int) -> int:
	"""Index of the last name of the dotted reference that starts at ``i``."""
	j = i
	while j + 2 < len(tokens) and tokens[j + 1] == "." and _NAME_RE.fullmatch(tokens[j + 2]):
		j += 2
	return j


def _chain_start(tokens: list[str], j: int) -> int:
	"""Index of the first name of the dotted reference that ends at ``j``."""
	i = j
	while i >= 2 and tokens[i - 1] == "." and _NAME_RE.fullmatch(tokens[i - 2]):
		i -= 2
	return i


def _ref_key(tokens: list[str], i: int, j: int) -> tuple[str, str]:
	"""``(qualifier, lowercase column)`` of the reference ``tokens[i..j]``."""
	qualifier = tokens[j - 2].strip('`"') if j > i else ""
	return qualifier, tokens[j].strip('`"').lower()


def _single_column_branch(tokens: list[str], start: int, end: int, pairs: dict[int, int]):
	"""The ``(qualifier, column)`` that ``tokens[start:end]`` compares by ``= value``,
	``IS NULL`` or ``IN (values)``, else None."""
	if start >= end or not _NAME_RE.fullmatch(tokens[start]):
		return None
	j = _chain_end(tokens, start)
	rest = [tok.lower() for tok in tokens[j + 1 : end]]
	key = _ref_key(tokens, start, j)
	if len(rest) == 2 and rest[0] == "=" and _VALUE_RE.fullmatch(tokens[j + 2]):
		return key
	if rest == ["is", "null"]:
		return key
	if (
		len(rest) >= 3 and rest[:2] == ["in", "("] and pairs.get(j + 2) == end - 1
		and all(_VALUE_RE.fullmatch(tok) or tok == "," for tok in tokens[j + 3 : end - 1])
	):
		return key
	return None


def _level_or(tokens: list[str], start: int, end: int, pairs: dict[int, int]):
	"""``(True, key | None)`` when ``tokens[start:end]`` has an OR at its own level (bracket
	and CASE groups skipped); ``key`` is the ``(qualifier, column)`` every OR branch
	compares, when they all compare the same one column plainly. ``(False, None)``
	without an OR."""
	branches: list[tuple[int, int]] = []
	first = start
	i = start
	while i < end:
		close = _group_close(tokens, i, end, pairs)
		if close is not None:
			i = close + 1
			continue
		if tokens[i].lower() in _OR_WORDS:
			branches.append((first, i))
			first = i + 1
		i += 1
	if not branches:
		return False, None
	branches.append((first, end))
	keys = {_single_column_branch(tokens, a, b, pairs) for a, b in branches}
	return True, (keys.pop() if len(keys) == 1 and None not in keys else None)


def _wildcard_like(tokens: list[str], at: int) -> bool:
	"""True when ``tokens[at:]`` starts with ``[NOT] LIKE <pattern>`` (or ILIKE, RLIKE,
	REGEXP) and an index cannot serve it: a regular expression, a parameter (normalized
	queries hide every literal), anything but a string literal, or a literal that starts
	with % or _."""
	if at < len(tokens) and tokens[at].lower() == "not":
		at += 1
	if at >= len(tokens) or tokens[at].lower() not in _LIKE_WORDS:
		return False
	if tokens[at].lower() in ("rlike", "regexp"):
		return True
	pattern = tokens[at + 1] if at + 1 < len(tokens) else ""
	if pattern[:1] in ("'", '"') and len(pattern) > 1:
		return pattern[1:2] in ("%", "_")
	return True


def _column_partner(tokens: list[str], a: int, b: int, qualifiers) -> bool:
	"""True when ``tokens[a..b]`` is a column of the target table: a name chain that is
	not a call and not a value word (NULL, CURRENT_DATE, ...), whose qualifier is empty or
	a target qualifier. A join condition to another table is no partner."""
	if not _NAME_RE.fullmatch(tokens[a]) or (b + 1 < len(tokens) and tokens[b + 1] == "("):
		return False
	if a == b and _BARE_NAME_RE.fullmatch(tokens[a]):
		low = tokens[a].lower()
		if low in _VALUE_WORDS or low.startswith(_VALUE_WORD_PREFIXES):
			return False
	qualifier, _name = _ref_key(tokens, a, b)
	return qualifiers is None or not qualifier or qualifier in qualifiers


def _is_expression(tokens: list[str], i: int, j: int, qualifiers) -> bool:
	"""True when the reference ``tokens[i..j]`` sits next to an arithmetic operator or is
	compared with another column of the target table."""
	before = tokens[i - 1].lower() if i > 0 else ""
	after = tokens[j + 1].lower() if j + 1 < len(tokens) else ""
	if before in _ARITHMETIC or after in _ARITHMETIC:
		return True
	if after in _COMPARISONS and j + 2 < len(tokens):
		if _column_partner(tokens, j + 2, _chain_end(tokens, j + 2), qualifiers):
			return True
	if before in _COMPARISONS and i >= 2 and _NAME_RE.fullmatch(tokens[i - 2]):
		return _column_partner(tokens, _chain_start(tokens, i - 2), i - 2, qualifiers)
	return False


def _comparison(tokens: list[str], i: int, j: int) -> str:
	"""``"eq"`` when the plain reference ``tokens[i..j]`` is compared by =, <=> or IS NULL,
	``"in"`` for IN (...) (equality on several values), else ``"range"`` (<, >, BETWEEN,
	<>, NOT ..., IS NOT NULL, a prefix LIKE, or a shape the scan does not know, which is
	never treated as equality)."""
	after = [tok.lower() for tok in tokens[j + 1 : j + 3]]
	first = after[0] if after else ""
	if first == "in":
		return "in"
	if first in _EQUALITY:
		return "eq"
	if first == "is":
		return "eq" if after[1:] == ["null"] else "range"
	if not first or first in _AND_WORDS:
		before = tokens[i - 1].lower() if i > 0 else ""
		return "eq" if before in _EQUALITY else "range"
	return "range"


def _conjunct_refs(tokens: list[str], qualifiers) -> list[tuple[str, set[str], str]]:
	"""``(lowercase column, kinds, comparison)`` for each column reference in one AND
	piece; empty kinds is a plain use, compared ``"eq"``, ``"in"`` (several values: IN, or
	a same-column ``IS NULL OR =``) or ``"range"``. A reference qualified by another table
	(``acc.company``) and everything inside a ``(SELECT ...)`` group is skipped."""
	pairs = _bracket_pairs(tokens)
	out: list[tuple[str, set[str], str]] = []

	def walk(start: int, end: int, frames: list[tuple[str | None, bool, tuple | None]]) -> None:
		i = start
		while i < end:
			tok = tokens[i]
			if tok == "(":
				close = pairs.get(i, end)
				prev = tokens[i - 1] if i > 0 else ""
				inner = tokens[i + 1].lower() if i + 1 < close else ""
				if inner not in ("select", "with"):
					call = prev.upper() if _BARE_NAME_RE.fullmatch(prev) and prev.lower() not in _GROUPING_WORDS else None
					has_or, or_key = _level_or(tokens, i + 1, close, pairs)
					walk(i + 1, close, [*frames, (call, has_or, or_key)])
				i = close + 1
				continue
			if _is_case(tok):
				close = _case_close(tokens, i, end, pairs)
				walk(i + 1, close, [*frames, ("CASE", False, None)])
				i = close + 1
				continue
			if not _NAME_RE.fullmatch(tok):
				i += 1
				continue
			j = _chain_end(tokens, i)
			if j + 1 < end and tokens[j + 1] == "(":
				i = j + 1  # a call's name; the "(" opens its frame
				continue
			qualifier, name = _ref_key(tokens, i, j)
			if qualifiers is None or not qualifier or qualifier in qualifiers:
				kinds: set[str] = set()
				call = next((frame[0] for frame in reversed(frames) if frame[0]), None)
				if call:
					kinds.add(_FUNCTION + call)
				if any(has_or and or_key != (qualifier, name) for _call, has_or, or_key in frames):
					kinds.add("or")
				if not call and _wildcard_like(tokens, j + 1):
					kinds.add("like")
				if _is_expression(tokens, i, j, qualifiers):
					kinds.add("expression")
				multi = any(has_or and or_key == (qualifier, name) for _call, has_or, or_key in frames)
				out.append((name, kinds, "" if kinds else "in" if multi else _comparison(tokens, i, j)))
			i = j + 1

	# the top level has an OR only when every branch compares one column (_conjuncts)
	top_or, top_key = _level_or(tokens, 0, len(tokens), pairs)
	walk(0, len(tokens), [(None, top_or, top_key)])
	return out


def _scan_where(query: str, labelled, qualifiers=None, *, truncated: bool = False):
	"""``(unusable, comparisons)`` for the WHERE columns of ``labelled``: ``unusable`` is
	``{column: kinds}`` for each column a composite index cannot use (a kind is ``"or"``,
	``"like"``, ``"function:<NAME>"``, ``"expression"`` or ``"unsure"``, see the section
	comment), ``comparisons`` is ``{column: "eq" | "in" | "range"}`` for the usable ones."""
	where_cols: list[str] = []
	for label, col in labelled or []:
		if label == "WHERE" and col not in where_cols:
			where_cols.append(col)
	if not where_cols:
		return {}, {}
	tokens = _where_tokens(query, truncated=truncated)
	if tokens is None:
		return {col: {"unsure"} for col in where_cols}, {}
	conjuncts = _conjuncts(tokens)
	if conjuncts is None:
		return {col: {"or"} for col in where_cols}, {}
	usable: dict[str, str] = {}
	kinds: dict[str, set[str]] = defaultdict(set)
	rank = {"eq": 0, "in": 1, "range": 2}  # the most selective plain use wins
	for part in conjuncts:
		for name, ref_kinds, comparison in _conjunct_refs(part, qualifiers):
			if ref_kinds:
				kinds[name] |= ref_kinds
			elif name not in usable or rank[comparison] < rank[usable[name]]:
				usable[name] = comparison
	unusable = {
		col: set(kinds.get(col.lower()) or {"unsure"}) for col in where_cols if col.lower() not in usable
	}
	return unusable, {col: usable[col.lower()] for col in where_cols if col.lower() in usable}


def _unusable_where_columns(query: str, labelled, qualifiers=None, *, truncated: bool = False) -> dict[str, set[str]]:
	"""``{column: kinds}`` for each WHERE column of ``labelled`` that a composite index
	cannot use (``_scan_where``). ``qualifiers`` (the target table and its aliases) limits
	which dotted references count; None counts every one."""
	return _scan_where(query, labelled, qualifiers, truncated=truncated)[0]


def table_aliases(query: str) -> dict:
	"""sql_metadata's ``tables_aliases`` for ``query`` (``{alias: table}``), {} when it
	cannot parse the query. A second parse of the query, so a render memoises it per query
	text (``recipe_enrichment.make_query_parser``, PF2)."""

	def aliases() -> dict:
		from sql_metadata import Parser

		return dict(Parser(query, disable_logging=True).tables_aliases or {})

	found = best_effort(aliases, {})
	return found if isinstance(found, dict) else {}


def _target_qualifiers(query: str, table: str, aliases: Callable[[str], dict] | None = None) -> frozenset[str]:
	"""The target table's name and its aliases. ``aliases(query)`` gives the alias map
	(``table_aliases``, or a per-render memo of it)."""
	found = (aliases or table_aliases)(query)
	return frozenset({table} | {alias for alias, real in (found or {}).items() if real == table})


def _union_branches(query: str, qualifiers) -> int:
	"""How many branches of the main query's top-level UNION filter the target table: a
	branch with a WHERE of its own that names the table or one of its aliases outside
	brackets. A query without a top-level UNION is one branch (E7)."""
	tokens = [tok for tok in _SQL_TOKEN_RE.findall(query or "") if not _COMMENT_RE.match(tok)]
	branches: list[list[str]] = [[]]
	depth = 0
	for tok in tokens:
		depth += {"(": 1, ")": -1}.get(tok, 0)
		if not depth and tok not in ("(", ")") and tok.lower() == "union":
			branches.append([])
			continue
		if not depth and tok not in ("(", ")"):
			branches[-1].append(tok)
	names = {str(q).lower() for q in qualifiers}
	return sum(
		1 for branch in branches
		if any(tok.lower() == "where" for tok in branch) and any(tok.strip('`"').lower() in names for tok in branch)
	)


def _index_order(
	cols: list[str], comparisons: Mapping[str, str], *, serves: str = "",
) -> tuple[list[str], list[tuple[str, str]]]:
	"""Recipe columns in index order: equality columns first, then the sort or group
	columns. A range column (a sort column that is also range-filtered counts as the sort
	column, one index serving both) cannot share an index with a sort column after it:
	when the index ``serves`` a sort or a grouping (Filesort, Temporary Table) the range
	filter is left out, otherwise one range column follows the equality columns and the
	rest is left out. Each left-out column comes with the reason."""
	eq = [col for col in cols if comparisons.get(col, "eq") in ("eq", "in")]
	ranges = [col for col in cols if comparisons.get(col) == "range"]
	sorts = [col for col in cols if comparisons.get(col) == "sort"]
	if not ranges:
		return eq + sorts, []
	if serves and sorts:
		return eq + sorts, [
			(col, f"the range filter on {col} cannot also use this index, which removes {serves} instead")
			for col in ranges
		]
	why = f"it comes after the range condition on {ranges[0]}, so the index cannot use it"
	return eq + ranges[:1], [(col, why) for col in ranges[1:] + sorts]


_CLAUSE_ENDS: frozenset[str] = frozenset({
	"limit", "for", "lock", "union", "having", "order", "group", "window", "offset", "into", "procedure",
	"with",
})


def _clause_items(tokens: list[str], keyword: str) -> list[list[str]] | None:
	"""The comma-separated items of the main query's ``<keyword> BY`` clause (ORDER BY or
	GROUP BY at depth 0), [] when the query has none."""
	depth = 0
	start = None
	for i, tok in enumerate(tokens):
		depth += {"(": 1, ")": -1}.get(tok, 0)
		if depth or tok in ("(", ")"):
			continue
		low = tok.lower()
		if start is None:
			if low == keyword and i + 1 < len(tokens) and tokens[i + 1].lower() == "by":
				start = i + 2
		elif low in _CLAUSE_ENDS and i > start:
			return _split_items(tokens[start:i])
	return _split_items(tokens[start:]) if start is not None else []


def _split_items(tokens: list[str]) -> list[list[str]]:
	items: list[list[str]] = [[]]
	depth = 0
	for tok in tokens:
		depth += {"(": 1, ")": -1}.get(tok, 0)
		if tok == "," and not depth:
			items.append([])
		else:
			items[-1].append(tok)
	return items


def _select_aliases(tokens: list[str]) -> set[str]:
	"""Lowercase names given with AS in the main query's select list."""
	aliases: set[str] = set()
	depth = 0
	for i, tok in enumerate(tokens):
		depth += {"(": 1, ")": -1}.get(tok, 0)
		if depth:
			continue
		if tok.lower() == "from":
			break
		if tok.lower() == "as" and i + 1 < len(tokens) and _NAME_RE.fullmatch(tokens[i + 1]):
			aliases.add(tokens[i + 1].strip('`"').lower())
	return aliases


def _aggregate_aliases(tokens: list[str]) -> set[str]:
	"""Lowercase names the main query's select list gives an aggregate: ``SUM(x) AS total``,
	or ``SUM(x) total`` right before a comma or FROM."""
	pairs = _bracket_pairs(tokens)
	out: set[str] = set()
	depth = 0
	for i, tok in enumerate(tokens):
		if not depth and tok.lower() == "from":
			break
		if tok == "(" and not depth and i and tokens[i - 1].lower() in _AGGREGATES and i in pairs:
			j = pairs[i] + 1
			explicit = j < len(tokens) and tokens[j].lower() == "as"
			j += explicit
			after = tokens[j + 1].lower() if j + 1 < len(tokens) else ""
			if j < len(tokens) and _NAME_RE.fullmatch(tokens[j]) and tokens[j].lower() != "from" and (
				explicit or after in (",", "from")
			):
				out.add(tokens[j].strip('`"').lower())
		depth += {"(": 1, ")": -1}.get(tok, 0)
	return out


def _aggregate_item(item: list[str], aggregate_aliases: set[str]) -> bool:
	"""True when one ORDER BY item is an aggregate (``SUM(x) DESC``) or a select alias of one."""
	if len(item) > 1 and item[0].lower() in _AGGREGATES and item[1] == "(":
		return True
	return bool(item) and _NAME_RE.fullmatch(item[0]) is not None and item[0].strip('`"').lower() in aggregate_aliases


def _sort_problem(query: str, ftype: str, qualifiers, evidence: TableEvidence | None) -> str:
	"""Why an index on the ORDER BY (Filesort) or GROUP BY (Temporary Table) columns
	cannot return the rows in order, or "" when it can: every item of that clause is a bare
	column of the target table (one name, qualified by the table or an alias or not at all,
	ASC or DESC, all in one direction, a real column and no select alias, one an index can
	order by: not text, not JSON) and the other clause, when there is one, names the same
	columns. Otherwise (C7):

	- ``"aggregate"``: the ORDER BY sorts by an aggregate (``SUM(x)``, or a select alias
	  of one), the sort column of a Filesort or the order of a Temporary Table's groups;
	- ``"differs"``: GROUP BY and ORDER BY name different columns;
	- ``"distinct"``: a Temporary Table query with no GROUP BY whose SELECT is DISTINCT;
	- ``"none"``: no such clause at all;
	- ``"expression"``: anything else (a function, FIELD(), CASE, arithmetic or a parameter
	  around an item, a select alias, a text column, mixed directions), or no evidence."""
	if evidence is None:
		return "expression"
	tokens = [tok for tok in _SQL_TOKEN_RE.findall(query or "") if not _COMMENT_RE.match(tok)]
	order, group = _clause_items(tokens, "order"), _clause_items(tokens, "group")
	aggregates = _aggregate_aliases(tokens)
	if any(_aggregate_item(item, aggregates) for item in order or []):
		return "aggregate"
	main, other = (group, order) if ftype == "Temporary Table" else (order, group)
	if not main:
		first = next((i for i, tok in enumerate(tokens) if tok.lower() == "select"), None)
		distinct = first is not None and first + 1 < len(tokens) and tokens[first + 1].lower() in ("distinct", "distinctrow")
		return "distinct" if ftype == "Temporary Table" and distinct else "none"
	columns = {col.lower(): col for col in evidence.column_types}
	aliases = _select_aliases(tokens)
	names: list[str] = []
	directions: set[str] = set()
	for item in main:
		if not item or not _NAME_RE.fullmatch(item[0]):
			return "expression"
		j = _chain_end(item, 0)
		rest = [tok.lower() for tok in item[j + 1 :]]
		if rest not in ([], ["asc"], ["desc"]):
			return "expression"
		qualifier, name = _ref_key(item, 0, j)
		col = columns.get(name)
		if (
			(qualifier and qualifiers is not None and qualifier not in qualifiers) or col is None
			or name in aliases or col in evidence.text_columns or _unindexable(evidence, col)
		):
			return "expression"
		names.append(name)
		directions.add(rest[0] if rest else "asc")
	if len(directions) > 1:
		return "expression"
	other_names = [_ref_key(item, 0, _chain_end(item, 0))[1] for item in other if item and _NAME_RE.fullmatch(item[0])]
	return "" if not other or other_names == names else "differs"


def _function_names(kinds: set[str]) -> list[str]:
	return sorted(kind[len(_FUNCTION) :] for kind in kinds if kind.startswith(_FUNCTION))


def _function_phrase(name: str, col: str) -> str:
	if name == "NOT":
		return f"a NOT (...) around {col}"
	if name == "CASE":
		return f"a CASE expression around {col}"
	return f"the function {name}() wrapped around {col}"


def _shape_phrases(shapes: Mapping[str, set[str]]) -> list[str]:
	like = [col for col, kinds in shapes.items() if "like" in kinds]
	ored = [col for col, kinds in shapes.items() if "or" in kinds]
	out: list[str] = []
	if like:
		out.append(f"a LIKE on {', '.join(like)}, which cannot use an index when its pattern starts with a wildcard")
	if ored:
		out.append(f"an OR between conditions on {', '.join(ored)}")
	for col, kinds in shapes.items():
		names = _function_names(kinds)
		out += [_function_phrase(name, col) for name in names]
		if "expression" in kinds and not names:
			out.append(f"arithmetic on {col}")
	return out


def _shape_why(kinds: set[str]) -> str:
	"""Why one column was left out, by precedence: or, like, function, expression, unsure."""
	if "or" in kinds:
		return _SHAPE_WHY["or"]
	if "like" in kinds:
		return _SHAPE_WHY["like"]
	names = _function_names(kinds)
	if names:
		return _SHAPE_WHY.get(names[0]) or _SHAPE_WHY["function"].format(name=names[0])
	if "expression" in kinds:
		return _SHAPE_WHY["expression"]
	return _SHAPE_WHY["unsure"]


def _shape_no_code_reason(shapes: Mapping[str, set[str]], checks: list[str]) -> tuple[str, bool]:
	"""``(text, unknown)`` for a NO_CODE when the filter leaves no column an index could
	narrow on: the filter shape, the columns Optimus could not read, and any Check fields
	left. ``unknown`` is True when part of the filter could not be read, so the text is no
	verdict on the index."""
	known = {col: kinds for col, kinds in shapes.items() if kinds != {"unsure"}}
	unsure = [col for col, kinds in shapes.items() if kinds == {"unsure"}]
	explain = "Check the query with EXPLAIN to see which index it needs."
	if not known and not checks:
		return (
			"Optimus could not read how this query combines its filters (it may be cut short, wrapped in "
			f"brackets, a UNION or a derived table), so it gives no index code. {explain}"
		), True
	parts: list[str] = []
	if known:
		parts.append(
			f"The cost comes from the shape of the filter: {'; '.join(_shape_phrases(known))}. A composite "
			"index cannot use those columns."
		)
	if unsure:
		parts.append(f"Optimus could not read how the query filters on {', '.join(unsure)}.")
	if len(checks) == 1:
		parts.append(f"{checks[0]} is a Check field, which matches too many rows for an index to narrow.")
	elif checks:
		parts.append(f"{', '.join(checks)} are Check fields, which match too many rows for an index to narrow.")
	if unsure:
		parts.append(f"So Optimus gives no index code. {explain}")
		return " ".join(parts), True
	parts.append("So an index would not help, and Optimus gives no index code.")
	if known:
		parts.append(
			"Rewrite the filter (an exact match instead of a LIKE, the bare column instead of a function "
			"or arithmetic around it, or one query per OR branch) and check the result with EXPLAIN."
		)
	else:
		parts.append("Filter on a more selective field as well, and check the result with EXPLAIN.")
	return " ".join(parts), False


def _existing_tail(query: str, shapes: Mapping[str, set[str]]) -> str:
	"""What to check when the index exists already (U4). It names only the filter shapes
	the scan found on the query's other columns, never a list of shapes the query may not
	have; a filter with none looks index-friendly. A table card or a Missing Index finding
	has no query, so it is never told to rewrite a filter."""
	known = {col: kinds for col, kinds in shapes.items() if kinds != {"unsure"}}
	unsure = [col for col, kinds in shapes.items() if kinds == {"unsure"}]
	check = "check the query with EXPLAIN to see which index it uses and how many rows it reads"
	if known:
		return (
			f"The cost comes from how the query filters: {'; '.join(_shape_phrases(known))}. Rewrite the filter "
			"so an index can be used, and check the result with EXPLAIN."
		)
	if unsure:
		return f"Optimus could not read how the query filters on {', '.join(unsure)}, so {check}."
	if query:
		return f"The query's filter looks index-friendly, so {check}."
	return "Check the slow queries on this column with EXPLAIN to see which index they use and how many rows they read."


def _explain_index_names(explain_row) -> list[tuple[str, bool]]:
	"""``[(index name, used)]`` from a MariaDB EXPLAIN row (``key`` is used,
	``possible_keys`` can be used) or a Postgres plan node (``Index Name``)."""
	row = explain_row
	if isinstance(row, str):
		try:
			row = json.loads(row)
		except ValueError:
			return []
	if not isinstance(row, dict):
		return []
	out: list[tuple[str, bool]] = []
	key = row.get("key") or row.get("Index Name")
	if isinstance(key, str) and key.strip():
		out.append((key.strip(), True))
	possible = row.get("possible_keys")
	if isinstance(possible, str):
		out += [(name.strip(), False) for name in possible.split(",") if name.strip()]
	return out


def _reserved(evidence: TableEvidence, cols) -> list[str]:
	"""The columns of ``cols`` that are MariaDB reserved words, on MariaDB only: Postgres
	``add_index`` quotes every identifier (E3)."""
	if evidence.dialect == "postgres":
		return []
	return [c for c in cols if c.lower() in MARIADB_RESERVED_WORDS]


def _column_problem(evidence: TableEvidence, cols: list[str]) -> str | None:
	by_lower = {c.lower(): c for c in evidence.column_types}
	for col in cols:
		if col not in evidence.column_types:
			actual = by_lower.get(col.lower())
			if actual:
				return (
					f'The query names "{col}", but the column on table "{evidence.table}" is "{actual}", '
					"so Optimus gives no index code. Check the column name in the query."
				)
			return (
				f'Table "{evidence.table}" has no column "{col}", so Optimus gives no index code. '
				"The query may name a field that was removed or renamed."
			)
		if col.lower() not in FRAPPE_METADATA_COLUMNS and col not in evidence.fields:
			return (
				f'Column "{col}" of table "{evidence.table}" is not a field of DocType "{evidence.doctype}", '
				"so Frappe does not manage it and Optimus gives no index code."
			)
	reserved = _reserved(evidence, cols)
	if reserved:
		return (
			f'Column "{reserved[0]}" is a reserved word in MariaDB, and frappe.db.add_index writes column '
			"names without quotes, so building the index would fail. Optimus gives no index code for it."
		)
	return None


def _existing_index_problem(
	evidence: TableEvidence, cols: list[str], explain_row, query: str, shapes: Mapping[str, set[str]],
) -> str | None:
	"""NO_CODE when the FINAL recipe (``cols``, bare names) exists already (C1). A recipe of
	several columns is refused only when an existing index already starts with all of them:
	its first column leading an index of its own, or one EXPLAIN names, says nothing about
	the composite, so the verdict never depends on the order of the query's predicates. A
	single-column recipe is refused when that column is unique on its own (a Unique field or
	a one-column unique index; a composite unique index it leads is just an index it leads,
	C6), leads an existing index under any name, or leads an index EXPLAIN names. Search
	Index is never proof (C2): on Postgres a Search Index is named after the bare field and
	index names are schema-wide, so only the first table with that field name gets one; the
	table's real index list decides. creation and modified never stand alone in a recipe
	(``apply_metadata_rule``), so Frappe's own creation index never refuses one (D5)."""
	whole = tuple(cols)
	tail = _existing_tail(query, shapes)
	if len(whole) > 1:
		covering = next((ix for ix in evidence.indexes if ix.columns[: len(whole)] == whole), None)
		if covering is None:
			return None
		return (
			f'The index "{covering.name}" on table "{evidence.table}" already starts with {_cols_text(whole)}, '
			f"so this index exists already and a new one would not help. {tail}"
		)
	col = whole[0]
	field = evidence.fields.get(col)
	if (field is not None and field.unique) or any(ix.unique and ix.columns == (col,) for ix in evidence.indexes):
		return f'Column "{col}" is already unique, so the database already has an index on it. {tail}'
	led = next((ix for ix in evidence.indexes if ix.columns[:1] == (col,)), None)
	if led is not None:
		return (
			f'Column "{col}" already leads the index "{led.name}" on table "{evidence.table}", so a new '
			f"index would not help. {tail}"
		)
	for name, used in _explain_index_names(explain_row):
		named = next((ix for ix in evidence.indexes if ix.name == name), None)
		if (named is not None and named.columns[:1] == (col,)) or name in (col, f"{col}_index"):
			how = "uses" if used else "can use"
			return (
				f'EXPLAIN shows the database {how} the index "{name}" on column "{col}", so a new index '
				f"would not help. {tail}"
			)
	return None


def _key_bytes(evidence: TableEvidence, columns: list[str]) -> int:
	total = 0
	for col in columns:
		base = col.split("(", 1)[0]
		if col != base:
			total += TEXT_INDEX_PREFIX * BYTES_PER_CHAR
		elif evidence.column_types.get(base, "") in _VARCHAR_TYPES:
			field = evidence.fields.get(base)
			length = (field.length if field is not None else 0) or VARCHAR_DEFAULT_LENGTH
			total += int(length) * BYTES_PER_CHAR
		else:
			total += FIXED_WIDTH_BYTES
	return total


def _index_columns(evidence: TableEvidence, cols: list[str]) -> tuple[list[str], list[tuple[str, str]], str | None]:
	"""(index columns, ``[(left-out column, why)]``, NO_CODE reason or None)."""
	postgres = evidence.dialect == "postgres"
	final: list[str] = []
	dropped: list[tuple[str, str]] = []
	for i, col in enumerate(cols):
		if _unindexable(evidence, col):
			if i == 0:
				field = evidence.fields.get(col)
				what = (
					"is a JSON field" if field is not None and field.fieldtype == "JSON"
					else f"has the type {evidence.column_types.get(col, '')}"
				)
				return [], [], (
					f'Column "{col}" {what}, which a plain index cannot cover, so Optimus gives no index code.'
				)
			dropped.append((col, "a type a plain index cannot cover"))
			continue
		if col in evidence.text_columns:
			if postgres:
				if i == 0:
					return [], [], (
						f'Column "{col}" is a text column. On Postgres an index stores the whole value and '
						"refuses a value longer than about 2.7 KB, so Optimus gives no index code. "
						"Filtering on a shorter Data field avoids the problem."
					)
				dropped.append((col, "a text column, which a Postgres index cannot hold safely"))
				continue
			final.append(f"{col}({TEXT_INDEX_PREFIX})")
			continue
		final.append(col)
	if postgres:
		limit, limit_name, may = POSTGRES_MAX_INDEX_ROW_BYTES, "Postgres index row", "could"
	else:
		limit, limit_name, may = MARIADB_MAX_KEY_BYTES, "MariaDB key", "would"
	while len(final) > 1 and _key_bytes(evidence, final) > limit:
		dropped.append((final.pop().split("(", 1)[0], f"the index would pass the {limit}-byte {limit_name} limit"))
	width = _key_bytes(evidence, final)
	if width > limit:
		return [], [], (
			f"An index on {_cols_text(final)} {may} be {width} bytes wide, over the {limit}-byte {limit_name} "
			"limit, so Optimus gives no index code."
		)
	return final, dropped, None


def _no_code(doctype: str, cols, reason: str, *, unknown: bool = False) -> IndexAdvice:
	return IndexAdvice(
		route=ROUTE_NO_CODE, doctype=doctype, table=f"tab{doctype}", columns=tuple(cols), reason=reason,
		unknown=unknown,
	)


def _no_evidence(doctype: str, cols) -> IndexAdvice:
	"""NO_CODE for a table Optimus has no evidence for (E6): not the table of a DocType on
	this site (a core table such as tabSessions, tabSeries or tabSingles, a virtual DocType, a
	removed one) or one whose evidence could not be read. No verdict on the index."""
	return _no_code(doctype, cols, (
		f'Optimus has no information about table "tab{doctype}": it is not the table of a DocType on this '
		"site (a core table such as tabSessions or tabSeries, a virtual DocType or a removed one), or Optimus "
		"could not read it. So it gives no index code. Check the slow queries on it with EXPLAIN."
	), unknown=True)


def _search_index_reason(doctype: str, field: str, evidence: TableEvidence, custom_field: bool) -> str:
	if custom_field:
		return (
			f'The "{field}" field of "{doctype}" is a Custom Field. Open that Custom Field, tick "Search Index" '
			"and save: Frappe adds the index when the Custom Field is saved and keeps it. If your app ships "
			"the Custom Field as a fixture, export the fixture again so it carries search_index 1. If your "
			'app creates it in code, add "search_index": 1 to the field\'s dict in your '
			"create_custom_fields() call."
		)
	if evidence.is_custom_doctype:
		return (
			f'DocType "{doctype}" was created in the UI. Open it, tick "Search Index" on the "{field}" field '
			"and save: Frappe adds the index when the DocType is saved and keeps it."
		)
	return (
		f'Tick "Search Index" on the "{field}" field of DocType "{doctype}" in the DocType editor of your app '
		f'"{evidence.app}" (developer mode), save and commit the DocType JSON. bench migrate then adds the '
		"index on every site and keeps it."
	)


def _why_kept(db: str | None, final: list[str]) -> str:
	"""Why Frappe's schema sync keeps the index, for each database the entry runs on
	(``db`` is the entry's stamp; an entry without one runs on both)."""
	if db == "postgres":
		return (
			"Frappe's schema sync on Postgres drops only indexes named after a bare field name, so this "
			"explicitly named index stays."
		)
	if db is None:
		return (
			"Frappe's schema sync keeps it on both databases: on MariaDB it never drops an index that spans "
			"several columns, and on Postgres it drops only indexes named after a bare field name."
		)
	if len(final) > 1:
		return "Frappe's schema sync never drops an index that spans several columns, so it stays."
	return "Frappe's schema sync never adds or drops an index on a text column, so it stays."


def _caveats(evidence: TableEvidence, dropped: list[tuple[str, str]]) -> tuple[str, ...]:
	out: list[str] = []
	if dropped:
		out.append("Optimus left out " + "; ".join(f"{col} ({why})" for col, why in dropped) + ".")
	if is_write_hot_table(evidence.table):
		out.append(
			f'Note: "{evidence.table}" takes many writes for every submitted document in production, and '
			"each index slows those writes, so add it only if this read is slow in production too."
		)
		if evidence.dialect == "postgres":
			out.append(
				"On Postgres, building an index blocks writes to the table until it finishes, so run "
				"bench migrate in a maintenance window."
			)
	return tuple(out)


def _ensure_caveats(evidence: TableEvidence, base: tuple[str, ...], entry: dict) -> tuple[str, ...]:
	out: list[str] = []
	columns_entry = "columns" in entry
	if columns_entry and any((evidence.fields.get(c) is not None and evidence.fields[c].is_custom_field) for c in base):
		out.append(
			"One of these columns is a Custom Field: a fixture-shipped Custom Field is indexed right after "
			"fixtures sync on install (after_sync)."
		)
	# every entry that can run on Postgres, an unstamped one profiled on MariaDB too (D4)
	if columns_entry and entry.get("db") != "mariadb":
		out.append(
			"On Postgres, when Frappe syncs this DocType it can run DROP INDEX IF EXISTS on an index named "
			"after one of these columns, which removes another table's Search Index of that name until that "
			"table syncs again (a Frappe issue); check pg_indexes after bench migrate."
		)
	return tuple(out)


def _route(doctype: str, final: list[str], dropped, evidence: TableEvidence, tracked_apps: tuple[str, ...]) -> IndexAdvice:
	base = tuple(c.split("(", 1)[0] for c in final)
	lead_field = evidence.fields.get(base[0])
	own = bool(tracked_apps) and evidence.app in tracked_apps
	custom_field = bool(lead_field is not None and lead_field.is_custom_field)
	plain_single = len(final) == 1 and final[0] == base[0]
	mariadb = evidence.dialect != "postgres"
	caveats = _caveats(evidence, dropped)
	if plain_single and mariadb and (own or custom_field or evidence.is_custom_doctype):
		return IndexAdvice(
			route=ROUTE_SEARCH_INDEX, doctype=doctype, table=evidence.table, columns=tuple(final),
			reason=_search_index_reason(doctype, base[0], evidence, custom_field), caveats=caveats,
		)
	if own and _APP_RE.fullmatch(evidence.app or ""):
		app_name = evidence.app
	elif len(tracked_apps) == 1 and _APP_RE.fullmatch(tracked_apps[0]):
		app_name = tracked_apps[0]
	else:
		app_name = UNKNOWN_APP
	if plain_single and mariadb:
		entry = {"doctype": doctype, "search_index_field": base[0], "db": "mariadb"}
		app_label = f'the "{evidence.app}" app' if evidence.app else "another app"
		does = (
			f'Your app\'s ensure_indexes() function creates the index "{base[0]}_index" on "{base[0]}" once, '
			"unless that column already has an index of its own, and then sets Search Index on the field with a "
			"Property Setter, so Frappe's schema sync keeps the index."
		)
		if tracked_apps or evidence.app in FRAMEWORK_APPS:
			reason = f'DocType "{doctype}" belongs to {app_label}, so do not edit it. {does}'
		else:
			# Empty Tracked Apps: no app counts as the developer's own (cycle-1 R-minor), so
			# that a non-framework app is someone else's is a guess, never a fact (E1).
			yours = f'"{evidence.app}" is your app' if evidence.app else "the DocType belongs to your app"
			reason = (
				f'DocType "{doctype}" belongs to {app_label}. {does} If {yours}, tick Search Index on the field '
				"instead; set Tracked Apps in Optimus Settings so Optimus can tell."
			)
	else:
		entry = {"doctype": doctype, "columns": list(final), "index_name": optimus_index_name(doctype, base)}
		if any("(" in c for c in final):
			entry["db"] = "mariadb"  # a text prefix: Postgres would index the whole value
		elif not mariadb and (len(final) == 1 or any(c.lower() in MARIADB_RESERVED_WORDS for c in base)):
			# MariaDB schema sync drops an undeclared single-column index, and MariaDB add_index
			# writes column names unquoted, so a reserved word fails to build there (E3)
			entry["db"] = "postgres"
		elif mariadb and _key_bytes(evidence, final) > POSTGRES_MAX_INDEX_ROW_BYTES:
			entry["db"] = "mariadb"  # wider than a Postgres index row can hold
		reason = (
			f'Your app\'s ensure_indexes() function creates the index "{entry["index_name"]}" on '
			f"{_cols_text(base)} once. It skips the index when it already exists or when the table or a column "
			"is missing, and it writes an Error Log entry instead of stopping bench migrate when the index "
			f"cannot be built. {_why_kept(entry.get('db'), final)}"
		)
	return IndexAdvice(
		route=ROUTE_ENSURE_INDEXES, doctype=doctype, table=evidence.table, columns=tuple(final), reason=reason,
		caveats=caveats + _ensure_caveats(evidence, base, entry), entry=entry,
		app_name=app_name,
	)


def advise(
	table: str,
	columns,
	*,
	evidence: TableEvidence | None,
	tracked_apps: tuple[str, ...] = (),
	explain_row=None,
	query: str = "",
	unusable: Mapping[str, set[str]] | None = None,
	comparisons: Mapping[str, str] | None = None,
	serves: str = "",
) -> IndexAdvice | None:
	"""The advice for indexing ``columns`` of ``table``, or None when there is nothing
	to advise (not a DocType table, no usable column). ``unusable`` names the columns
	the query's predicate shape keeps an index from using (``_scan_where``): they are
	left out and named. ``comparisons`` (``{column: "eq" | "in" | "range" | "sort"}``)
	puts the columns in index order (``_index_order``; ``serves`` names the sort or the
	temporary table a Filesort or Temporary Table index removes). When that sort-first
	recipe gives no code or loses its plain sort column (a prefix, the column cap), the
	recipe without ``serves`` is given instead. A recipe made only of Check fields, or
	with nothing left, is NO_CODE. The existing-index checks run on the final recipe, after
	columns were left out (``_existing_index_problem``)."""
	kwargs = {
		"evidence": evidence, "tracked_apps": tracked_apps, "explain_row": explain_row, "query": query,
		"unusable": unusable, "comparisons": comparisons,
	}
	advice = _advise(table, columns, serves=serves, **kwargs)
	if serves:
		sorts = [col for col, kind in (comparisons or {}).items() if kind == "sort"]
		if advice is None or advice.route == ROUTE_NO_CODE or not any(col in advice.columns for col in sorts):
			advice = _advise(table, columns, serves="", **kwargs)
	return advice


def _advise(
	table: str,
	columns,
	*,
	evidence: TableEvidence | None,
	tracked_apps: tuple[str, ...],
	explain_row,
	query: str,
	unusable: Mapping[str, set[str]] | None,
	comparisons: Mapping[str, str] | None,
	serves: str,
) -> IndexAdvice | None:
	doctype = doctype_of(table)
	if doctype is None:
		return None
	shapes = {
		col: set(kinds) for col, kinds in (unusable or {}).items()
		if isinstance(col, str) and _IDENT_RE.fullmatch(col) and col.lower() not in FRAPPE_METADATA_COLUMNS
	}
	cols = _clean_columns([c for c in columns or [] if c not in (unusable or {})], cap=False)
	order_dropped: list[tuple[str, str]] = []
	if comparisons:
		cols, order_dropped = _index_order(cols, comparisons, serves=serves)
		cols = apply_metadata_rule(cols)  # creation / modified never lead after reordering either
	cols = cols[:MAX_INDEX_COLUMNS]
	if not cols and not shapes:
		return None
	if evidence is None:
		return _no_evidence(doctype, cols or list(shapes))
	# a name the table does not have (a fragment of a query cut short) is never shown
	shapes = {col: kinds for col, kinds in shapes.items() if col in evidence.fields}
	if not cols or all(_is_check_field(evidence, col) for col in cols):
		reason, unknown = _shape_no_code_reason(shapes, cols)
		return _no_code(doctype, cols + list(shapes), reason, unknown=unknown)
	problem = _column_problem(evidence, cols)
	if problem:
		return _no_code(doctype, cols, problem)
	final, dropped, problem = _index_columns(evidence, cols)
	if problem:
		return _no_code(doctype, cols, problem)
	# the existing-index checks look at the FINAL recipe, after columns were left out (C1)
	problem = _existing_index_problem(evidence, [c.split("(", 1)[0] for c in final], explain_row, query, shapes)
	if problem:
		return _no_code(doctype, cols, problem)
	shape_dropped = [(col, _shape_why(kinds)) for col, kinds in shapes.items()]
	return _route(doctype, final, shape_dropped + order_dropped + dropped, evidence, tuple(tracked_apps or ()))


def _is_check_field(evidence: TableEvidence, col: str) -> bool:
	field = evidence.fields.get(col)
	return field is not None and field.fieldtype == "Check"


def _sort_cause(ftype: str, problem: str) -> str:
	"""The sentence naming why the sort (Filesort) or the temporary table stays (C7)."""
	filesort = ftype == "Filesort"
	does, stays = ("sorts", "the sort stays") if filesort else ("groups", "the temporary table stays")
	if problem == "aggregate":
		return f"The query sorts {'' if filesort else 'its groups '}by an aggregate, which no index can return in order, so {stays}."
	if problem == "differs":
		return (
			"The query groups by other columns than it sorts by" if filesort
			else "The query sorts by other columns than it groups by"
		) + f", so {stays}."
	if problem == "distinct":
		return "The temporary table comes from the query's DISTINCT, which this index does not cover, so it stays."
	return (
		f"The query {does} by an expression, a select alias, a text column or in mixed directions, which no "
		f"index can return in order, so {stays}."
	)


def _lead_for(
	ftype: str,
	labelled: list[tuple[str, str]],
	advice: IndexAdvice,
	*,
	sort_problem: str = "",
	ranged: str | None = None,
	multi: tuple[str, ...] = (),
) -> str:
	"""The finding type's opening sentence. For Filesort / Temporary Table it never claims
	the sort or the temporary table goes when the index cannot remove it: the sort is not
	on bare columns (``sort_problem``, see ``_sort_problem``, which also names the cause),
	the sort column follows a range condition (``ranged``), or a kept filter matches several
	values (``multi``)."""
	if advice.route == ROUTE_NO_CODE:
		return ""
	kept = {c.split("(", 1)[0] for c in advice.columns}
	labels = {label for label, col in labelled if col in kept}
	sort_label = {"Filesort": "ORDER BY", "Temporary Table": "GROUP BY"}.get(ftype)
	if sort_label:
		stays = "the sort stays" if ftype == "Filesort" else "the temporary table stays"
		expression = _FILTER_LEAD + " " + _sort_cause(ftype, sort_problem)
		if sort_label in labels:
			kept_multi = [col for col in multi if col in kept]
			if sort_problem:
				return expression
			if kept_multi:
				column, still = (
					("sort", "sorts the rows") if ftype == "Filesort" else ("GROUP BY", "groups them in a temporary table")
				)
				return (
					f"Index the filter columns followed by the {column} column so fewer rows are read. The filter "
					f"on {kept_multi[0]} matches more than one value, so the database still {still}."
				)
		else:
			sort_cols = [col for label, col in labelled if label == sort_label]
			# a sort column the parser could name that is no Frappe metadata column
			if any(col.lower() not in FRAPPE_METADATA_COLUMNS for col in sort_cols):
				if sort_problem:
					return expression
				if ranged:
					column = "sort" if ftype == "Filesort" else "grouping"
					return (
						_FILTER_LEAD + f" The {column} column comes after the range condition on {ranged}, so this "
						f"index cannot return the rows in order and {stays}."
					)
		if sort_label not in labels:
			# the sort or group column is not in the index: name why (C7)
			column = "sort" if ftype == "Filesort" else "grouping"
			if sort_problem in ("aggregate", "distinct"):
				return expression
			if any(label == sort_label and col.lower() in FRAPPE_METADATA_COLUMNS for label, col in labelled):
				return (
					_FILTER_LEAD + f" The {column} column is a Frappe metadata column, which Optimus never indexes, "
					f"so {stays}."
				)
			if sort_problem in ("expression", "differs"):
				return expression
			return _FILTER_LEAD + f" This index does not cover the {column}, so {stays}."
	return _TYPE_LEADS.get(ftype, "")


def advise_finding(
	finding: dict,
	*,
	evidence_lookup: Callable[[str], TableEvidence | None],
	tracked_apps: tuple[str, ...] = (),
	parser: Callable[[str], dict] | None = None,
) -> IndexAdvice | None:
	"""The advice for an index-family or Slow Query finding (render dict or row-shaped
	dict), or None when there is nothing to advise."""
	ftype = finding.get("finding_type") or ""
	if ftype not in ADVISED_FINDING_TYPES:
		return None
	detail = _detail(finding)
	query = str(detail.get("normalized_query") or "")
	if ftype != "Missing Index" and len(query) > MAX_QUERY_CHARS:
		# The parser is superlinear (cycle 1: 216 ms at 19 KB); fail closed to an explanation.
		doctype = doctype_of(str(detail.get("table") or ""))
		if doctype is None:
			return None
		return _no_code(doctype, (), (
			f"This query is longer than {MAX_QUERY_CHARS} characters, so Optimus does not parse it for "
			"index advice. Check it with EXPLAIN."
		), unknown=True)
	table, labelled = _index_target(ftype, detail, parser or parse_query)
	doctype = doctype_of(table)
	if doctype is None:
		return None
	evidence = evidence_lookup(f"tab{doctype}")
	unusable = comparisons = None
	serves = ""
	sort_problem = ""
	multi: tuple[str, ...] = ()
	if query:
		# top_queries keeps QUERY_TEXT_LIMIT characters, so a Slow Query that long may be cut
		truncated = ftype == "Slow Query" and len(query) >= QUERY_TEXT_LIMIT
		qualifiers = _target_qualifiers(query, table, getattr(parser, "aliases", None))
		if evidence is not None and labelled and _union_branches(query, qualifiers) > 1:
			# one branch's filter is not the query's: never advise from one branch (E7)
			return _no_code(doctype, _clean_columns([col for _label, col in labelled]), (
				f'Optimus could not read how this query filters: it is a UNION that filters "tab{doctype}" in '
				"more than one branch, and one index may not serve them all. So it gives no index code. Check "
				"each branch with EXPLAIN."
			), unknown=True)
		unusable, usable = _scan_where(query, labelled, qualifiers, truncated=truncated)
		comparisons = {}
		for label, col in labelled:  # filters come first, then the sort or group columns
			if label in ("ORDER BY", "GROUP BY"):
				if comparisons.get(col) != "eq":  # an equality-filtered sort column stays an equality column
					comparisons[col] = "sort"
			else:
				comparisons.setdefault(col, "eq" if label == "JOIN" else usable.get(col, "range"))
		multi = tuple(col for col, kind in comparisons.items() if kind == "in")
		if ftype in _SERVES:
			sort_problem = _sort_problem(query, ftype, qualifiers, evidence)
			# rows matching several values (IN, IS NULL OR =) never come back in one sorted run
			serves = _SERVES[ftype] if not sort_problem and not multi else ""
	advice = advise(
		table, [col for _label, col in labelled], evidence=evidence,
		tracked_apps=tracked_apps, explain_row=detail.get("explain_row"), query=query, unusable=unusable,
		comparisons=comparisons, serves=serves,
	)
	if advice is None:
		return None
	ranged = next((col for col, kind in (comparisons or {}).items() if kind == "range"), None)
	return replace(advice, lead=_lead_for(ftype, labelled, advice, sort_problem=sort_problem, ranged=ranged, multi=multi))


def advise_table(
	table: str, columns, *, evidence_lookup: Callable[[str], TableEvidence | None], tracked_apps: tuple[str, ...] = (),
) -> IndexAdvice | None:
	"""The advice for a table card's recommended columns."""
	doctype = doctype_of(table)
	if doctype is None:
		return None
	return advise(table, list(columns or []), evidence=evidence_lookup(f"tab{doctype}"), tracked_apps=tracked_apps)


def _string_hook_pair(hook: str) -> str:
	"""A hooks.py value set as a string, turned into a list with ensure_indexes last (D2):
	a list pasted under the string would replace it, or be replaced by it."""
	return f'["<the string already there>", "{hook}"]'


def _install_text(advice: IndexAdvice) -> str:
	"""How to install the code, which the report shows above the prose (finding and card)."""
	app_name = advice.app_name
	hook = f"{app_name}.{HOOK_MODULE}.ensure_indexes"
	text = (
		f"Save the code above as {app_name}/{app_name}/{HOOK_MODULE}.py. If that file already exists, add only "
		f"this entry to its INDEXES list: {json.dumps(advice.entry)}. In hooks.py add \"{hook}\" as the last "
		f"item of the {_HOOK_EVENTS_TEXT} lists. When hooks.py sets one of them as a string, make it a list "
		f"that keeps that string first, for example after_migrate = {_string_hook_pair(hook)}."
	)
	if app_name == UNKNOWN_APP:
		text += " Replace your_app with the name of your app in the file path and in hooks.py."
	return text


def finding_text(advice: IndexAdvice, *, install: bool = True) -> str:
	"""Prose for the finding's fix-hint slot. ``install=False`` leaves out how to save
	the code, for the Slow Query AI prompt, which carries no code."""
	parts = [advice.lead, advice.reason]
	if install and advice.route == ROUTE_ENSURE_INDEXES:
		parts.append(_install_text(advice))
	parts += list(advice.caveats)
	return " ".join(p for p in parts if p)


def card_note(advice: IndexAdvice) -> str:
	"""Prose for the table card's note, which sits under the card's code block (the
	advice's own ``code``, shown only when there is code; owner decision D3). A no-code
	note opens with "Do not add this index." only when that is a verdict; when Optimus
	could not tell (``advice.unknown``) it opens with the neutral ``NO_VERDICT`` (U2, E6)."""
	entry = advice.entry or {}
	if advice.route == ROUTE_NO_CODE:
		parts = [NO_VERDICT if advice.unknown else "Do not add this index.", advice.reason]
	elif advice.route == ROUTE_SEARCH_INDEX or entry.get("search_index_field"):
		parts = [
			"One column: bench migrate drops a single-column index that its field does not declare, so "
			"this index belongs on the field.",
			advice.reason,
		]
	else:
		parts = [advice.reason]
	if advice.route == ROUTE_ENSURE_INDEXES:
		parts.append(_install_text(advice))
	parts += list(advice.caveats)
	return " ".join(p for p in parts if p)


def ensure_indexes_code(entries: list[dict], *, app_name: str = UNKNOWN_APP) -> str:
	"""The developer's ``<app>/<app>/optimus_indexes.py``: the hooks.py lines as comments,
	then ``INDEXES`` (one JSON literal per entry) and ``ensure_indexes()``."""
	app = app_name if _APP_RE.fullmatch(app_name or "") else UNKNOWN_APP
	hook = f"{app}.{HOOK_MODULE}.ensure_indexes"
	body = "".join(f"\t{json.dumps(entry)},\n" for entry in entries)
	hook_lines = "".join(f'#   {event} = ["{hook}"]\n' for event in HOOK_EVENTS)
	return (
		f"# {app}/{app}/{HOOK_MODULE}.py\n"
		f"# In {app}/hooks.py run it after install, right after the install's fixture sync and after\n"
		"# every migrate. Add it as the last item of each list:\n"
		+ hook_lines
		+ "# When hooks.py sets one of them as a string, make it a list that keeps that string first:\n"
		f"#   after_migrate = {_string_hook_pair(hook)}\n"
		+ "import contextlib\n\nimport frappe\n\n"
		'# An entry with "db" runs only on that database (frappe.db.db_type).\n'
		"INDEXES = [\n" + body + "]\n\n\n" + _ENSURE_FUNCTION
	)
