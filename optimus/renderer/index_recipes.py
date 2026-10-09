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
- ``no_code``: an explanation and no code. A finding recipe's equality columns are put in
  one fixed order, so the same filter in any predicate order gives one recipe and one
  index name (a table card keeps the analyzer's most-used-first order). The index exists
  already when an existing index starts with the recipe's equality columns in any order,
  then its range or sort columns in order; a recipe is also refused when an equality
  column is unique on its own or a unique index's columns are all equality columns, or
  when an existing index serves every column but its Check fields, and a one-column
  recipe when its column leads an index or the index EXPLAIN names. A lookup by name (the
  primary key), or by parent on a child table with a real parent index, needs no other
  index when the key is compared with a value, never with another table's column (a join
  condition, ``_key_lookup``). A recipe wider than ``MAX_INDEX_COLUMNS`` keeps its columns
  by evidence (``_cap``). !=, <> and NOT never narrow an index. A sort-serving index on a
  Filesort finding is one the optimizer rejected, unless the query has a LIMIT
  (``_served_evidence``). The table's real index list decides, never the Search Index
  flag. A no_code that only says Optimus could not tell (``unknown``: no
  evidence for the table, an unread filter, a column the SQL parser did not report, a UNION
  that filters the table in more than one branch, a query too long to parse) is no verdict,
  so a table card never says "Do not add this index." for it.

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

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from optimus.analyzers.base import (
	FRAMEWORK_APPS,
	FRAPPE_METADATA_COLUMNS,
	INDEX_FINDING_TYPES,
	QUERY_TEXT_LIMIT,
	TAB_TABLE_RE,
	is_write_hot_table,
)
from optimus.renderer.ensure_indexes_template import (  # noqa: F401  (public names re-exported)
	_APP_RE,
	_ENSURE_FUNCTION,
	_HOOK_EVENTS_TEXT,
	HOOK_EVENTS,
	HOOK_MODULE,
	UNKNOWN_APP,
	_string_hook_pair,
	ensure_indexes_code,
	optimus_index_name,
)
from optimus.renderer.index_evidence import TableEvidence
from optimus.renderer.where_scan import (  # noqa: F401  (re-exported: tests and index_recipes use them)
	_AND_WORDS,
	_COMMENT_RE,
	_COMPARISONS,
	_EQUALITY_KINDS,
	_FUNCTION,
	_GROUPING_WORDS,
	_LIKE_WORDS,
	_NAME_RE,
	_OR_WORDS,
	_SHAPE_WHY,
	_SQL_TOKEN_RE,
	_VALUE_WORDS,
	_WHERE_ENDS,
	_bracket_pairs,
	_chain_end,
	_conjunct_refs,
	_conjuncts,
	_ref_key,
	_scan_where,
	_valued,
	_where_tokens,
)
from optimus.safe_call import best_effort

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
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PREFIX_SUFFIX_RE = re.compile(r"\(\d+\)$")  # an index prefix length, as in remarks(255)
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
		"An index on these filter columns lets the database skip most of the rows it now reads; "
		"confirm with EXPLAIN that the new index is used."
	),
}
# What an index on the sort or group column removes, for the finding types about it.
_SERVES: dict[str, str] = {"Filesort": "the sort", "Temporary Table": "the temporary table"}
_SORT_LABELS: dict[str, str] = {"Filesort": "ORDER BY", "Temporary Table": "GROUP BY"}
# A table card's neutral verdict when its no-code advice is no verdict on the index (U2).
NO_VERDICT = "Optimus cannot say whether this index would help."

@dataclass(frozen=True)
class IndexAdvice:
	"""One piece of index advice. ``columns`` are what ``frappe.db.add_index`` takes
	(``col(255)`` is a MariaDB text prefix); ``reason`` is the route's explanation;
	``lead`` is the finding type's opening sentence (findings only); ``entry`` is the
	``ensure_indexes()`` entry for ``ROUTE_ENSURE_INDEXES``. ``unknown`` marks a
	``ROUTE_NO_CODE`` that is no verdict on the index, only a sign that Optimus could not
	tell (no evidence for the table, a filter it could not read, a query too long to parse),
	so a table card never says "Do not add this index." for it (U2, E6). ``served_by`` names
	the existing index behind a ``ROUTE_NO_CODE`` that says the index exists already, so
	``advise`` can weigh it against a recipe without the sort; on a sort recipe it is set
	only for an index that returns the rows in the query's order (``_serves_sort``).
	``sort_stays`` is True on a Filesort or Temporary Table finding's code that leaves the
	sort or the temporary table in place (its ``lead`` says why), so the finding's own text
	never promises that the index fixes it."""

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
	served_by: str = ""
	no_evidence: bool = False
	sort_stays: bool = False

	@property
	def code(self) -> str | None:
		if self.route != ROUTE_ENSURE_INDEXES or not self.entry:
			return None
		return ensure_indexes_code([self.entry], app_name=self.app_name)


def doctype_of(table: str) -> str | None:
	"""``"Sales Invoice"`` for ``tabSales Invoice`` (backticks allowed), else None."""
	name = str(table or "").strip().strip("`")
	if not TAB_TABLE_RE.fullmatch(name):
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


def _clean_columns(columns, *, cap: bool = True, keep_creation: bool = False) -> list[str]:
	"""Valid, de-duplicated column names. A text prefix the advisor itself wrote
	(``remarks(255)``) is read back as its bare column, so advising a card's own advice
	again gives the same advice. ``keep_creation`` keeps creation and modified wherever
	they are (other metadata columns still go): the caller orders the columns first and
	applies ``apply_metadata_rule`` after, so their place in the query never decides."""
	out: list[str] = []
	seen: set[str] = set()
	names = [_PREFIX_SUFFIX_RE.sub("", c) for c in columns or [] if isinstance(c, str)]
	valid = [c for c in names if _IDENT_RE.fullmatch(c)]
	if keep_creation:
		kept = [c for c in valid if c.lower() not in FRAPPE_METADATA_COLUMNS or c.lower() in TRAILING_METADATA_OK]
	else:
		kept = apply_metadata_rule(valid)
	for col in kept:
		if col.lower() in seen:
			continue
		seen.add(col.lower())
		out.append(col)
	return out[:MAX_INDEX_COLUMNS] if cap else out


def table_aliases(query: str) -> dict:
	"""sql_metadata's ``tables_aliases`` for ``query`` (``{alias: table}``), {} when it
	cannot parse the query. A second parse of the query, so a render memoises it per query
	text (``recipe_enrichment.make_query_parser``, PF2)."""

	def aliases() -> dict:
		from sql_metadata import Parser

		return dict(Parser(query, disable_logging=True).tables_aliases or {})

	found = best_effort(aliases, {})
	return found if isinstance(found, dict) else {}


# Words sql_metadata can report as a table alias (a UNION ALL query gives {"WHERE": table}).
_NOT_ALIASES: frozenset[str] = _WHERE_ENDS | frozenset({
	"where", "on", "join", "inner", "left", "right", "outer", "cross", "natural", "straight_join", "using",
	"as", "select", "from", "all", "distinct", "set", "values", "and", "or",
})


def _target_qualifiers(query: str, table: str, aliases: Callable[[str], dict] | None = None) -> frozenset[str]:
	"""The target table's name and its aliases. ``aliases(query)`` gives the alias map
	(``table_aliases``, or a per-render memo of it); an SQL keyword is never an alias."""
	found = (aliases or table_aliases)(query)
	return frozenset({table} | {
		alias for alias, real in (found or {}).items()
		if real == table and str(alias).lower() not in _NOT_ALIASES
	})


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


# Names that are also SQL words: one counts as a column only right before a comparison.
_SQL_WORDS: frozenset[str] = (
	_VALUE_WORDS | _GROUPING_WORDS | _OR_WORDS | _AND_WORDS
	| frozenset({"not", "null", "escape", "collate", "div", "mod", "regexp", "rlike", "sounds"})
)
_COMPARED_BY: frozenset[str] = _COMPARISONS | frozenset({"is", "in", "not", "between"}) | _LIKE_WORDS


