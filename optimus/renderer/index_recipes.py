# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Deterministic index advice for index-family findings and table cards (no site access, no I/O).

``advise`` is the one advisor for both surfaces, so a finding and its table's card never
disagree. It reads only the evidence ``recipe_enrichment`` collects for the table
(DocField flags, real column types, existing indexes) and picks one route:

- ``search_index``: one non-text column of a field the developer controls (an app in
  Tracked Apps, a Custom Field, or a DocType created in the UI) on MariaDB. The advice
  is text: tick Search Index.
- ``ensure_indexes``: any other index that can be built. The code is one idempotent
  ``ensure_indexes()`` for the developer's app, run from hooks.py ``after_install``,
  ``after_sync`` and ``after_migrate``. Each entry checks its database (an entry that is
  only right on MariaDB or Postgres says so in ``"db"``), its table, its columns and its
  index first; a failing entry rolls back only its own writes and writes an Error Log row
  instead of stopping bench migrate.
- ``no_code``: an explanation and no code.

Frappe v16.18 facts relied on: MariaDB schema sync drops a single-column index its
DocField does not declare but never one that spans several columns or covers a text
column (frappe/database/schema.py:310-315, mariadb/schema.py:142-145,
mariadb/database.py:383-411); Postgres schema sync names a Search Index after the bare
field name and drops by that name (postgres/schema.py:124, :183); frappe.db.add_index
writes column names unquoted on MariaDB (mariadb/database.py:420-425) and strips a
prefix on Postgres (postgres/database.py:400); on install, after_install runs before
fixtures are synced and after_sync right after them (installer.py:332-345), and on
migrate after_migrate runs after them (migrate.py:165-197).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from optimus.analyzers.base import FRAPPE_METADATA_COLUMNS, INDEX_FINDING_TYPES, is_write_hot_table
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
HOOK_MODULE = "optimus_indexes"
UNKNOWN_APP = "your_app"
# The hooks.py lists ensure_indexes() is registered in (owner decision D4): after_sync
# runs right after the install's fixture sync, so a fixture-shipped Custom Field is
# indexed on a fresh install too.
HOOK_EVENTS: tuple[str, ...] = ("after_install", "after_sync", "after_migrate")
_HOOK_EVENTS_TEXT = "after_install, after_sync and after_migrate"

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
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
_SHAPE_GENERIC = (
	"a LIKE pattern that starts with a wildcard, a function or IFNULL wrapped around the column, "
	"an OR between conditions, or a filter that matches most of the table's rows"
)

