# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Deterministic fix recipes and AI gates (no site access or I/O).

- ``index_recipe``: the durable Frappe way to add the index an index-family
  finding asks for, chosen from the DocType's ownership (own app, another
  app, Custom Field, custom DocType). Never raw DDL, never Customize Form.
- ``table_card_columns``: the columns the per-table card may print in its
  ``frappe.db.add_index`` snippet, or None when that snippet would not
  survive ``bench migrate``.
- ``hot_line_gate``: a note explaining why a Hot Line gets no AI call.
- ``loop_facts`` / ``format_loop_facts``: AST facts about the loop around a
  callsite, injected into the AI prompt for loop-shaped findings.

Frappe facts relied on (v16.18): schema sync drops a single-column index the
DocField does not declare (frappe/database/schema.py:311), builds the table
from the meta with Property Setters applied (schema.py:26, model/meta.py:176),
never drops an index spanning several columns (mariadb/database.py:400-411)
and never adds or drops an index on a text/longtext column (schema.py:274,
311); DocType validation refuses Search Index on text fields
(core/doctype/doctype/doctype.py:1467); ``frappe.db.add_index`` skips an
existing index (has_index) and on Postgres strips a ``(255)`` prefix
(postgres/database.py:400). Prefix precedent: email_queue.py:537.
"""

from __future__ import annotations

import ast
import json
import re
import textwrap
from collections.abc import Callable

from optimus.analyzers.base import (
	FRAMEWORK_APPS,
	FRAPPE_METADATA_COLUMNS,
	is_framework_callsite,
	is_write_hot_table,
)
from optimus.safe_call import best_effort

INDEX_FINDING_TYPES: frozenset[str] = frozenset({
	"Missing Index",
	"Full Table Scan",
	"Filesort",
	"Temporary Table",
	"Low Filter Ratio",
})

# Frappe fieldtypes stored as text/longtext: Search Index is refused on them
# and schema sync never manages their indexes, so they need add_index("c(255)").
TEXT_FIELDTYPES: frozenset[str] = frozenset({
	"Small Text", "Text", "Long Text", "Code", "Text Editor", "Markdown Editor", "HTML Editor",
})
TEXT_INDEX_PREFIX = 255
MAX_INDEX_COLUMNS = 4
TRAILING_METADATA_OK: frozenset[str] = frozenset({"creation", "modified"})

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TAB_TABLE_RE = re.compile(r"^tab[A-Za-z0-9 _\-]+$")
_DDL_PREFIX_COL_RE = re.compile(r"`(?P<col>[A-Za-z_][A-Za-z0-9_]*)`\(\d+\)")

_CLAUSES_BY_TYPE: dict[str, tuple[tuple[str, ...], ...]] = {
	"Full Table Scan": (("WHERE", "JOIN"),),
	"Low Filter Ratio": (("WHERE", "JOIN"),),
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


# ---------------------------------------------------------------------------
# Index recipes
# ---------------------------------------------------------------------------


def apply_metadata_rule(columns: list[str]) -> list[str]:
	"""Drop Frappe metadata columns from an index column list, except a
	trailing run of ``creation`` / ``modified`` after at least one business
	column. A metadata column is never kept alone or first."""
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


def is_own_app(
	app: str,
	*,
	tracked_apps: tuple[str, ...] = (),
	installed_apps: frozenset[str] | None = None,
) -> bool:
	"""True when ``app`` is the developer's own app, by the rule the report
	already uses for "your code vs framework" (analyzers.base.is_framework_callsite):
	Tracked Apps is an allowlist when set; otherwise any installed app outside
	FRAMEWORK_APPS is the developer's."""
	if not app:
		return False
	if tracked_apps:
		return app in tracked_apps
	if app in FRAMEWORK_APPS:
		return False
	if installed_apps is not None:
		return app in installed_apps
	return True


def _detail(finding: dict) -> dict:
	detail = finding.get("technical_detail")
	if isinstance(detail, dict):
		return detail
	try:
		parsed = json.loads(finding.get("technical_detail_json") or "{}")
	except (TypeError, ValueError):
		return {}
	return parsed if isinstance(parsed, dict) else {}


def _doctype_of(table: str) -> str | None:
	name = str(table or "").strip().strip("`")
	if not _TAB_TABLE_RE.match(name):
		return None
	return name[3:]