def _where_columns(query: str, qualifiers, columns: Mapping[str, str], *, truncated: bool = False) -> set[str]:
	"""Lowercase target-table columns the main query's WHERE clause names, read by the
	token scan: a name chain that is no call, qualified by the target table or one of its
	aliases or not at all, and a real column of the table; a name that is also an SQL word
	(DATE, BINARY, ...) only right before a comparison. A ``(SELECT ...)`` group is skipped,
	and an unscannable clause gives the empty set."""
	tokens = _where_tokens(query, truncated=truncated)
	if not tokens:
		return set()
	pairs = _bracket_pairs(tokens)
	out: set[str] = set()
	i = 0
	while i < len(tokens):
		tok = tokens[i]
		if tok == "(":
			close = pairs.get(i, len(tokens))
			inner = tokens[i + 1].lower() if i + 1 < close else ""
			i = close + 1 if inner in ("select", "with") else i + 1
			continue
		if not _NAME_RE.fullmatch(tok):
			i += 1
			continue
		j = _chain_end(tokens, i)
		after = tokens[j + 1].lower() if j + 1 < len(tokens) else ""
		qualifier, name = _ref_key(tokens, i, j)
		if (
			after != "(" and name in columns and (qualifiers is None or not qualifier or qualifier in qualifiers)
			and (name not in _SQL_WORDS or after in _COMPARED_BY)
		):
			out.add(name)
		i = j + 1
	return out


_JOIN_SIDES: frozenset[str] = frozenset({"left", "right", "inner", "cross", "full", "natural"})


def _from_clause(query: str) -> list[str] | None:
	"""Lowercase unquoted names of the tables in the main query's FROM clause (the first
	one outside brackets), in order, "(derived)" for a subquery; None when there is none.
	A "." qualifies only the table name right before it (``db.table``): the qualified
	columns of a JOIN's ON clause (``ON gl.voucher_no = p.name``) are never table names, or
	the joined table would drop out of the list."""
	tokens = [tok for tok in _SQL_TOKEN_RE.findall(query or "") if not _COMMENT_RE.match(tok)]
	depth = 0
	start = None
	for i, tok in enumerate(tokens):
		depth += {"(": 1, ")": -1}.get(tok, 0)
		if not depth and tok.lower() == "from":
			start = i + 1
			break
	if start is None:
		return None
	out: list[str] = []
	expect = True
	depth = 0
	named_at = -1  # the token index of the last table name read
	for i in range(start, len(tokens)):
		tok = tokens[i]
		if tok == "(":
			if not depth and expect:
				out.append("(derived)")
				expect = False
			depth += 1
			continue
		if tok == ")":
			depth -= 1
			continue
		if depth:
			continue
		low = tok.lower()
		if low == "where" or low in _CLAUSE_ENDS:
			break
		if low in (",", "join", "straight_join"):
			expect = True
		elif low == "." and out and named_at == i - 1 and i + 1 < len(tokens):
			out[-1] = tokens[i + 1].strip('`"').lower()  # db.table: the table
			named_at = i + 1
		elif expect and _NAME_RE.fullmatch(tok) and low not in _JOIN_SIDES | {"outer", "lateral"}:
			out.append(tok.strip('`"').lower())
			expect = False
			named_at = i
	return out


def _key_lookup(valued: Mapping[str, str], evidence: TableEvidence | None) -> str:
	"""NO_CODE text when the WHERE clause finds its rows by a key that already has its index:
	name, the primary key, or parent on a child table (one parent has only a few rows).
	``valued`` holds the equality uses compared with a value (``_scan_where``): a key
	compared with another table's column is a join condition, never a lookup (round 4,
	item 1). Frappe indexes parent on a MariaDB child table only (mariadb/schema.py; none on
	Postgres), so the parent rule needs a real index that parent leads (item 2). Else ""."""
	by_name = {str(col).lower(): kind for col, kind in (valued or {}).items()}
	kind = by_name.get("name")
	if kind in _EQUALITY_KINDS:
		what = "its row" if kind == "eq" else "its rows"
		return f"The query finds {what} by name, the primary key, so no other index can help."
	if evidence is None or by_name.get("parent") not in _EQUALITY_KINDS:
		return ""
	columns = {col.lower() for col in evidence.column_types}
	index = next((ix for ix in evidence.indexes if ix.columns[:1] == ("parent",)), None)
	if index is not None and {"parent", "parenttype"} <= columns:
		return (
			f'The query finds its rows by parent, which the index "{index.name}" on table "{evidence.table}" '
			"serves, and one parent has only a few rows, so no other index can help."
		)
	return ""


def _join_probes(query: str, qualifiers, target: str) -> set[str]:
	"""Lowercase columns of the target table that a LEFT JOIN only uses as probe values: the
	column sits in the ON clause of a LEFT JOIN whose joined table is another table, and the
	WHERE clause of that SELECT names a column of the joined table only as ``col IS NULL``,
	if at all (any other WHERE on it rejects NULLs, which makes the database run an inner
	join it may drive either way; an anti-join keeps them). A column the
	target's own join compares (its lookup key) is never a probe. Every SELECT level is read,
	derived tables included; an unqualified ON column is never counted (M2, item 4)."""
	tokens = [tok for tok in _SQL_TOKEN_RE.findall(query or "") if not _COMMENT_RE.match(tok)]
	quals = {str(q).strip('`"').lower() for q in qualifiers or ()}
	scope: list[int] = []  # the index of the "(" each token sits in, -1 at the top
	stack: list[int] = []
	for i, tok in enumerate(tokens):
		if tok == ")" and stack:
			stack.pop()
		scope.append(stack[-1] if stack else -1)
		if tok == "(":
			stack.append(i)
	ends = {"where", "group", "order", "limit", "having", "union", "window", "for", "lock", "join", ",", "on"} | _JOIN_SIDES

	def clause(start: int, stop_words) -> list[str]:
		"""The tokens from ``start`` to a stop word at its own level or the ")" that closes it."""
		out: list[str] = []
		depth = 0
		for tok in tokens[start:]:
			if tok == "(":
				depth += 1
			elif tok == ")":
				if not depth:
					break
				depth -= 1
			elif not depth and tok.lower() in stop_words:
				break
			out.append(tok)
		return out

	def qualified(toks: list[str]) -> list[tuple[str, str]]:
		return [
			(toks[k].strip('`"').lower(), toks[k + 2].strip('`"').lower())
			for k in range(len(toks) - 2) if toks[k + 1] == "." and _NAME_RE.fullmatch(toks[k])
		]

	def rejects_nulls(toks: list[str], names: set[str]) -> bool:
		"""True when the WHERE names a column of the joined table other than as a bare
		``col IS NULL``: only that keeps the rows the LEFT JOIN fills with NULLs (an anti-join,
		``WHERE c.name IS NULL``), so the join stays a LEFT JOIN."""
		for k in range(len(toks) - 2):
			if toks[k + 1] != "." or not _NAME_RE.fullmatch(toks[k]) or toks[k].strip('`"').lower() not in names:
				continue
			before = toks[k - 1].lower() if k else ""
			if before == "not" or [tok.lower() for tok in toks[k + 3 : k + 5]] != ["is", "null"]:
				return True
		return False

	probes: set[str] = set()
	keys: set[str] = set()
	for i, tok in enumerate(tokens):
		if tok.lower() != "join" or i + 1 >= len(tokens) or tokens[i + 1] == "(":
			continue
		side = tokens[i - 1].lower() if i else ""
		if side == "outer" and i > 1:
			side = tokens[i - 2].lower()
		joined = tokens[i + 1].strip('`"').lower()
		names = {joined}
		k = i + 2
		if k < len(tokens) and tokens[k].lower() == "as":
			k += 1
		if k < len(tokens) and _NAME_RE.fullmatch(tokens[k]) and tokens[k].lower() not in ends | {"using"}:
			names.add(tokens[k].strip('`"').lower())
			k += 1
		if k >= len(tokens) or tokens[k].lower() != "on":
			continue
		on = clause(k + 1, ends - {"on"})
		where_at = next((w for w in range(k + 1, len(tokens)) if scope[w] == scope[i] and tokens[w].lower() == "where"), None)
		where = clause(where_at + 1, {"group", "order", "limit", "having", "union", "window", "for", "lock"}) if where_at else []
		fixed = side == "left" and not rejects_nulls(where, names)
		for q, col in qualified(on):
			if q not in quals:
				continue
			if joined == target:
				keys.add(col)
			elif fixed:
				probes.add(col)
	return probes - keys