_ENSURE_FUNCTION = '''def ensure_indexes():
	"""Create each index in INDEXES once. One failed entry never stops the others."""
	for entry in INDEXES:
		try:
			# commit what ran before this entry, so the rollback below undoes only this entry
			frappe.db.commit()
			_ensure_index(entry)
			frappe.db.commit()
		except Exception:
			with contextlib.suppress(Exception):
				frappe.db.rollback()
				frappe.log_error(title=_title(entry, "was not created"))


def _title(entry, what):
	key = entry.get("index_name") or entry.get("search_index_field")
	return f"Index for {entry['doctype']} {what}: {key}"[:140]


def _ensure_index(entry):
	doctype = entry["doctype"]
	if entry.get("db", frappe.db.db_type) != frappe.db.db_type:
		title = _title(entry, f"skipped on {frappe.db.db_type}, the entry is for {entry['db']}")
		if not frappe.db.exists("Error Log", {"method": title}):
			frappe.log_error(title=title)
		return
	if not frappe.db.table_exists(doctype, cached=False):
		return
	field = entry.get("search_index_field")
	if field:
		if not frappe.db.has_column(doctype, field):
			return
		if not frappe.db.exists(
			"Property Setter",
			{"doc_type": doctype, "field_name": field, "property": "search_index", "value": "1"},
		):
			from frappe.custom.doctype.property_setter.property_setter import make_property_setter

			make_property_setter(doctype, field, "search_index", 1, "Check", validate_fields_for_doctype=False)
		if not frappe.db.has_index(f"tab{doctype}", f"{field}_index"):
			frappe.db.updatedb(doctype)
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
	``ensure_indexes()`` entry for ``ROUTE_ENSURE_INDEXES``."""

	route: str
	doctype: str
	table: str
	columns: tuple[str, ...]
	reason: str
	caveats: tuple[str, ...] = ()
	entry: dict | None = None
	app_name: str = UNKNOWN_APP
	lead: str = ""

	@property
	def code(self) -> str | None:
		if self.route != ROUTE_ENSURE_INDEXES or not self.entry:
			return None
		return ensure_indexes_code([self.entry], app_name=self.app_name)


def doctype_of(table: str) -> str | None:
	"""``"Sales Invoice"`` for ``tabSales Invoice`` (backticks allowed), else None."""
	name = str(table or "").strip().strip("`")
	if not _TAB_TABLE_RE.match(name):
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
	seen: set[str] = set()
	for labels in _CLAUSES_BY_TYPE.get(ftype, (("WHERE", "JOIN"),)):
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


def _clean_columns(columns) -> list[str]:
	out: list[str] = []
	seen: set[str] = set()
	for col in apply_metadata_rule([c for c in columns or [] if isinstance(c, str) and _IDENT_RE.fullmatch(c)]):
		if col.lower() in seen:
			continue
		seen.add(col.lower())
		out.append(col)
	return out[:MAX_INDEX_COLUMNS]


# --- predicate shape -------------------------------------------------------------
# sql_metadata (the table-breakdown parser) lists a query's WHERE columns but not how
# they combine, and Frappe's normalize_query turns every literal into ?, so a LIKE
# pattern is never visible (frappe/recorder.py:154-179). A conservative token scan of the
# main query's WHERE clause decides which WHERE columns a composite index can use: a
# column compared only inside an OR group, or only by a LIKE whose pattern is a parameter
# or starts with a wildcard, cannot be used. Fail closed: a WHERE column the scan cannot
# place (an unbalanced bracket or quote, no top-level WHERE, a column it cannot find) is
# treated as unusable.

_SQL_TOKEN_RE = re.compile(
	r"`[^`]*`|'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.)*\"|%\([A-Za-z_]\w*\)s|%s"
	r"|[A-Za-z_][A-Za-z0-9_$]*|\|\||&&|\S"
)
_NAME_RE = re.compile(r"^(?:`[^`]*`|[A-Za-z_][A-Za-z0-9_$]*)$")
_WHERE_ENDS: frozenset[str] = frozenset({
	"group", "order", "limit", "having", "union", "for", "lock", "window", "into", "procedure",
})
_OR_WORDS: frozenset[str] = frozenset({"or", "xor", "||"})
_AND_WORDS: frozenset[str] = frozenset({"and", "&&"})
_SHAPE_WHY: dict[str, str] = {
	"or": "compared only inside an OR between conditions",
	"like": "compared by a LIKE whose pattern can start with a wildcard",
	"unsure": "a filter Optimus could not place with certainty",
}


def _where_tokens(query: str) -> list[str] | None:
	"""The tokens of the main query's WHERE clause ([] when it has none), or None when the
	query cannot be scanned with certainty (an unbalanced bracket or quote)."""
	tokens = _SQL_TOKEN_RE.findall(query or "")
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
	for i, tok in enumerate(tokens):
		depth += {"(": 1, ")": -1}.get(tok, 0)
		if depth or tok in ("(", ")"):
			continue
		low = tok.lower()
		if start is None and low == "where":
			start = i + 1
		elif start is not None and low in _WHERE_ENDS:
			return tokens[start:i]
	return tokens[start:] if start is not None else []


def _conjuncts(tokens: list[str]) -> list[list[str]] | None:
	"""The pieces of a WHERE clause between its top-level ANDs, or None when an OR sits at
	its top level (AND binds tighter, so no piece is then sure to apply). The AND of a
	BETWEEN also splits: harmless, because a nested OR stays inside its brackets, so a
	finer split never moves a column out of an OR group."""
	parts: list[list[str]] = [[]]
	depth = 0
	for tok in tokens:
		low = tok.lower()
		depth += {"(": 1, ")": -1}.get(tok, 0)
		if depth == 0 and tok not in ("(", ")"):
			if low in _OR_WORDS:
				return None
			if low in _AND_WORDS:
				parts.append([])
				continue
		parts[-1].append(tok)
	return [part for part in parts if part]


def _column_refs(tokens: list[str]) -> list[tuple[str, int]]:
	"""``(lowercase column, index of the token after it)`` for each name that is not a
	function call; a dotted reference (``tabX.col``, ``alias.col``) counts by its last part."""
	out: list[tuple[str, int]] = []
	i = 0
	while i < len(tokens):
		if not _NAME_RE.match(tokens[i]):
			i += 1
			continue
		j = i
		while j + 2 < len(tokens) and tokens[j + 1] == "." and _NAME_RE.match(tokens[j + 2]):
			j += 2
		if not (j + 1 < len(tokens) and tokens[j + 1] == "("):
			out.append((tokens[j].strip("`").lower(), j + 1))
		i = j + 1
	return out


def _wildcard_like(tokens: list[str], at: int) -> bool:
	"""True when ``tokens[at:]`` starts with ``[NOT] LIKE <pattern>`` and the pattern may
	start with a wildcard: a parameter (normalized queries hide every literal), anything
	but a string literal, or a literal that starts with % or _."""
	if at < len(tokens) and tokens[at].lower() == "not":
		at += 1
	if at >= len(tokens) or tokens[at].lower() != "like":
		return False
	pattern = tokens[at + 1] if at + 1 < len(tokens) else ""
	if pattern[:1] in ("'", '"') and len(pattern) > 1:
		return pattern[1:2] in ("%", "_")
	return True


def _unusable_where_columns(query: str, labelled) -> dict[str, set[str]]:
	"""``{column: kinds}`` for each WHERE column of ``labelled`` that a composite index
	cannot use; a kind is ``"or"``, ``"like"`` or ``"unsure"`` (see the section comment)."""
	where_cols: list[str] = []
	for label, col in labelled or []:
		if label == "WHERE" and col not in where_cols:
			where_cols.append(col)
	if not where_cols:
		return {}
	tokens = _where_tokens(query)
	if tokens is None:
		return {col: {"unsure"} for col in where_cols}
	conjuncts = _conjuncts(tokens)
	if conjuncts is None:
		return {col: {"or"} for col in where_cols}
	usable: set[str] = set()
	kinds: dict[str, set[str]] = defaultdict(set)
	for part in conjuncts:
		has_or = any(tok.lower() in _OR_WORDS for tok in part)
		for name, after in _column_refs(part):
			like = _wildcard_like(part, after)
			if has_or:
				kinds[name].add("or")
			if like:
				kinds[name].add("like")
			if not has_or and not like:
				usable.add(name)
	return {col: set(kinds.get(col.lower()) or {"unsure"}) for col in where_cols if col.lower() not in usable}


def _shape_phrases(shapes: Mapping[str, set[str]]) -> list[str]:
	like = [col for col, kinds in shapes.items() if "like" in kinds]
	ored = [col for col, kinds in shapes.items() if "or" in kinds]
	unsure = [col for col, kinds in shapes.items() if kinds == {"unsure"}]
	out: list[str] = []
	if like:
		out.append(f"a LIKE on {', '.join(like)}, which cannot use an index when its pattern starts with a wildcard")
	if ored:
		out.append(f"an OR between conditions on {', '.join(ored)}")
	if unsure:
		out.append(f"a filter on {', '.join(unsure)} that Optimus could not place with certainty")
	return out


def _shape_why(kinds: set[str]) -> str:
	return _SHAPE_WHY["or" if "or" in kinds else "like" if "like" in kinds else "unsure"]


def _shape_no_code_reason(shapes: Mapping[str, set[str]], checks: list[str]) -> str:
	"""NO_CODE when the predicate shape leaves no column an index could narrow on."""
	rest = ""
	if len(checks) == 1:
		rest = f" {checks[0]} is a Check field, which matches too many rows for an index to narrow."
	elif checks:
		rest = f" {', '.join(checks)} are Check fields, which match too many rows for an index to narrow."
	return (
		f"The cost comes from the shape of the filter: {'; '.join(_shape_phrases(shapes))}. A composite "
		f"index cannot use those columns.{rest} So an index would not help, and Optimus gives no index "
		"code. Rewrite the filter (an exact match instead of a LIKE, or one query per OR branch) and "
		"check the result with EXPLAIN."
	)


def _shape_text(query: str, col: str) -> str:
	"""Why an index that already exists does not help, from the query's shape."""
	ref = r"(?:`[^`]+`\.|\w+\.)?`?" + re.escape(col) + r"`?"
	shapes: list[str] = []
	if re.search(ref + r"\s+(?:not\s+)?like\b", query or "", re.IGNORECASE):
		shapes.append(f"a LIKE on {col}, which cannot use an index when its pattern starts with a wildcard")
	function = re.search(r"\b([a-z_]+)\s*\(\s*" + ref + r"\s*[,)]", query or "", re.IGNORECASE)
	if function and function.group(1).lower() not in _AGGREGATES:
		shapes.append(f"the function {function.group(1).upper()}() wrapped around {col}")
	if re.search(r"\bwhere\b.*\bor\b", query or "", re.IGNORECASE | re.DOTALL):
		shapes.append("an OR between conditions")
	what = "; ".join(shapes) if shapes else _SHAPE_GENERIC
	return (
		f"The cost comes from how the query filters: {what}. Rewrite the filter so the existing "
		"index can be used, and check the result with EXPLAIN."
	)


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
	reserved = [c for c in cols if c.lower() in MARIADB_RESERVED_WORDS]
	if reserved:
		return (
			f'Column "{reserved[0]}" is a reserved word in MariaDB, and frappe.db.add_index writes column '
			"names without quotes, so building the index would fail. Optimus gives no index code for it."
		)
	return None


def _existing_index_problem(evidence: TableEvidence, cols: list[str], explain_row, query: str) -> str | None:
	"""NO_CODE when a recipe column is unique, when an existing index already starts with
	the whole recipe column list, or when the LEADING column already leads an index, has
	Search Index ticked, or leads an index EXPLAIN names. A later column with an index of
	its own does not make the composite useless (fix round 1, I3). A trailing creation /
	modified is not checked: Frappe indexes creation on every non-child table
	(mariadb/schema.py:36-39; owner decision D5)."""
	checked = [c for c in cols if c.lower() not in TRAILING_METADATA_OK]
	for col in checked:
		field = evidence.fields.get(col)
		if (field is not None and field.unique) or any(ix.unique and ix.columns[:1] == (col,) for ix in evidence.indexes):
			return f'Column "{col}" is already unique, so the database already has an index on it. {_shape_text(query, col)}'
	lead = cols[0]
	whole = tuple(cols)
	covering = next((ix for ix in evidence.indexes if ix.columns[: len(whole)] == whole), None)
	if covering is not None:
		return (
			f'The index "{covering.name}" on table "{evidence.table}" already starts with {_cols_text(whole)}, '
			f"so this index exists already and a new one would not help. {_shape_text(query, lead)}"
		)
	led = next((ix for ix in evidence.indexes if ix.columns[:1] == (lead,)), None)
	if led is not None:
		return (
			f'Column "{lead}" already leads the index "{led.name}" on table "{evidence.table}", so a new '
			f"index would not help. {_shape_text(query, lead)}"
		)
	field = evidence.fields.get(lead)
	if field is not None and field.search_index:
		return (
			f'Column "{lead}" already has Search Index ticked, so Frappe keeps an index on it and a new '
			f"one would not help. {_shape_text(query, lead)}"
		)
	for name, used in _explain_index_names(explain_row):
		named = next((ix for ix in evidence.indexes if ix.name == name), None)
		if (named is not None and named.columns[:1] == (lead,)) or name in (lead, f"{lead}_index"):
			how = "uses" if used else "can use"
			return (
				f'EXPLAIN shows the database {how} the index "{name}" on column "{lead}", so a new index '
				f"would not help. {_shape_text(query, lead)}"
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
		if col in evidence.unindexable_columns:
			if i == 0:
				dtype = evidence.column_types.get(col, "")
				return [], [], (
					f'Column "{col}" has the type {dtype}, which a plain index cannot cover, '
					"so Optimus gives no index code."
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


def _no_code(doctype: str, cols, reason: str) -> IndexAdvice:
	return IndexAdvice(route=ROUTE_NO_CODE, doctype=doctype, table=f"tab{doctype}", columns=tuple(cols), reason=reason)


def _search_index_reason(doctype: str, field: str, evidence: TableEvidence, custom_field: bool) -> str:
	if custom_field:
		return (
			f'The "{field}" field of "{doctype}" is a Custom Field. Open that Custom Field, tick "Search Index" '
			"and save: Frappe adds the index when the Custom Field is saved and keeps it. If your app ships "
			"the Custom Field as a fixture, export the fixture again so it carries search_index 1."
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


def _why_kept(evidence: TableEvidence, final: list[str]) -> str:
	if evidence.dialect == "postgres":
		return (
			"Frappe's schema sync on Postgres drops only indexes named after a bare field name, so this "
			"explicitly named index stays."
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


def _ensure_caveats(evidence: TableEvidence, base: tuple[str, ...], app_name: str, columns_entry: bool) -> tuple[str, ...]:
	out: list[str] = []
	if columns_entry and any((evidence.fields.get(c) is not None and evidence.fields[c].is_custom_field) for c in base):
		out.append(
			"One of these columns is a Custom Field: a fixture-shipped Custom Field is indexed right after "
			"fixtures sync on install (after_sync)."
		)
	if evidence.dialect == "postgres":
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
	if own and _APP_RE.match(evidence.app or ""):
		app_name = evidence.app
	elif len(tracked_apps) == 1 and _APP_RE.match(tracked_apps[0]):
		app_name = tracked_apps[0]
	else:
		app_name = UNKNOWN_APP
	if plain_single and mariadb:
		entry = {"doctype": doctype, "search_index_field": base[0], "db": "mariadb"}
		app_label = f'the "{evidence.app}" app' if evidence.app else "another app"
		reason = (
			f'DocType "{doctype}" belongs to {app_label}, so do not edit it. Your app\'s ensure_indexes() '
			f'function sets Search Index on its "{base[0]}" field with a Property Setter when none exists and '
			"syncs the table, so Frappe's schema sync then creates the index and keeps it."
		)
	else:
		entry = {"doctype": doctype, "columns": list(final), "index_name": optimus_index_name(doctype, base)}
		if any("(" in c for c in final):
			entry["db"] = "mariadb"  # a text prefix: Postgres would index the whole value
		elif not mariadb and len(final) == 1:
			entry["db"] = "postgres"  # MariaDB schema sync drops an undeclared single-column index
		elif mariadb and _key_bytes(evidence, final) > POSTGRES_MAX_INDEX_ROW_BYTES:
			entry["db"] = "mariadb"  # wider than a Postgres index row can hold
		reason = (
			f'Your app\'s ensure_indexes() function creates the index "{entry["index_name"]}" on '
			f"{_cols_text(base)} once. It skips the index when it already exists or when the table or a column "
			"is missing, and it writes an Error Log entry instead of stopping bench migrate when the index "
			f"cannot be built. {_why_kept(evidence, final)}"
		)
	return IndexAdvice(
		route=ROUTE_ENSURE_INDEXES, doctype=doctype, table=evidence.table, columns=tuple(final), reason=reason,
		caveats=caveats + _ensure_caveats(evidence, base, app_name, "columns" in entry), entry=entry,
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
) -> IndexAdvice | None:
	"""The advice for indexing ``columns`` of ``table``, or None when there is nothing
	to advise (not a DocType table, no usable column). ``unusable`` names the columns
	the query's predicate shape keeps an index from using (``_unusable_where_columns``):
	they are left out and named, and when nothing but Check fields is left the advice is
	NO_CODE."""
	doctype = doctype_of(table)
	if doctype is None:
		return None
	shapes = {
		col: set(kinds) for col, kinds in (unusable or {}).items()
		if isinstance(col, str) and _IDENT_RE.fullmatch(col) and col.lower() not in FRAPPE_METADATA_COLUMNS
	}
	cols = _clean_columns([c for c in columns or [] if c not in (unusable or {})])
	if not cols and not shapes:
		return None
	if not cols:
		return _no_code(doctype, list(shapes), _shape_no_code_reason(shapes, []))
	if evidence is None:
		return _no_code(doctype, cols, (
			f'Optimus could not read DocType "{doctype}" or the columns of its table while building this '
			"report (the DocType may not exist on this site), so it gives no index code. Check the query "
			"with EXPLAIN on a site where the DocType exists."
		))
	if shapes and all(_is_check_field(evidence, col) for col in cols):
		return _no_code(doctype, cols + list(shapes), _shape_no_code_reason(shapes, cols))
	problem = _column_problem(evidence, cols) or _existing_index_problem(evidence, cols, explain_row, query)
	if problem:
		return _no_code(doctype, cols, problem)
	final, dropped, problem = _index_columns(evidence, cols)
	if problem:
		return _no_code(doctype, cols, problem)
	shape_dropped = [(col, _shape_why(kinds)) for col, kinds in shapes.items()]
	return _route(doctype, final, shape_dropped + dropped, evidence, tuple(tracked_apps or ()))


def _is_check_field(evidence: TableEvidence, col: str) -> bool:
	field = evidence.fields.get(col)
	return field is not None and field.fieldtype == "Check"


def _lead_for(ftype: str, labelled: list[tuple[str, str]], advice: IndexAdvice) -> str:
	if advice.route == ROUTE_NO_CODE:
		return ""
	kept = {c.split("(", 1)[0] for c in advice.columns}
	labels = {label for label, col in labelled if col in kept}
	if ftype == "Filesort" and "ORDER BY" not in labels:
		return (
			_FILTER_LEAD + " The sort column is a Frappe metadata column or an aggregate, which this index "
			"cannot cover, so the sort stays."
		)
	if ftype == "Temporary Table" and "GROUP BY" not in labels:
		return (
			_FILTER_LEAD + " The grouping column is a Frappe metadata column, which Optimus never indexes, "
			"so the temporary table stays."
		)
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
	table, labelled = _index_target(ftype, detail, parser or parse_query)
	doctype = doctype_of(table)
	if doctype is None:
		return None
	query = str(detail.get("normalized_query") or "")
	advice = advise(
		table, [col for _label, col in labelled], evidence=evidence_lookup(f"tab{doctype}"),
		tracked_apps=tracked_apps, explain_row=detail.get("explain_row"), query=query,
		unusable=_unusable_where_columns(query, labelled) if query else None,
	)
	if advice is None:
		return None
	return replace(advice, lead=_lead_for(ftype, labelled, advice))


def advise_table(
	table: str, columns, *, evidence_lookup: Callable[[str], TableEvidence | None], tracked_apps: tuple[str, ...] = (),
) -> IndexAdvice | None:
	"""The advice for a table card's recommended columns."""
	doctype = doctype_of(table)
	if doctype is None:
		return None
	return advise(table, list(columns or []), evidence=evidence_lookup(f"tab{doctype}"), tracked_apps=tracked_apps)


def _install_text(advice: IndexAdvice) -> str:
	"""How to install the code, which the report shows above the prose (finding and card)."""
	app_name = advice.app_name
	text = (
		f"Save the code above as {app_name}/{app_name}/{HOOK_MODULE}.py and add "
		f'"{app_name}.{HOOK_MODULE}.ensure_indexes" to the {_HOOK_EVENTS_TEXT} lists in hooks.py, '
		"keeping entries already there. If that file already exists, add only this entry to its "
		f"INDEXES list: {json.dumps(advice.entry)}."
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
	advice's own ``code``, shown only when there is code; owner decision D3)."""
	entry = advice.entry or {}
	if advice.route == ROUTE_NO_CODE:
		parts = ["Do not add this index.", advice.reason]
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
	app = app_name if _APP_RE.match(app_name or "") else UNKNOWN_APP
	hook = f"{app}.{HOOK_MODULE}.ensure_indexes"
	body = "".join(f"\t{json.dumps(entry)},\n" for entry in entries)
	hook_lines = "".join(f'#   {event} = ["{hook}"]\n' for event in HOOK_EVENTS)
	return (
		f"# {app}/{app}/{HOOK_MODULE}.py\n"
		f"# In {app}/hooks.py run it after install, right after the install's fixture sync and after\n"
		"# every migrate (add it to these lists when hooks.py already defines them):\n"
		+ hook_lines
		+ "import contextlib\n\nimport frappe\n\n"
		'# An entry with "db" runs only on that database (frappe.db.db_type).\n'
		"INDEXES = [\n" + body + "]\n\n\n" + _ENSURE_FUNCTION
	)