def _safe_meta(meta_lookup: Callable[[str], dict | None], doctype: str) -> dict | None:
	meta = best_effort(lambda: meta_lookup(doctype), None)
	return meta if isinstance(meta, dict) else None


def _explain_columns(ftype: str, table: str, query: str) -> tuple[str, list[str]]:
	"""(real table name, ordered columns) for an EXPLAIN-flag finding, parsed
	from its normalized query with the table-breakdown analyzer's parser."""
	if not query:
		return table, []
	from optimus.analyzers.table_breakdown import _parse_query

	parsed = best_effort(lambda: _parse_query(query), {})
	by_table = parsed.get("index_cols") or {}
	key = table if table in by_table else next(
		(t for t in by_table if t.lower() == str(table).lower()), None,
	)
	if key is None and not str(table).startswith("tab"):
		tab_tables = [t for t in (parsed.get("tables") or []) if str(t).startswith("tab")]
		if len(tab_tables) == 1:
			key = tab_tables[0]
	if key is None:
		return table, []
	pairs = by_table.get(key) or []
	cols: list[str] = []
	for labels in _CLAUSES_BY_TYPE.get(ftype, (("WHERE", "JOIN"),)):
		for label, col in pairs:
			if label in labels and col not in cols:
				cols.append(col)
	return key, cols


def _index_target(ftype: str, detail: dict) -> tuple[str, list[str], set[str]]:
	"""(table, raw columns, columns the analyzer's DDL gave a prefix)."""
	table = str(detail.get("table") or "").strip().strip("`")
	if ftype == "Missing Index":
		col = str(detail.get("column") or "").strip().strip("`")
		ddl = str(detail.get("suggested_ddl") or "")
		prefixed = {m.group("col") for m in _DDL_PREFIX_COL_RE.finditer(ddl)}
		return table, ([col] if col else []), prefixed
	table, cols = _explain_columns(ftype, table, str(detail.get("normalized_query") or ""))
	return table, cols, set()


def _finalize_columns(columns: list[str], meta: dict | None, prefixed: set[str]) -> list[str]:
	"""Metadata rule, identifier check, ``col(255)`` for text columns (at most
	one per index, which keeps the key under MariaDB's 3072-byte limit), cap."""
	fields = (meta or {}).get("fields") or {}
	out: list[str] = []
	seen: set[str] = set()
	text_used = False
	for col in apply_metadata_rule([c for c in columns if isinstance(c, str) and _IDENT_RE.fullmatch(c)]):
		if col.lower() in seen:
			continue
		seen.add(col.lower())
		fieldtype = (fields.get(col) or {}).get("fieldtype") or ""
		is_text = fieldtype in TEXT_FIELDTYPES or (not fieldtype and col in prefixed)
		if is_text:
			if text_used:
				continue
			text_used = True
			out.append(f"{col}({TEXT_INDEX_PREFIX})")
		else:
			out.append(col)
		if len(out) == MAX_INDEX_COLUMNS:
			break
	return out


def _base(col: str) -> str:
	return col.split("(", 1)[0]


def _is_prefixed(col: str) -> bool:
	return col.endswith(f"({TEXT_INDEX_PREFIX})")


def _cols_literal(columns: list[str]) -> str:
	return ", ".join(json.dumps(c) for c in columns)


def _patch_code(doctype: str, columns: list[str]) -> str:
	return (
		"import frappe\n\n\n"
		"def execute():\n"
		f"\tfrappe.db.add_index({json.dumps(doctype)}, [{_cols_literal(columns)}])\n"
	)


def _hook_code(doctype: str, columns: list[str]) -> str:
	return (
		"import frappe\n\n\n"
		"def on_doctype_update():\n"
		f"\tfrappe.db.add_index({json.dumps(doctype)}, [{_cols_literal(columns)}])\n"
	)


def _property_setter_code(doctype: str, field: str) -> str:
	return (
		"import frappe\n"
		"from frappe.custom.doctype.property_setter.property_setter import make_property_setter\n\n\n"
		"def execute():\n"
		f"\tmake_property_setter({json.dumps(doctype)}, {json.dumps(field)}, "
		"\"search_index\", \"1\", \"Check\", for_doctype=False)\n"
		f"\tfrappe.db.add_index({json.dumps(doctype)}, [{json.dumps(field)}])\n"
	)