def _index_order(
	cols: list[str], comparisons: Mapping[str, str], *, serves: str = "", removes: str = "",
) -> tuple[list[str], list[tuple[str, str]]]:
	"""Recipe columns in index order: equality columns first, then the sort or group
	columns. A range column (a sort column that is also range-filtered counts as the sort
	column, one index serving both) cannot share an index with a sort column after it:
	when the index ``serves`` a sort or a grouping (Filesort, Temporary Table) the range
	filter is left out, otherwise one range column follows the equality columns and the
	rest is left out. A sort or group column the index cannot serve (no ``serves``: a filter
	matches several values, or the sort is no plain column list) is left out too, since it
	would only widen the index (M8); a range-filtered sort column (``"rsort"``) then still
	counts as the range it is. ``removes`` names what the finding is about (the sort or the
	temporary table) for that reason. Each left-out column comes with the reason."""
	eq = [col for col in cols if comparisons.get(col, "eq") in _EQUALITY_KINDS]
	ranges = [col for col in cols if comparisons.get(col) == "range" or (not serves and comparisons.get(col) == "rsort")]
	sorts = [col for col in cols if comparisons.get(col) == "sort" or (serves and comparisons.get(col) == "rsort")]
	if not ranges:
		if serves or not sorts:
			return eq + sorts, []
		if removes == "the temporary table":
			why = "an index cannot return these groups in order, so it would not remove the temporary table"
		else:
			why = "an index cannot return these rows in order, so it would not remove the sort"
		return eq, [(col, why) for col in sorts]
	if serves and sorts:
		return eq + sorts, [
			(col, f"the range filter on {col} cannot also use this index, which removes {serves} instead")
			for col in ranges
		]
	why = f"it comes after the range condition on {ranges[0]}, so the index cannot use it"
	stays = (
		f"it comes after the range condition on {ranges[0]}, so the index cannot return the rows in order and "
		f"{removes or 'the sort'} stays"
	)
	return eq + ranges[:1], [(col, why) for col in ranges[1:]] + [(col, stays) for col in sorts]


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