def _patch_text(columns: list[str]) -> str:
	why = (
		"bench migrate never drops an index on a text column"
		if len(columns) == 1
		else "bench migrate never drops an index that spans several columns"
	)
	return (
		"Add the index from a patch in your own app (list it in patches.txt). "
		f"frappe.db.add_index skips an index that already exists, and {why}."
	)


def _choose_row(doctype: str, columns: list[str], meta: dict | None) -> tuple[str, str | None]:
	single = len(columns) == 1 and not _is_prefixed(columns[0])
	field = _base(columns[0])
	if meta is None:
		if single:
			return (
				f'Optimus could not read DocType "{doctype}" while building this report. '
				f'If the DocType is in your app, tick "Search Index" on its "{field}" field in the '
				"DocType editor. If it belongs to another app, ship a Property Setter that sets "
				f'search_index to 1 on "{field}" from your app, then create the index once with '
				"frappe.db.add_index in a patch.",
				None,
			)
		return (
			f'Optimus could not read DocType "{doctype}" while building this report. '
			+ _patch_text(columns)
			+ " If the DocType is in your app, on_doctype_update in its module works too.",
			_patch_code(doctype, columns),
		)
	fields = meta.get("fields") or {}
	app = meta.get("module_app") or ""
	has_custom_field = any((fields.get(_base(c)) or {}).get("is_custom_field") for c in columns)
	own = bool(meta.get("is_own_app")) and not meta.get("is_custom_doctype")
	if single:
		if has_custom_field:
			return (
				f'The "{field}" field of "{doctype}" is a Custom Field. Open that Custom Field, '
				'tick "Search Index" and save: Frappe updates the table when a Custom Field is '
				"saved. If your app ships the Custom Field as a fixture, set search_index to 1 "
				"in the fixture as well.",
				None,
			)
		if meta.get("is_custom_doctype"):
			return (
				f'DocType "{doctype}" was created in the UI. Open it, tick "Search Index" on the '
				f'"{field}" field and save: Frappe updates the table when the DocType is saved.',
				None,
			)
		if own:
			return (
				f'Tick "Search Index" on the "{field}" field of DocType "{doctype}" in the DocType '
				f'editor of your app "{app}" (developer mode), save, and commit the DocType JSON. '
				"bench migrate then adds the index on every site and keeps it.",
				None,
			)
		return (
			f'DocType "{doctype}" belongs to the "{app}" app, so do not edit it. From your own app, '
			f'set search_index to 1 on its "{field}" field with a Property Setter, shipped as a '
			"fixture or created in a patch as shown, so bench migrate keeps the index. The patch "
			"also creates the index once.",
			_property_setter_code(doctype, field),
		)
	if own and not has_custom_field:
		return (
			f'Add this function to the Python module of DocType "{doctype}" in your app "{app}". '
			"Frappe runs on_doctype_update when bench migrate syncs the DocType, which happens "
			"only after its JSON changes, so also call the same frappe.db.add_index once from a "
			"patch to add the index on sites that already have the DocType. frappe.db.add_index "
			"skips an index that already exists.",
			_hook_code(doctype, columns),
		)
	if meta.get("is_custom_doctype"):
		lead = f'DocType "{doctype}" was created in the UI and has no module file.'
	elif has_custom_field:
		lead = "This index includes a Custom Field."
	else:
		lead = f'DocType "{doctype}" belongs to the "{app}" app, so do not edit it.'
	return lead + " " + _patch_text(columns), _patch_code(doctype, columns)


def _write_hot_note(table: str) -> str:
	if not is_write_hot_table(table):
		return ""
	return (
		f'Note: "{table}" takes many writes for every submitted document in production, and '
		"each index slows those writes, so add it only if this read is slow in production too."
	)


def index_recipe(finding: dict, *, meta_lookup: Callable[[str], dict | None]) -> dict | None:
	"""The durable Frappe recipe for an index-family finding, or None when
	there is nothing safe to suggest (not a DocType table, no usable column).

	Returns ``{"kind": "index", "text": str, "code": str | None}``: ``text`` is
	plain prose for the finding's fix-hint slot, ``code`` is Python for the
	code slot. ``meta_lookup(doctype)`` returns ``{"module_app", "is_own_app",
	"is_custom_doctype", "fields": {fieldname: {"fieldtype", "is_custom_field"}}}``
	or None when the DocType cannot be read.
	"""
	ftype = finding.get("finding_type") or ""
	if ftype not in INDEX_FINDING_TYPES:
		return None
	table, raw_columns, prefixed = _index_target(ftype, _detail(finding))
	doctype = _doctype_of(table)
	if doctype is None:
		return None
	meta = _safe_meta(meta_lookup, doctype)
	columns = _finalize_columns(raw_columns, meta, prefixed)
	if not columns:
		return None
	text, code = _choose_row(doctype, columns, meta)
	parts = [_TYPE_LEADS.get(ftype, ""), text, _write_hot_note(table)]
	return {"kind": "index", "text": " ".join(p for p in parts if p), "code": code}


def table_card_columns(
	table: str, columns: list[str], *, meta_lookup: Callable[[str], dict | None],
) -> list[str] | None:
	"""Columns for the per-table card's ``frappe.db.add_index`` snippet (which
	the report tells the reader to put in a patch), or None when no such
	snippet is durable: a single non-text column added from a patch is dropped
	by the next schema sync of that DocType."""
	doctype = _doctype_of(table)
	if doctype is None:
		return None
	cols = _finalize_columns(list(columns or []), _safe_meta(meta_lookup, doctype), set())
	if not cols or (len(cols) == 1 and not _is_prefixed(cols[0])):
		return None
	return cols


# ---------------------------------------------------------------------------
# Hot Line gate
# ---------------------------------------------------------------------------

_CHEAP_CALLS: frozenset[str] = frozenset({
	# Python builtins that cost nothing worth profiling on their own
	"abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "float", "format",
	"frozenset", "getattr", "hasattr", "hash", "int", "isinstance", "issubclass", "iter", "len",
	"list", "map", "max", "min", "next", "print", "range", "repr", "reversed", "round", "set",
	"setattr", "slice", "sorted", "str", "sum", "tuple", "type", "zip",
	# frappe.utils converters, frappe._ and frappe._dict
	"_", "_dict", "cint", "cstr", "flt", "sbool", "getdate", "get_datetime", "nowdate", "now",
	"today", "add_days", "add_months", "date_diff", "rounded",
	# str / list / dict / set methods
	"append", "extend", "get", "items", "keys", "values", "strip", "lstrip", "rstrip", "split",
	"join", "lower", "upper", "startswith", "endswith", "replace", "add", "update", "pop",
	"setdefault", "copy", "count", "index", "insert", "remove", "sort", "discard",
})
_NO_CALL_OPENERS = re.compile(r"^(else|finally|try|except)\b")


def _strip_comment(line: str) -> str:
	in_str: str | None = None
	for i, ch in enumerate(line):
		if in_str:
			if ch == in_str and line[i - 1 : i] != "\\":
				in_str = None
			continue
		if ch in ("'", '"'):
			in_str = ch
		elif ch == "#":
			return line[:i]
	return line


def _call_name(func: ast.AST) -> str | None:
	"""Render a call target: ``frappe.db.get_value``, ``self.run``,
	``super().validate``. None for shapes that are not a name chain."""
	parts: list[str] = []
	node = func
	while isinstance(node, ast.Attribute):
		parts.append(node.attr)
		node = node.value
	if isinstance(node, ast.Name):
		parts.append(node.id)
	elif isinstance(node, ast.Call):
		inner = _call_name(node.func)
		if not inner:
			return None
		parts.append(inner + "()")
	else:
		return None
	return ".".join(reversed(parts))


def _parse_line(line: str) -> ast.AST | None:
	src = _strip_comment(line).strip()
	if not src or _NO_CALL_OPENERS.match(src):
		return None
	if src.startswith("elif "):
		src = src[2:]
	candidates = [src] + ([src + " pass"] if src.endswith(":") else [])
	for candidate in candidates:
		try:
			return ast.parse(candidate)
		except SyntaxError:
			continue
	return None