def _clause_columns(query: str, keyword: str, qualifiers, columns: Mapping[str, str]) -> list[str]:
	"""Real target-table columns (``columns``: lowercase name to column) that are whole items
	of the main query's ORDER BY or GROUP BY clause (``keyword`` "order" or "group"), in
	order: a bare name, qualified by the target table, one of its aliases or not at all, then
	ASC or DESC. [] for a query with a top-level UNION, whose ORDER BY sorts the union's
	rows (round 4, item 6)."""
	tokens = [tok for tok in _SQL_TOKEN_RE.findall(query or "") if not _COMMENT_RE.match(tok)]
	depth = 0
	for tok in tokens:
		depth += {"(": 1, ")": -1}.get(tok, 0)
		if not depth and tok.lower() == "union":
			return []
	out: list[str] = []
	for item in _clause_items(tokens, keyword) or []:
		if not item or not _NAME_RE.fullmatch(item[0]):
			continue
		j = _chain_end(item, 0)
		if [tok.lower() for tok in item[j + 1 :]] not in ([], ["asc"], ["desc"]):
			continue
		qualifier, name = _ref_key(item, 0, j)
		col = columns.get(name)
		if col is not None and col not in out and (not qualifier or qualifier in qualifiers):
			out.append(col)
	return out


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
	- ``"other_table"``: an item is a column of another table of the query;
	- ``"alias"``: an item is a select alias;
	- ``"not_column"``: an item names no column of this table;
	- ``"text"``: an item is a text column; ``"unindexable"``: a JSON-like column;
	- ``"directions"``: the items mix ASC and DESC;
	- ``"expression"``: anything else (a function, FIELD(), CASE, arithmetic or a parameter
	  around an item), or no evidence.

	Every code but "" is a reason the sort or the temporary table stays; ``_sort_cause``
	names it, so the text never lists causes the query does not have."""
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
		if qualifier and qualifiers is not None and qualifier not in qualifiers:
			return "other_table"
		col = columns.get(name)
		if name in aliases:
			return "alias"
		if col is None:
			return "not_column"
		if col in evidence.text_columns:
			return "text"
		if _unindexable(evidence, col):
			return "unindexable"
		names.append(name)
		directions.add(rest[0] if rest else "asc")
	if len(directions) > 1:
		return "directions"
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
	nots = [col for col, kinds in shapes.items() if "not" in kinds and not kinds & {"or", "like"}]
	if nots:
		out.append(f"a !=, <> or NOT comparison on {', '.join(nots)}, which usually matches most of the table's rows")
	for col, kinds in shapes.items():
		names = _function_names(kinds)
		out += [_function_phrase(name, col) for name in names]
		if "expression" in kinds and not names:
			out.append(f"arithmetic on {col}")
	return out


def _shape_why(kinds: set[str]) -> str:
	"""Why one column was left out, by precedence: or, like, function, expression, not,
	unsure."""
	if "or" in kinds:
		return _SHAPE_WHY["or"]
	if "like" in kinds:
		return _SHAPE_WHY["like"]
	names = _function_names(kinds)
	if names:
		return _SHAPE_WHY.get(names[0]) or _SHAPE_WHY["function"].format(name=names[0])
	if "expression" in kinds:
		return _SHAPE_WHY["expression"]
	if "not" in kinds:
		return _SHAPE_WHY["not"]
	return _SHAPE_WHY["unsure"]


def _check_rarity(checks) -> str:
	"""What Optimus can say about Check fields: most rows usually share one value, which it
	cannot see (R1), so the text never states it as a fact."""
	if len(checks) == 1:
		return f"{checks[0]} is a Check field, which usually matches most of the table's rows"
	return f"{', '.join(checks)} are Check fields, which usually match most of the table's rows"


def _values(checks) -> str:
	return "value" if len(checks) == 1 else "values"


def _not_rarity(nots) -> str:
	"""What Optimus can say about a !=, <> or NOT filter: it usually keeps most rows, but when
	most rows hold the value it leaves out (an empty project on most Stock Entries) the rows
	it keeps are rare and an index finds them. Optimus cannot see which, so it hedges."""
	if len(nots) == 1:
		return f"A !=, <> or NOT comparison on {nots[0]} usually matches most of the table's rows"
	return f"!=, <> or NOT comparisons on {', '.join(nots)} usually match most of the table's rows"


def _rewrites(shapes: Mapping[str, set[str]]) -> list[str]:
	"""The rewrite for each filter shape the scan found, in the order found, each once: a
	text never suggests a rewrite for a shape the query does not have."""
	out: list[str] = []
	for kinds in shapes.values():
		names = _function_names(kinds)
		found = [
			*(["an exact match or a pattern without a leading wildcard instead of the LIKE"] if "like" in kinds else []),
			*(["one query per OR branch"] if "or" in kinds else []),
			*(
				"the comparison itself instead of NOT (...) around it" if name == "NOT"
				else "the bare column instead of the CASE expression" if name == "CASE"
				else f"the bare column instead of {name}() around it"
				for name in names
			),
			*(
				["the bare column compared with a value instead of arithmetic or another column"]
				if "expression" in kinds and not names else []
			),
		]
		out += [text for text in found if text not in out]
	return out


def _or_list(items: list[str]) -> str:
	return items[0] if len(items) == 1 else f"{', '.join(items[:-1])}, or {items[-1]}"


def _shape_no_code_reason(
	shapes: Mapping[str, set[str]], checks: list[str], *, query: str = "", card: bool = False,
) -> tuple[str, bool]:
	"""``(text, unknown)`` for a NO_CODE when the filter leaves no column an index could
	narrow on: the filter shape, the columns Optimus could not read, any !=, <> or NOT
	columns and any Check fields left. ``unknown`` is True when part of the filter could not
	be read, so the text is no verdict on the index, and on a table ``card`` when the text
	only hedges (a Check or != filter can be the rare value): "Do not add this index."
	would contradict "an index can help". The text names only the shapes the scan found and
	only their rewrites; with no ``query`` (a card, a Missing Index finding) it speaks of the
	slow queries and never tells the reader to change a filter it has not seen."""
	known = {col: kinds for col, kinds in shapes.items() if kinds != {"unsure"}}
	unsure = [col for col, kinds in shapes.items() if kinds == {"unsure"}]
	nots = [col for col, kinds in known.items() if kinds == {"not"}]
	shaped = {col: kinds for col, kinds in known.items() if kinds != {"not"}}
	explain = "Check the query with EXPLAIN to see which index it needs."
	if not known and not checks:
		return (
			"Optimus could not read how this query combines its filters (it may be cut short, wrapped in "
			f"brackets, a UNION or a derived table), so it gives no index code. {explain}"
		), True
	parts: list[str] = []
	if shaped:
		which = f"An index on {next(iter(shaped))}" if len(shaped) == 1 else "An index on those columns"
		parts.append(
			f"The cost comes from the shape of the filter: {'; '.join(_shape_phrases(shaped))}. {which} cannot "
			"serve that filter."
		)
	if unsure:
		parts.append(f"Optimus could not read how the query filters on {', '.join(unsure)}.")
	if nots:
		parts.append(
			f"{_not_rarity(nots)}; if the rows this query looks for are rare, an index on {_cols_text(nots)} can help."
		)
	if checks:
		# Optimus cannot see how the values are spread, so the rare value keeps its index (R1)
		who = "this query looks" if query else "the slow queries look"
		parts.append(
			f"{_check_rarity(checks)}; if {who} for the rare {_values(checks)}, an index on {_cols_text(checks)} can help."
		)
	if unsure:
		parts.append(f"So Optimus gives no index code. {explain}")
		return " ".join(parts), True
	hedged = bool(checks or nots)
	parts.append(
		"So Optimus gives no index code." if hedged else "So an index would not help, and Optimus gives no index code."
	)
	rewrites = _rewrites(shaped)
	if rewrites:
		parts.append(f"Rewrite the filter ({_or_list(rewrites)}) and check the result with EXPLAIN.")
	elif checks and query:
		parts.append("Filter on a more selective field as well, and check the result with EXPLAIN.")
	elif checks:
		which = "this column" if len(checks) == 1 else "these columns"
		parts.append(f"Check the slow queries on {which} with EXPLAIN to see which {_values(checks)} they look for.")
	else:
		parts.append("Check the query with EXPLAIN to see how many rows it reads.")
	return " ".join(parts), card and hedged


def _existing_tail(query: str, shapes: Mapping[str, set[str]], columns: int = 1) -> str:
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
	which = "this column" if columns == 1 else "these columns"
	return f"Check the slow queries on {which} with EXPLAIN to see which index they use and how many rows they read."


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


def _unique_alone(evidence: TableEvidence, col: str) -> bool:
	"""True when ``col`` is unique on its own: a Unique field or a one-column unique index
	(a composite unique index it leads is only an index it leads, C6)."""
	field = evidence.fields.get(col)
	return (field is not None and field.unique) or any(ix.unique and ix.columns == (col,) for ix in evidence.indexes)


def _serves(index_columns: tuple[str, ...], equality: tuple[str, ...], rest: tuple[str, ...]) -> bool:
	"""True when an index on ``index_columns`` serves a recipe of ``equality`` columns (any
	order: an equality filter uses a leading column set the same way in every order)
	followed by ``rest`` (the range or sort columns, which must follow in order)."""
	k = len(equality)
	return set(index_columns[:k]) == set(equality) and tuple(index_columns[k : k + len(rest)]) == tuple(rest)


def _existing_index_problem(
	evidence: TableEvidence,
	cols: list[str],
	equality: set[str],
	explain_row,
	query: str,
	shapes: Mapping[str, set[str]],
	*,
	sort_tail: bool = False,
) -> tuple[str | None, str, bool]:
	"""``(text, index name, hedged)`` for a NO_CODE when the FINAL recipe (``cols``, bare
	names; ``equality`` names its columns compared by =, IN or IS NULL, every column of a
	table card) exists already or cannot help. A recipe of several columns is refused only
	for the recipe as a whole (C1): its first column leading an index of its own, or the one
	EXPLAIN names, says nothing about the composite. It is refused when

	- an equality column is unique on its own: the database finds its rows through that
	  index (a range or sort column stays exempt: ``ORDER BY username`` needs the composite);
	- an existing index serves it: it starts with the equality columns in any order, then
	  the range or sort columns in order, so ``(posting_date, company)`` serves
	  ``company = ? AND posting_date = ?`` (the equality block is put in one order by
	  ``_canonical``, so the verdict, the columns and the index name never depend on the
	  order of the query's predicates);
	- a unique index's columns are all equality columns: it returns at most one row (M3);
	- an existing index serves every column but the Check fields (the non-Check equality
	  columns in any order, then the range or sort columns in order), which match too many
	  rows for an index to narrow (the advisor's own Check rule; M1: Frappe's creation index
	  serves ``creation > ? AND is_return = ?``). This verdict is ``hedged``: the text names
	  the index that helps a query for the rare value. When the recipe's equality columns
	  are all Check fields, the index serves no filter column, only the tail, so it names no
	  index (``creation`` serves the sort of ``is_return = ? ORDER BY creation``, not its
	  filter); and when that tail is the sort (``sort_tail``) of a query with a LIMIT, the
	  recipe stands: the database sorted although that index exists, and an index on the
	  Check fields and then the sort returns the first rows directly for any value.

	A single-column recipe is refused when its column is unique on its own, leads an
	existing index under any name, or leads an index EXPLAIN names. Search Index is never
	proof (C2): on Postgres a Search Index is named after the bare field and index names are
	schema-wide, so only the first table with that field name gets one; the table's real
	index list decides. creation and modified never stand alone in a recipe
	(``apply_metadata_rule``), so Frappe's own creation index never refuses one (D5). With
	no ``query`` (a table card) the texts speak of the slow queries."""
	whole = tuple(cols)
	tail = _existing_tail(query, shapes, len(whole))
	table = evidence.table
	compares = "this query compares" if query else "the slow queries compare"
	if len(whole) > 1:
		k = 0
		while k < len(whole) and whole[k] in equality:
			k += 1
		eq, rest = whole[:k], whole[k:]
		unique = next((col for col in eq if _unique_alone(evidence, col)), None)
		if unique is not None:
			return (
				f'Column "{unique}" is already unique, so the database finds the rows through its index and a '
				f"new index would not help. {tail}"
			), "", False
		# a unique index whose columns the equality filter all fixes returns at most one row (M3)
		pinned = next((ix for ix in evidence.indexes if ix.unique and ix.columns and set(ix.columns) <= set(eq)), None)
		if pinned is not None:
			return (
				f'The unique index "{pinned.name}" on table "{table}" covers {_cols_text(pinned.columns)}, which '
				f"{compares} with known values, so the database finds the rows through it and a new index "
				f"would not help. {tail}"
			), pinned.name, False
		covering = next((ix for ix in evidence.indexes if _serves(ix.columns, eq, rest)), None)
		if covering is not None:
			starts = tuple(covering.columns[: len(whole)])
			same = "" if starts == whole else " (the same equality columns in another order)"
			return (
				f'The index "{covering.name}" on table "{table}" already starts with {_cols_text(starts)}{same}, '
				f"so this index exists already and a new one would not help. {tail}"
			), covering.name, False
		plain = tuple(col for col in eq if not _is_check_field(evidence, col))
		checks = [col for col in eq if _is_check_field(evidence, col)]
		if checks and (plain or rest):
			# an existing index serves every other column (the range or sort tail too, as
			# Frappe's creation index serves creation > ?), and Check fields add nothing (M1)
			prefix = next((ix for ix in evidence.indexes if _serves(ix.columns, plain, rest)), None)
			if prefix is not None:
				if not plain and sort_tail and _has_limit(query):
					return None, "", False
				looks = "this query looks" if query else "the slow queries look"
				return (
					f'The index "{prefix.name}" on table "{table}" already starts with '
					f"{_cols_text(prefix.columns[: len(plain) + len(rest)])}, and {_check_rarity(checks)}, so Optimus "
					f"gives no index code; if {looks} for the rare {_values(checks)}, an index on "
					f"{_cols_text(whole)} can help. {tail}"
				), (prefix.name if plain else ""), True
		return None, "", False
	col = whole[0]
	if _unique_alone(evidence, col):
		unique_ix = next((ix.name for ix in evidence.indexes if ix.unique and ix.columns == (col,)), "")
		return f'Column "{col}" is already unique, so the database already has an index on it. {tail}', unique_ix, False
	led = next((ix for ix in evidence.indexes if ix.columns[:1] == (col,)), None)
	if led is not None:
		return (
			f'Column "{col}" already leads the index "{led.name}" on table "{table}", so a new '
			f"index would not help. {tail}"
		), led.name, False
	for name, used in _explain_index_names(explain_row):
		named = next((ix for ix in evidence.indexes if ix.name == name), None)
		if (named is not None and named.columns[:1] == (col,)) or name in (col, f"{col}_index"):
			how = "uses" if used else "can use"
			return (
				f'EXPLAIN shows the database {how} the index "{name}" on column "{col}", so a new index '
				f"would not help. {tail}"
			), "", False
	return None, "", False


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


def _no_code(doctype: str, cols, reason: str, *, unknown: bool = False, no_evidence: bool = False) -> IndexAdvice:
	return IndexAdvice(
		route=ROUTE_NO_CODE, doctype=doctype, table=f"tab{doctype}", columns=tuple(cols), reason=reason,
		unknown=unknown, no_evidence=no_evidence,
	)


def _read_failed_reason(doctype: str) -> str:
	"""The reason for a table whose evidence READ raised (O2), not one with no DocType on
	this site: the table may well exist, so the text must not say it does not."""
	return (
		f"Optimus could not read the details of table \"tab{doctype}\" (an error while it read the "
		"columns and indexes). So it gives no index code. Check the slow queries on it with EXPLAIN; if it "
		"keeps happening, send the bench log line \"optimus: evidence read failed\" to the Optimus maintainers."
	)


def _with_read_failure(advice: IndexAdvice | None, evidence_lookup) -> IndexAdvice | None:
	"""``advice`` with the read-failure reason when it is the no-evidence advice for a table
	whose evidence read raised (the lookup says so through ``read_failed``)."""
	if advice is None or not advice.no_evidence:
		return advice
	read_failed = getattr(evidence_lookup, "read_failed", None)
	if read_failed is None or not read_failed(f"tab{advice.doctype}"):
		return advice
	return replace(advice, reason=_read_failed_reason(advice.doctype))


def _no_evidence(doctype: str, cols) -> IndexAdvice:
	"""NO_CODE for a table Optimus has no evidence for (E6): not the table of a DocType on
	this site (a core table such as tabSessions, tabSeries or tabSingles, a virtual DocType, a
	removed one) or one whose evidence could not be read. No verdict on the index."""
	return _no_code(doctype, cols, (
		f'Optimus has no information about table "tab{doctype}": it is not the table of a DocType on this '
		"site (a core table such as tabSessions or tabSeries, a virtual DocType or a removed one), or Optimus "
		"could not read it. So it gives no index code. Check the slow queries on it with EXPLAIN."
	), unknown=True, no_evidence=True)


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
		grouped: dict[str, list[str]] = {}
		for col, why in dropped:
			grouped.setdefault(why, []).append(col)
		out.append("Optimus left out " + "; ".join(f"{', '.join(cols)} ({why})" for why, cols in grouped.items()) + ".")
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
	removes: str = "",
	card: bool = False,
) -> IndexAdvice | None:
	"""The advice for indexing ``columns`` of ``table``, or None when there is nothing
	to advise (not a DocType table, no usable column). ``card`` is True for a table card,
	where a no-code that only hedges is no verdict (``_shape_no_code_reason``).
	``unusable`` names the columns the query's predicate shape keeps an index from using
	(``_scan_where``): they are left out and named. ``comparisons``
	(``{column: "eq" | "in" | "range" | "sort" | "rsort"}``, ``"rsort"`` a sort column that is
	also range-filtered)
	puts the columns in index order (``_index_order``; ``serves`` names the sort or the
	temporary table a Filesort or Temporary Table index removes). When that sort-first
	recipe gives no code or loses its plain sort column (a prefix, the column cap), the
	recipe without ``serves`` is given instead: an existing sort-serving index on a Filesort
	finding is one the optimizer rejected, and the text names it. Only LIMIT keeps that
	served verdict, and its text then names the recipe without the sort
	(``_served_evidence``). A recipe made only of Check fields, or with nothing left, is
	NO_CODE. The existing-index checks run on the final recipe, after columns were left out
	(``_existing_index_problem``)."""
	kwargs = {
		"evidence": evidence, "tracked_apps": tracked_apps, "explain_row": explain_row, "query": query,
		"unusable": unusable, "comparisons": comparisons, "removes": removes or serves, "card": card,
	}
	advice = _advise(table, columns, serves=serves, **kwargs)
	if serves:
		sorts = [col for col, kind in (comparisons or {}).items() if kind in ("sort", "rsort")]
		kept = {c.split("(", 1)[0] for c in advice.columns} if advice is not None else set()
		# without a LIMIT the query reads every row it matches, so a usable range filter on
		# another column beats an index that only returns the rows in order (Filesort only); a
		# metadata column Optimus never indexes (docstatus < 2) is no such filter
		ranged = [
			col for col, kind in (comparisons or {}).items()
			if kind == "range" and col not in (unusable or {})
			and col.lower() not in FRAPPE_METADATA_COLUMNS - TRAILING_METADATA_OK
		]
		range_first = serves == "the sort" and bool(ranged) and not _has_limit(query)
		# a recipe that keeps only some sort columns (the cap or the key width left the rest out)
		# never returns the rows in the query's order (R2)
		if advice is None or advice.route == ROUTE_NO_CODE or range_first or not all(col in kept for col in sorts):
			cut_sort = advice is not None and advice.route != ROUTE_NO_CODE and not all(col in kept for col in sorts)
			retry = _advise(table, columns, serves="", **kwargs)
			served_by = advice.served_by if advice is not None else ""
			if served_by and _served_evidence(query):
				# the sort-serving index exists and the optimizer may well use it (item 1)
				advice = replace(advice, reason=_served_reason(advice, retry))
			elif served_by and retry is not None and retry.served_by != served_by:
				# an existing sort-serving index on a Filesort finding was rejected by the
				# optimizer (or added after the capture), so the recipe without the sort is the
				# advice, and it names that index (round 4, item 3)
				advice = replace(retry, caveats=(_unused_index_note(advice, serves), *retry.caveats))
			elif cut_sort and retry is not None and retry.route == ROUTE_NO_CODE:
				# the filter's index exists already; say why the sort or the temporary table stays (R2)
				missing = ", ".join(col for col in sorts if col not in kept)
				column = "sort" if serves == "the sort" else "grouping"
				advice = replace(retry, caveats=(*retry.caveats, (
					f"An index that returns these rows in the query's order would need every {column} column "
					f"({', '.join(sorts)}), and Optimus leaves out {missing} here, so {serves} stays."
				)))
			elif range_first and retry is not None and retry.route == ROUTE_NO_CODE:
				# the range filter's index exists already; say why the sort stays
				advice = replace(retry, caveats=(*retry.caveats, (
					f"The query has no LIMIT, so it reads every row the range filter on {ranged[0]} matches, and the "
					"database usually reads fewer rows through that filter than through an index that returns them "
					f"in order, so {serves} stays."
				)))
			else:
				# a retry left with nothing to index (the sort column was all) keeps the first verdict
				advice = retry or advice
	return advice


def _only_not_null(query: str, qualifiers, col: str) -> bool:
	"""True when every AND piece of the main WHERE that names ``col`` (of the target table) is
	``col IS NOT NULL``, which narrows no index in practice (R3), and at least one does."""
	tokens = _where_tokens(query)
	conjuncts = _conjuncts(tokens) if tokens else None
	found = False
	for part in conjuncts or []:
		if not any(name == col.lower() for name, *_rest in _conjunct_refs(part, qualifiers)):
			continue
		if not _NAME_RE.fullmatch(part[0]):
			return False
		j = _chain_end(part, 0)
		if _ref_key(part, 0, j)[1] != col.lower() or [tok.lower() for tok in part[j + 1 :]] != ["is", "not", "null"]:
			return False
		found = True
	return found


def _has_limit(query: str) -> bool:
	"""True when the main query has a LIMIT (it takes only its first rows)."""
	depth = 0
	for tok in _SQL_TOKEN_RE.findall(query or ""):
		depth += {"(": 1, ")": -1}.get(tok, 0)
		if not depth and tok.lower() == "limit":
			return True
	return False


def _served_evidence(query: str) -> bool:
	"""True when an existing index that serves a sort recipe may still be the answer: the
	query takes only its first rows (LIMIT), which that index returns in order. Without it
	the finding itself says the optimizer chose another plan (item 1). A capture-time EXPLAIN
	is no evidence (round 4, item 3): MariaDB's possible_keys never lists an index that only
	serves the ORDER BY, and a Postgres plan node names only the index it used."""
	return _has_limit(query)


def _unused_index_note(advice: IndexAdvice, serves: str) -> str:
	"""The range recipe's note naming the existing index that could remove the sort or the
	temporary table but did not (round 4, item 3)."""
	return (
		f'The index "{advice.served_by}" on table "{advice.table}" can already remove {serves} for this filter, '
		"but the captured query did not use it that way: the database chose another plan (or the index was "
		"added after the capture). Check the query with EXPLAIN to see which index it uses."
	)


def _served_reason(advice: IndexAdvice, retry: IndexAdvice | None) -> str:
	"""The kept served verdict's text: it names the recipe without the sort as the
	alternative and never says a new index would not help (item 1c)."""
	because = "the query takes only its first rows, which that index returns in order"
	if retry is not None and retry.route != ROUTE_NO_CODE:
		cols = tuple(c.split("(", 1)[0] for c in retry.columns)
		alt = (
			f" If EXPLAIN shows the database does not use it, an index on {_cols_text(cols)} for the filter may "
			"help instead; check the query with EXPLAIN."
		)
	else:
		alt = " Check the query with EXPLAIN to see whether the database uses it."
	return (
		f'The index "{advice.served_by}" on table "{advice.table}" already serves this filter and sort '
		f"({because}), so Optimus gives no index code.{alt}"
	)


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
	removes: str = "",
	card: bool = False,
) -> IndexAdvice | None:
	doctype = doctype_of(table)
	if doctype is None:
		return None
	shapes = {
		col: set(kinds) for col, kinds in (unusable or {}).items()
		if isinstance(col, str) and _IDENT_RE.fullmatch(col) and col.lower() not in FRAPPE_METADATA_COLUMNS
	}
	cols = _clean_columns([c for c in columns or [] if c not in (unusable or {})], cap=False, keep_creation=True)
	order_dropped: list[tuple[str, str]] = []
	if comparisons:
		cols, order_dropped = _index_order(cols, comparisons, serves=serves, removes=removes)
	k = _equality_count(cols, comparisons)
	equality = set(cols[:k])
	if comparisons:
		# a finding's equality block in one order; a card keeps the analyzer's most-used-first
		# order, which its frozen text promises (M6)
		cols = _canonical(cols, k, evidence)
	# creation / modified never lead, after the reordering either
	cols = apply_metadata_rule(cols)
	cols, capped = _cap(cols, equality, evidence if comparisons else None)
	if not cols and not shapes:
		return None
	if evidence is None:
		return _no_evidence(doctype, cols or list(shapes))
	if capped:
		# never leave out every column of an index the query fixes when it narrows the rows at
		# least as well as the capped recipe could: a unique one finds at most one row (item 3),
		# and one whose lead column ranks by field type at least as well as the weakest kept
		# column (the cap's own rank) is as selective as any column the recipe keeps (round 5).
		# A Select index left out behind Link columns may match many rows, so the capped recipe
		# stays (round 4, item 4). The verdict is about the filter only, never the sort, so it
		# names no sort-serving index (item 5)
		cut = [
			ix for ix in evidence.indexes
			if ix.columns and set(ix.columns) <= equality and not set(ix.columns) & set(cols)
		]
		compares = "this query compares" if query else "the slow queries compare"
		check = (
			"Check the query with EXPLAIN to see which index it uses." if query
			else "Check the slow queries with EXPLAIN to see which index they use."
		)
		unique = next((ix for ix in cut if ix.unique), None)
		if unique is not None:
			return _no_code(doctype, cols, (
				f'The unique index "{unique.name}" on table "{evidence.table}" already finds these rows by '
				f"{_cols_text(unique.columns)}, which {compares} with known values, so it returns at most "
				f"one row, and an index here holds at most {MAX_INDEX_COLUMNS} columns, so Optimus gives no index "
				f"code. {check}"
			))
		weakest = max((_type_rank(evidence, col) for col in cols), default=0)
		found = next((ix for ix in cut if _type_rank(evidence, ix.columns[0]) <= weakest), None)
		if found is not None:
			return _no_code(doctype, cols, (
				f'The index "{found.name}" on table "{evidence.table}" covers {_cols_text(found.columns)}, which '
				f"{compares} with known values. An index here holds at most {MAX_INDEX_COLUMNS} columns, so a new "
				"one would leave that out, and by field type it narrows the rows at least as well as the columns a "
				f"new index would keep, so Optimus gives no index code. {check}"
			))
		order_dropped = order_dropped + [
			(col, f"an index here holds at most {MAX_INDEX_COLUMNS} columns") for col in capped
		]
	# a name the table does not have (a fragment of a query cut short) is never shown
	named = list(shapes)
	shapes = {col: kinds for col, kinds in shapes.items() if col in evidence.fields}
	if not cols and not shapes and named:
		# every filter column the shape left out is no field of the table: say which
		problem = _column_problem(evidence, named)
		if problem:
			return _no_code(doctype, named, problem)
	if not cols or all(_is_check_field(evidence, col) for col in cols):
		reason, unknown = _shape_no_code_reason(shapes, cols, query=query, card=card)
		return _no_code(doctype, cols + list(shapes), reason, unknown=unknown)
	problem = _column_problem(evidence, cols)
	if problem:
		return _no_code(doctype, cols, problem)
	final, dropped, problem = _index_columns(evidence, cols)
	if problem:
		return _no_code(doctype, cols, problem)
	# the existing-index checks look at the FINAL recipe, after columns were left out (C1)
	bare = [c.split("(", 1)[0] for c in final]
	problem, served_by, hedged = _existing_index_problem(
		evidence, bare, equality, explain_row, query, shapes, sort_tail=bool(serves),
	)
	if problem:
		sorts = [col for col, kind in (comparisons or {}).items() if kind in ("sort", "rsort")]
		if serves and not _serves_sort(evidence, served_by, equality, sorts):
			# the index that refused the recipe does not return the rows in the query's order (the
			# cap, the key width or the parser left a sort column out), so it is no sort verdict (item 5)
			served_by = ""
		return replace(_no_code(doctype, cols, problem, unknown=card and hedged), served_by=served_by)
	shape_dropped = [(col, _shape_why(kinds)) for col, kinds in shapes.items()]
	advice = _route(doctype, final, shape_dropped + order_dropped + dropped, evidence, tuple(tracked_apps or ()))
	checks = [col for col in bare if col in equality and _is_check_field(evidence, col)]
	if serves and checks and _has_limit(query) and all(col in checks for col in bare if col in equality):
		# a recipe led by Check fields alone: say why it still helps a sorted LIMIT query
		its = "its" if len(checks) == 1 else "their"
		advice = replace(advice, caveats=(*advice.caveats, (
			f"{_check_rarity(checks)}; this index still returns the query's first rows in order for any of {its} values."
		)))
	return advice


def _serves_sort(evidence: TableEvidence, name: str, equality: set[str], sorts: list[str]) -> bool:
	"""True when the existing index ``name`` returns this query's rows in the order of its
	``sorts`` (the ORDER BY or GROUP BY columns, in order): it starts with equality columns
	(in any order), then every sort column in order; or it is a unique index whose columns
	are all equality columns, which returns at most one row (round 4, item 5)."""
	index = next((ix for ix in evidence.indexes if ix.name == name), None) if name else None
	if index is None or not sorts:
		return False
	if index.unique and set(index.columns) <= equality:
		return True
	k = 0
	while k < len(index.columns) and index.columns[k] in equality:
		k += 1
	return tuple(index.columns[k : k + len(sorts)]) == tuple(sorts)


# How likely a field type is to narrow an equality filter, for the column cap (item 3).
_TYPE_RANK: dict[str, int] = {
	"Link": 0, "Dynamic Link": 0, "Data": 0, "Read Only": 0, "Barcode": 0,
	"Date": 1, "Datetime": 1, "Time": 1, "Int": 2, "Float": 2, "Currency": 2, "Percent": 2, "Select": 3,
	"Check": 9,
}


def _type_rank(evidence: TableEvidence, col: str) -> int:
	"""``_TYPE_RANK`` of the column's field type (lower narrows an equality filter better); 4
	for a type it does not list or a column that is no field."""
	field = evidence.fields.get(col)
	return _TYPE_RANK.get(field.fieldtype, 4) if field is not None else 4


def _cap(cols: list[str], equality: set[str], evidence: TableEvidence | None) -> tuple[list[str], list[str]]:
	"""``(kept, left out)`` for the ``MAX_INDEX_COLUMNS`` cap. A finding's equality block
	wider than the cap keeps, by evidence and never by name (item 3): first the columns of
	an existing index whose columns are all equality columns (the widest such index first),
	then the rest by field type (Link and Data before dates, numbers and Select), Check
	fields last; the kept columns keep their canonical order and the tail goes. Otherwise
	(a card, no evidence, a block that fits) the trailing columns go."""
	if len(cols) <= MAX_INDEX_COLUMNS:
		return cols, []
	block = [col for col in cols if col in equality]
	if evidence is None or len(block) <= MAX_INDEX_COLUMNS:
		return cols[:MAX_INDEX_COLUMNS], cols[MAX_INDEX_COLUMNS:]
	full = [ix for ix in evidence.indexes if ix.columns and set(ix.columns) <= set(block)]
	width = {col: max((len(ix.columns) for ix in full if col in ix.columns), default=0) for col in block}

	def key(col: str) -> tuple:
		return (0 if width[col] else 1, -width[col], _type_rank(evidence, col), col.lower())

	keep = set(sorted(block, key=key)[:MAX_INDEX_COLUMNS])
	return [col for col in block if col in keep], [col for col in cols if col not in keep]


def _is_check_field(evidence: TableEvidence | None, col: str) -> bool:
	field = evidence.fields.get(col) if evidence is not None else None
	return field is not None and field.fieldtype == "Check"


def _equality_count(cols: list[str], comparisons: Mapping[str, str] | None) -> int:
	"""How many leading columns of ``cols`` (in ``_index_order``'s order) form the equality
	block: compared by =, IN or IS NULL. A table card has no query, so every column does."""
	if not comparisons:
		return len(cols)
	k = 0
	while k < len(cols) and comparisons.get(cols[k], "eq") in _EQUALITY_KINDS:
		k += 1
	return k


def _cannot_lead(evidence: TableEvidence | None, col: str) -> bool:
	"""True for a column ``_index_columns`` refuses as the first column: a type no plain
	index covers, a text column on Postgres, or one wider alone than the key limit."""
	if evidence is None:
		return False
	if _unindexable(evidence, col):
		return True
	postgres = evidence.dialect == "postgres"
	if col in evidence.text_columns:
		if postgres:
			return True
		col = f"{col}({TEXT_INDEX_PREFIX})"
	limit = POSTGRES_MAX_INDEX_ROW_BYTES if postgres else MARIADB_MAX_KEY_BYTES
	return _key_bytes(evidence, [col]) > limit


def _canonical(cols: list[str], k: int, evidence: TableEvidence | None) -> list[str]:
	"""``cols`` with its equality block (the first ``k`` columns) in one fixed order, so the
	same filter written in any predicate order gives one recipe and one index name (an
	equality filter uses a leading column set the same way in every order). The order:
	business columns, then Check fields (they never lead a useful index), then creation and
	modified (which never lead, ``apply_metadata_rule``), then columns that cannot lead an
	index (``_cannot_lead``: ``_index_columns`` leaves them out when another column leads,
	so their name never decides between no code and a recipe), each group by name. The
	range or sort columns after the block keep their order."""

	def key(col: str) -> tuple:
		return (_cannot_lead(evidence, col), col.lower() in TRAILING_METADATA_OK, _is_check_field(evidence, col), col.lower())

	return sorted(cols[:k], key=key) + cols[k:]


def _sort_cause(ftype: str, problem: str) -> str:
	"""The clause naming why the sort (Filesort) or the temporary table stays (C7), for the
	``_sort_problem`` code the query has, never a list of causes it may not have. It follows
	"This index narrows the filter, but "."""
	filesort = ftype == "Filesort"
	does, stays = ("sorts", "the sort stays") if filesort else ("groups", "the temporary table stays")
	if problem == "aggregate":
		groups = "" if filesort else "its groups "
		return f"the query sorts {groups}by an aggregate, which no index can return in order, so {stays}."
	if problem == "differs":
		return (
			"the query groups by other columns than it sorts by" if filesort
			else "the query sorts by other columns than it groups by"
		) + f", so {stays}."
	if problem == "distinct":
		return "the temporary table comes from the query's DISTINCT, which this index does not cover, so it stays."
	if problem == "other_table":
		return (
			"the query sorts by a column of another table, which an index on this table cannot return in order, "
			"so the sort stays." if filesort
			else "the query groups by a column of another table, which an index on this table cannot cover, so "
			"the temporary table stays."
		)
	if problem == "none":
		return (
			"the query has no ORDER BY on this table, so the sort comes from elsewhere in the query (a GROUP BY, "
			"a join or a derived table) and may stay." if filesort
			else "the query has no GROUP BY or DISTINCT on this table, so the temporary table comes from elsewhere "
			"in the query (a join, a sort on another table, a UNION or a derived table) and may stay."
		)
	if problem == "alias":
		return f"the query {does} by a select alias, which Optimus cannot match to a column of this table, so {stays}."
	if problem == "not_column":
		return f"the query {does} by a name that is no column of this table, so {stays}."
	if problem == "text":
		return f"the query {does} by a text column, which Optimus does not index for a sort, so {stays}."
	if problem == "unindexable":
		return f"the query {does} by a column a plain index cannot cover (JSON), so {stays}."
	if problem == "directions":
		return (
			f"the query {does} in mixed directions (ASC and DESC), which this index cannot return in order, so {stays}."
		)
	return (
		f"the query {does} by an expression (a function, CASE, arithmetic or a parameter), which no index can "
		f"return in order, so {stays}."
	)


# The sort-problem codes that are about the sort items themselves (``_sort_problem``).
_ITEM_PROBLEMS: frozenset[str] = frozenset({
	"expression", "alias", "not_column", "text", "unindexable", "directions",
})
# A Filesort or Temporary Table lead whose index keeps the sort or the temporary table.
_NARROWS = "This index narrows the filter, but "


def _lead_for(
	ftype: str,
	labelled: list[tuple[str, str]],
	advice: IndexAdvice,
	*,
	sort_problem: str = "",
	ranged: str | None = None,
	multi: tuple[str, ...] = (),
	maybe: tuple[str, ...] = (),
	fixed: frozenset[str] = frozenset(),
) -> tuple[str, bool]:
	"""``(lead, sort stays)``: the finding type's opening sentence, and for a Filesort or
	Temporary Table finding whether the advised index leaves the sort or the temporary table
	in place. The lead never claims the sort or the temporary table goes when the index
	cannot remove it: the sort is not on bare columns (``sort_problem``, see
	``_sort_problem``, which also names the cause), the sort column follows a range condition
	(``ranged``), or a kept filter matches several values (``multi``). Such a lead opens "This
	index narrows the filter, but", never with the Full Table Scan promise that the index
	stops a whole-table read. A kept collapsed ``IN (?)`` (``maybe``) may hold one value or
	many, so the claim that the rows come back in order is hedged, and counts as removing
	the sort. The claim needs every sort or group column in the index, or fixed by an
	equality filter (``fixed``); a metadata column such as parent is never indexed (R2)."""
	if advice.route == ROUTE_NO_CODE:
		return "", False
	kept = {c.split("(", 1)[0] for c in advice.columns}
	labels = {label for label, col in labelled if col in kept}
	sort_label = {"Filesort": "ORDER BY", "Temporary Table": "GROUP BY"}.get(ftype)
	if not sort_label:
		return _TYPE_LEADS.get(ftype, ""), False
	stays = "the sort stays" if ftype == "Filesort" else "the temporary table stays"
	column = "sort" if ftype == "Filesort" else "grouping"
	expression = _NARROWS + _sort_cause(ftype, sort_problem)
	if sort_label in labels:
		kept_multi = [col for col in multi if col in kept]
		if sort_problem:
			return expression, True
		if kept_multi:
			heading, still = (
				("sort", "sorts the rows") if ftype == "Filesort" else ("GROUP BY", "groups them in a temporary table")
			)
			return (
				f"Index the filter columns followed by the {heading} column so fewer rows are read. The filter "
				f"on {kept_multi[0]} matches more than one value, so the database still {still}."
			), True
		missing = [col for label, col in labelled if label == sort_label and col not in kept and col not in fixed]
		if missing:
			return _NARROWS + f"it does not cover every {column} column ({', '.join(missing)}), so {stays}.", True
		kept_maybe = [col for col in maybe if col in kept]
		if kept_maybe:
			return _TYPE_LEADS[ftype][:-1] + f"; if the IN list on {kept_maybe[0]} has more than one value, {stays}.", False
		return _TYPE_LEADS[ftype], False
	sort_cols = [col for label, col in labelled if label == sort_label]
	# a sort column the parser could name that is no Frappe metadata column
	if any(col.lower() not in FRAPPE_METADATA_COLUMNS for col in sort_cols):
		if sort_problem:
			return expression, True
		kept_multi = [col for col in multi if col in kept]
		if kept_multi:
			return _NARROWS + (
				f"the filter on {kept_multi[0]} matches more than one value, so it cannot return the rows in order and "
				f"{stays}."
			), True
		if ranged:
			return _NARROWS + (
				f"the {column} column comes after the range condition on {ranged}, so it cannot return the rows in "
				f"order and {stays}."
			), True
	# the sort or group column is not in the index: name why (C7)
	if sort_problem in ("aggregate", "distinct", "other_table", "none"):
		return expression, True
	metadata = [col for label, col in labelled if label == sort_label and col.lower() in FRAPPE_METADATA_COLUMNS]
	trailing = [col for col in metadata if col.lower() in TRAILING_METADATA_OK]
	if trailing:
		return _NARROWS + (
			f"the {column} column {trailing[0]} can only follow an equality filter column in an index, and this "
			f"query has none, so {stays}."
		), True
	if metadata:
		return _NARROWS + (
			f"the {column} column is a Frappe metadata column, which Optimus never indexes, so {stays}."
		), True
	if sort_problem in _ITEM_PROBLEMS | {"differs"}:
		return expression, True
	return _NARROWS + f"it does not cover the {column}, so {stays}.", True


def advise_finding(
	finding: dict,
	*,
	evidence_lookup: Callable[[str], TableEvidence | None],
	tracked_apps: tuple[str, ...] = (),
	parser: Callable[[str], dict] | None = None,
) -> IndexAdvice | None:
	"""The advice for an index-family or Slow Query finding (render dict or row-shaped
	dict), or None when there is nothing to advise. A table whose evidence read raised
	says so (``_with_read_failure``), not that it has no DocType."""
	return _with_read_failure(
		_advise_finding(finding, evidence_lookup=evidence_lookup, tracked_apps=tracked_apps, parser=parser),
		evidence_lookup,
	)


def _advise_finding(
	finding: dict,
	*,
	evidence_lookup: Callable[[str], TableEvidence | None],
	tracked_apps: tuple[str, ...] = (),
	parser: Callable[[str], dict] | None = None,
) -> IndexAdvice | None:
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
	maybe: tuple[str, ...] = ()
	if query:
		# top_queries keeps QUERY_TEXT_LIMIT characters, so a Slow Query that long may be cut
		truncated = ftype == "Slow Query" and len(query) >= QUERY_TEXT_LIMIT
		qualifiers = _target_qualifiers(query, table, getattr(parser, "aliases", None))
		if evidence is not None and _union_branches(query, qualifiers) > 1:
			# one branch's filter is not the query's: never advise from one branch (E7)
			return _no_code(doctype, _clean_columns([col for _label, col in labelled]), (
				f'Optimus could not read how this query filters: it is a UNION that filters "tab{doctype}" in '
				"more than one branch, and one index may not serve them all. So it gives no index code. Check "
				"each branch with EXPLAIN."
			), unknown=True)
		source = _from_clause(query) or []
		target = table.strip().strip("`").lower()
		probes = _join_probes(query, qualifiers, target)
		if probes:
			# a column the table only feeds into a LEFT JOIN that the WHERE leaves a LEFT JOIN is
			# a probe value, never compared with a known value, so an index on it cannot narrow
			# this table (M2, item 4)
			where = {col.lower() for label, col in labelled if label == "WHERE"}
			labelled = [
				(label, col) for label, col in labelled
				if label != "JOIN" or col.lower() not in probes or col.lower() in where
			]
		dropped: list[str] = []
		names = {col.lower(): col for col in evidence.column_types} if evidence is not None else {}
		if evidence is not None and target in source:
			# sql_metadata leaves out a bare column named like an SQL word (account, user, type,
			# date, ...) and every unqualified column of a query on several tables (a subquery
			# counts). The scan reads the main WHERE, so only a table of the main FROM is checked,
			# never one that appears only in a subquery (M4).
			parsed = {col.lower() for label, col in labelled if label in ("WHERE", "JOIN")}
			dropped = sorted(
				names[name] for name in _where_columns(query, qualifiers, names, truncated=truncated)
				if name not in parsed and name not in FRAPPE_METADATA_COLUMNS
			)
		if dropped and source == [target]:
			# the only table of the main FROM owns every unqualified column: advise them
			labelled = [*labelled, *(("WHERE", col) for col in dropped)]
		elif dropped:
			# another table could own them; say so only when the filter shape would let an
			# index use one of them (M5), otherwise its shape keeps it out of the recipe anyway
			usable = _scan_where(query, [("WHERE", col) for col in dropped], qualifiers, truncated=truncated)[1]
			dropped = [col for col in dropped if col in usable]
			if dropped:
				return _no_code(doctype, _clean_columns([col for _label, col in labelled] + dropped), (
					f"Optimus could not read the filter on {', '.join(dropped)}: the SQL parser it uses did not "
					"report those columns (it leaves out some unquoted names such as account, user, type and "
					"date, and an unqualified column of a query on several tables), so advice built without them "
					"would be partial. So it gives no index code. Check the query with EXPLAIN to see which index "
					"it needs."
				), unknown=True)
		sort_label = _SORT_LABELS.get(ftype)
		if sort_label and evidence is not None and source == [target]:
			# the only table also owns its outer ORDER BY or GROUP BY, which Frappe's query builder
			# leaves unqualified next to a WHERE subquery and the parser drops when named like an SQL
			# word (round 4, item 6); an ORDER BY column the query aggregates is left out, as the
			# parser's own are (P9b). The sort columns then follow the clause's order, after every
			# filter, so a range-filtered sort column still reads as one
			parsed_sorts = {col.lower(): col for label, col in labelled if label == sort_label}
			clause = [
				col for col in _clause_columns(query, sort_label.split()[0].lower(), qualifiers, names)
				if not (sort_label == "ORDER BY" and _aggregated(query, col))
			]
			if any(col.lower() not in parsed_sorts for col in clause):
				in_clause = {col.lower() for col in clause}
				labelled = [
					*((label, col) for label, col in labelled if label != sort_label),
					*((sort_label, parsed_sorts.get(col.lower(), col)) for col in clause),
					*((label, col) for label, col in labelled if label == sort_label and col.lower() not in in_clause),
				]
		unusable, usable, valued = _scan_where(query, labelled, qualifiers, truncated=truncated)
		comparisons = {}
		for label, col in labelled:  # filters come first, then the sort or group columns
			if label in ("ORDER BY", "GROUP BY"):
				current = comparisons.get(col)
				if current != "eq":  # an equality-filtered sort column stays an equality column
					comparisons[col] = "rsort" if current == "range" else "sort"
			else:
				comparisons.setdefault(col, "eq" if label == "JOIN" else usable.get(col, "range"))
		# a self-join reads the table twice, and a key compared with a value pins only the
		# reference that names it (p.name = ? finds p's row, never a's rows)
		lookup = "" if source.count(target) > 1 else _key_lookup(valued, evidence)
		if lookup:
			return _no_code(doctype, _clean_columns([col for _label, col in labelled]), lookup)
		# only an IN column that can stay in the index's equality block counts (item 2b): a
		# metadata column never does, and an unusable one is left out anyway
		in_cols = [
			(col, kind) for col, kind in comparisons.items()
			if kind in ("in", "in1") and col.lower() not in FRAPPE_METADATA_COLUMNS and col not in unusable
		]
		multi = tuple(col for col, kind in in_cols if kind == "in")
		maybe = tuple(col for col, kind in in_cols if kind == "in1")
		if ftype in _SERVES:
			sort_problem = _sort_problem(query, ftype, qualifiers, evidence)
			# rows matching several values (IN, IS NULL OR =) never come back in one sorted run;
			# a collapsed IN (?) may be one value, so it keeps the sort and the lead hedges (item 2a)
			serves = _SERVES[ftype] if not sort_problem and not multi else ""
	advice = advise(
		table, [col for _label, col in labelled], evidence=evidence,
		tracked_apps=tracked_apps, explain_row=detail.get("explain_row"), query=query, unusable=unusable,
		comparisons=comparisons, serves=serves, removes=_SERVES.get(ftype, ""),
	)
	if advice is None:
		return None
	if ftype == "Filesort" and advice.route != ROUTE_NO_CODE and comparisons and not _has_limit(query):
		bare = [c.split("(", 1)[0] for c in advice.columns]
		if all(
			comparisons.get(col) == "sort" or (comparisons.get(col) == "rsort" and _only_not_null(query, qualifiers, col))
			for col in bare
		):
			# an index of sort columns only, for a query with no LIMIT and no filter it narrows (R3)
			db = "Postgres" if evidence is not None and evidence.dialect == "postgres" else "MariaDB"
			return _no_code(doctype, bare, (
				f"An index on {_cols_text(bare)} would hold only the query's sort "
				f"{'column' if len(bare) == 1 else 'columns'}, and the query has no LIMIT and no filter that index "
				f"could narrow. {db} rarely walks a whole index instead of sorting when the query has no LIMIT; add a "
				"LIMIT or a narrowing filter first. So Optimus gives no index code."
			))
	ranged = next((col for col, kind in (comparisons or {}).items() if kind == "range"), None)
	fixed = frozenset(col for col, kind in (comparisons or {}).items() if kind == "eq")
	lead, sort_stays = _lead_for(
		ftype, labelled, advice, sort_problem=sort_problem, ranged=ranged, multi=multi, maybe=maybe, fixed=fixed,
	)
	return replace(advice, lead=lead, sort_stays=sort_stays)


def advise_table(
	table: str, columns, *, evidence_lookup: Callable[[str], TableEvidence | None], tracked_apps: tuple[str, ...] = (),
) -> IndexAdvice | None:
	"""The advice for a table card's recommended columns."""
	doctype = doctype_of(table)
	if doctype is None:
		return None
	advice = advise(
		table, list(columns or []), evidence=evidence_lookup(f"tab{doctype}"), tracked_apps=tracked_apps, card=True,
	)
	return _with_read_failure(advice, evidence_lookup)


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