def _first_callee(line: str) -> str | None:
	"""The outermost call on ``line`` that is not a cheap builtin, converter or
	container method, or None."""
	tree = _parse_line(line)
	if tree is None:
		return None
	for node in ast.walk(tree):
		if isinstance(node, ast.Call):
			name = _call_name(node.func)
			if name and name.rsplit(".", 1)[-1] not in _CHEAP_CALLS:
				return name
	return None


def _short_path(filename: str) -> str:
	norm = filename.replace("\\", "/")
	if "/apps/" in norm:
		return norm.rsplit("/apps/", 1)[1]
	if norm.startswith("apps/"):
		return norm[len("apps/"):]
	return norm.rsplit("/", 1)[-1]


def hot_line_gate(
	finding: dict,
	*,
	tracked_apps: tuple[str, ...] = (),
	installed_apps: frozenset[str] | None = None,
) -> str | None:
	"""Why a Hot Line must not go to the AI, as a report-ready note, or None.

	Gated when the line sits in framework or library code (the report's own
	classification, ``is_framework_callsite``) or when its time is spent inside
	a function it calls (Phase 1 named the hot callee, or the line calls
	something other than a cheap builtin, converter or container method)."""
	if (finding.get("finding_type") or "") != "Hot Line":
		return None
	detail = _detail(finding)
	callsite = detail.get("callsite") if isinstance(detail.get("callsite"), dict) else {}
	filename = str(callsite.get("filename") or detail.get("file") or "")
	if filename and is_framework_callsite(filename, tracked_apps or None, installed_apps):
		return (
			f"This line is in framework or library code ({_short_path(filename)}), which your app "
			"cannot change, so Optimus does not ask the AI about it. Follow the call chain above to "
			"the first line in your own app and reduce how often that path runs."
		)
	hint = detail.get("phase1_hint") if isinstance(detail.get("phase1_hint"), dict) else {}
	callee = str(hint.get("next_hot_callee") or "") or _first_callee(str(detail.get("line_content") or ""))
	if callee:
		return (
			f"Most of this line's time is spent inside {callee}, which the line calls, so Optimus "
			"does not ask the AI about the line itself. Re-run Phase 2 with that function picked to "
			"see which of its lines is slow, or reduce how often this line calls it."
		)
	return None


# ---------------------------------------------------------------------------
# Loop facts for the AI prompt
# ---------------------------------------------------------------------------

_HEADER_RE = re.compile(
	r"^(?P<kw>async\s+for|for|while|if|elif|else|try|except|finally|with|async\s+with|match|case"
	r"|async\s+def|def|class)\b"
)
_DB_CALL_SUFFIXES: tuple[str, ...] = (
	"db.sql", "db.get_value", "db.get_values", "db.exists", "db.count", "db.get_single_value",
	"get_all", "get_list", "get_doc", "get_cached_doc", "get_cached_value", "has_permission",
	"cache.get_value", "db.set_value",
)
_WRITE_ATTRS: frozenset[str] = frozenset({
	"save", "insert", "submit", "cancel", "db_set", "delete", "set_value", "delete_doc",
	"db_insert", "db_update", "commit", "rollback", "bulk_update", "set_single_value",
})
_READ_SQL_VERBS: frozenset[str] = frozenset({"SELECT", "SHOW", "WITH", "EXPLAIN", "DESC", "DESCRIBE"})
_SQL_VERB_RE = re.compile(r"^\s*([A-Za-z]{1,12})\b")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z_][\w.()]*$")
_MAX_STMT_LINES = 15


def _indent(line: str) -> int:
	return len(line) - len(line.lstrip(" \t"))


def _try_parse(lines: list[str]) -> ast.Module | None:
	try:
		return ast.parse(textwrap.dedent("\n".join(lines)))
	except (SyntaxError, ValueError):
		return None


def _with_body(block: list[str]) -> list[str]:
	"""Give a trailing block header (``for x in y:``) a ``pass`` body."""
	last = block[-1]
	if not _strip_comment(last).rstrip().endswith(":"):
		return block
	ws = last[: _indent(last)]
	return block + [ws + ("    " if ws.startswith(" ") else "\t") + "pass"]


def _statement_span(lines: list[str], t: int) -> tuple[int, int] | None:
	"""(start, end) 0-based indices of the smallest parseable statement that
	contains line ``t``; the target line itself is tried as the start first."""
	n = len(lines)
	for s in range(t, max(-1, t - _MAX_STMT_LINES), -1):
		for e in range(t, min(n, t + _MAX_STMT_LINES)):
			if _try_parse(_with_body(lines[s : e + 1])) is not None:
				return s, e
	return None


def _names(node: ast.AST) -> set[str]:
	return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _assigned(node: ast.AST) -> set[str]:
	out: set[str] = set()
	for n in ast.walk(node):
		targets: list[ast.AST] = []
		if isinstance(n, ast.Assign):
			targets = list(n.targets)
		elif isinstance(n, (ast.AugAssign, ast.AnnAssign, ast.For, ast.AsyncFor, ast.comprehension)):
			targets = [n.target]
		elif isinstance(n, ast.withitem) and n.optional_vars is not None:
			targets = [n.optional_vars]
		elif isinstance(n, ast.NamedExpr):
			targets = [n.target]
		for tgt in targets:
			out |= _names(tgt)
	return out


def _loop_writes(node: ast.AST) -> list[str]:
	hits: set[str] = set()
	for n in ast.walk(node):
		if not isinstance(n, ast.Call):
			continue
		name = _call_name(n.func) or ""
		last = name.rsplit(".", 1)[-1]
		if last in _WRITE_ATTRS and not (last == "insert" and len(n.args) >= 2):
			hits.add(name)
		if name.endswith("db.sql") and n.args and isinstance(n.args[0], ast.Constant):
			m = _SQL_VERB_RE.match(str(n.args[0].value))
			if m and m.group(1).upper() not in _READ_SQL_VERBS:
				hits.add(f"frappe.db.sql({m.group(1).upper()})")
	return sorted(h for h in hits if _SAFE_NAME_RE.match(h))[:6]


def _pick_call(exprs: list[ast.AST], rel_line: int) -> ast.Call | None:
	calls = [
		n for e in exprs for n in ast.walk(e)
		if isinstance(n, ast.Call) and n.lineno <= rel_line <= (n.end_lineno or n.lineno)
	]
	for c in calls:
		name = _call_name(c.func) or ""
		if any(name == sfx or name.endswith("." + sfx) for sfx in _DB_CALL_SUFFIXES):
			return c
	return calls[0] if calls else None


def _safe_call_name(call: ast.Call) -> str | None:
	name = _call_name(call.func)
	return name if name and _SAFE_NAME_RE.match(name) else None


def _arg_names(call: ast.Call) -> set[str]:
	# The receiver can change each iteration even when the argument list is empty.
	used = _names(call.func)
	for a in list(call.args) + [k.value for k in call.keywords]:
		used |= _names(a)
	return used


def _stmt_exprs(stmt: ast.stmt) -> list[ast.AST]:
	if isinstance(stmt, (ast.If, ast.While)):
		return [stmt.test]
	if isinstance(stmt, (ast.For, ast.AsyncFor)):
		return [stmt.iter]
	if isinstance(stmt, (ast.With, ast.AsyncWith)):
		return [i.context_expr for i in stmt.items]
	return [stmt]


def _loop_facts_for(loop: ast.AST, kind: str, loop_line: int, stmt: ast.stmt, rel_line: int) -> dict:
	variant = _assigned(loop)
	if isinstance(loop, (ast.For, ast.AsyncFor)):
		variant |= _names(loop.target)
	call = _pick_call(_stmt_exprs(stmt), rel_line)
	return {
		"in_loop": True,
		"loop_kind": kind,
		"loop_line": loop_line,
		"call": _safe_call_name(call) if call is not None else None,
		"depends_on_loop_vars": sorted(_arg_names(call) & variant) if call is not None else [],
		"loop_writes": _loop_writes(loop),
		"result_used": (
			None if call is None else not (isinstance(stmt, ast.Expr) and stmt.value is call)
		),
	}


def _innermost_stmt(root: ast.AST, rel_line: int) -> ast.stmt | None:
	best = None
	for n in ast.walk(root):
		if isinstance(n, ast.stmt) and n.lineno <= rel_line <= (n.end_lineno or n.lineno):
			if best is None or n.lineno >= best.lineno:
				best = n
	return best


def _comprehension_facts(lines: list[str], s: int, e: int, t: int) -> dict | None:
	tree = _try_parse(_with_body(lines[s : e + 1]))
	if tree is None or not tree.body:
		return None
	rel = t - s + 1
	for n in ast.walk(tree.body[0]):
		if not isinstance(n, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
			continue
		element = [n.key, n.value] if isinstance(n, ast.DictComp) else [n.elt]
		for i, generator in enumerate(n.generators):
			element.extend(generator.ifs)
			if i:
				element.append(generator.iter)
		call = _pick_call(element, rel)
		if call is None:
			continue
		targets: set[str] = set()
		for gen in n.generators:
			targets |= _names(gen.target)
		return {
			"in_loop": True,
			"loop_kind": "comprehension",
			"loop_line": s + 1,
			"call": _safe_call_name(call),
			"depends_on_loop_vars": sorted(_arg_names(call) & targets),
			"loop_writes": _loop_writes(n),
			"result_used": True,
		}
	return None


def loop_facts(source_lines: list[str], target_lineno: int) -> dict:
	"""AST facts about the loop around ``source_lines[target_lineno - 1]``.

	``source_lines`` is a contiguous code window (it may start and end mid
	function); ``target_lineno`` is 1-based within it. Returns ``{}`` when the
	target cannot be analysed (out of range, blank, unparseable, or the
	enclosing header is outside the window); ``{"in_loop": False}`` when the
	enclosing function has no loop around it; otherwise ``{"in_loop": True,
	"loop_kind": "for" | "while" | "comprehension", "loop_line": int (1-based
	in source_lines), "call": str | None, "depends_on_loop_vars": [str],
	"loop_writes": [str], "result_used": bool | None}``.
	"""
	lines = [str(x) for x in (source_lines or [])]
	n = len(lines)
	if not (1 <= target_lineno <= n):
		return {}
	t = target_lineno - 1
	if not _strip_comment(lines[t]).strip():
		return {}
	span = _statement_span(lines, t)
	if span is None:
		return {}
	s, e = span
	comp = _comprehension_facts(lines, s, e, t)
	if comp is not None:
		return comp
	threshold = _indent(lines[s])
	enclosing_try: list[str] = []
	header = None
	for i in range(s - 1, -1, -1):
		code = _strip_comment(lines[i]).strip()
		if not code or _indent(lines[i]) >= threshold:
			continue
		m = _HEADER_RE.match(code)
		if not m:
			continue
		threshold = _indent(lines[i])
		kw = m.group("kw").split()[-1]
		if kw in ("def", "class"):
			return {"in_loop": False}
		if kw == "try":
			enclosing_try.append(lines[i][:threshold])
		if kw in ("for", "while"):
			header = i
			break
	if header is None:
		return {}
	h_ind = _indent(lines[header])
	end = header
	for j in range(header + 1, n):
		code = _strip_comment(lines[j]).strip()
		if not code:
			continue
		if _indent(lines[j]) <= h_ind and not code.startswith((")", "]", "}")):
			break
		end = j
	unit = "    " if lines[header][:h_ind].startswith(" ") else "\t"
	closers: list[str] = []
	for ws in enclosing_try:
		closers += [f"{ws}except BaseException:", f"{ws}{unit}pass"]
	rel = t - header + 1
	for block in (lines[header : end + 1], lines[header : e + 1] + closers):
		tree = _try_parse(block)
		if tree is None or not tree.body or not isinstance(tree.body[0], (ast.For, ast.AsyncFor, ast.While)):
			continue
		loop = tree.body[0]
		if not (loop.lineno <= rel <= (loop.end_lineno or loop.lineno)):
			return {"in_loop": False}
		stmt = _innermost_stmt(loop, rel)
		if stmt is None or stmt is loop:
			return {}
		kind = "while" if isinstance(loop, ast.While) else "for"
		return _loop_facts_for(loop, kind, header + 1, stmt, rel)
	return {}


def format_loop_facts(facts: dict, *, line_offset: int = 0) -> str:
	"""Plain sentences for the AI user message ("" when there are no facts).
	``line_offset`` turns window-relative line numbers into file line numbers."""
	if not facts:
		return ""
	if facts.get("in_loop") is False:
		return (
			"Loop facts computed by the profiler: the marked line is not inside a for or "
			"while loop in the code shown."
		)
	kind = {"for": "for loop", "while": "while loop", "comprehension": "comprehension"}.get(
		facts.get("loop_kind"), "loop",
	)
	parts = [
		"Loop facts computed by the profiler from the code shown: the marked line runs inside "
		f"the {kind} that starts on line {int(facts.get('loop_line') or 0) + line_offset}."
	]
	call = facts.get("call")
	if call:
		deps = facts.get("depends_on_loop_vars") or []
		if deps:
			parts.append(f"The call {call} uses these loop variables: {', '.join(deps)}.")
		else:
			parts.append(f"The arguments of {call} use no variable that changes inside the loop.")
		if facts.get("result_used") is False:
			parts.append(f"The result of {call} is not used.")
		elif facts.get("result_used") is True:
			parts.append(f"The result of {call} is used.")
	writes = facts.get("loop_writes") or []
	if writes:
		parts.append(f"The loop also writes through: {', '.join(writes)}.")
	else:
		parts.append("The loop makes no database write that the profiler can see.")
	return " ".join(parts)


# ---------------------------------------------------------------------------
# AI grounding window
# ---------------------------------------------------------------------------


def enclosing_function_window(
	source_lines: list[str],
	lineno: int,
	*,
	before: int = 24,
	after: int = 24,
	max_lines: int = 80,
	max_line_chars: int | None = None,
) -> list[dict]:
	"""The source window the AI fix prompt shows for ``lineno`` (1-based in
	``source_lines``): the largest enclosing function (decorators included)
	that fits ``max_lines``, else ``lineno - before`` .. ``lineno + after``
	clamped to the file, which is also the answer when the file does not parse
	or the line is not inside a function. Rows are ``{"lineno", "content",
	"is_target"}`` (the shape of renderer.source._read_source_window); a line
	longer than ``max_line_chars`` is cut and ends with "...". Returns [] when
	``lineno`` is out of range."""
	n = len(source_lines or [])
	if isinstance(lineno, bool) or not isinstance(lineno, int) or not 1 <= lineno <= n:
		return []
	start = max(1, lineno - max(0, before))
	end = min(n, lineno + max(0, after))
	try:
		tree = ast.parse("\n".join(source_lines))
	except (SyntaxError, ValueError):
		tree = None
	if tree is not None:
		spans = []
		for node in ast.walk(tree):
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
				first = min([node.lineno] + [d.lineno for d in node.decorator_list])
				last = node.end_lineno or node.lineno
				if first <= lineno <= last and last - first + 1 <= max_lines:
					spans.append((first, last))
		if spans:
			start, end = min(spans, key=lambda s: (s[0], -s[1]))
	rows = []
	for i in range(start, end + 1):
		content = source_lines[i - 1]
		if max_line_chars and len(content) > max_line_chars:
			content = content[:max_line_chars] + "..."
		rows.append({"lineno": i, "content": content, "is_target": i == lineno})
	return rows


# ---------------------------------------------------------------------------
# Redundant Call findings analyzed before the callsite fix
# ---------------------------------------------------------------------------

# The corrected redundant_calls analyzer writes
# technical_detail[CALLSITE_WALK_KEY] = CALLSITE_WALK_FIXED into every Redundant
# Call finding it builds. A finding without that exact stamp was analyzed by the
# old walk, whose callsite may be the outer hook entry instead of the loop.
CALLSITE_WALK_KEY = "callsite_walk"
CALLSITE_WALK_FIXED = "outermost_first"

PRE_L5_REDUNDANT_CALL_NOTE = (
	"This Redundant Call finding was analyzed before the callsite fix, so its line may point at "
	"the outer hook call instead of the loop, and an AI suggestion made for it may change the "
	"wrong loop. Optimus no longer asks the AI about it: re-record the flow to get a corrected "
	"finding."
)


def analyzed_before_callsite_fix(finding: dict) -> bool:
	"""True for a Redundant Call finding (render dict or row-shaped dict) whose
	technical detail lacks the fixed analyzer's callsite stamp."""
	if (finding.get("finding_type") or "") != "Redundant Call":
		return False
	return _detail(finding).get(CALLSITE_WALK_KEY) != CALLSITE_WALK_FIXED
