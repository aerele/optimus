# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""WHERE-clause parsing and existing-index coverage checks. Pure: no Frappe, no DB."""

import re
from dataclasses import dataclass, field

# FULLTEXT / SPATIAL / HASH cannot serve `col = ?`.
USABLE_INDEX_TYPES = frozenset({"BTREE", ""})

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
STRING_RE = re.compile(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.)*\"")
_BETWEEN_RE = re.compile(
	r"\bBETWEEN\s+(?:[^\s()]+|\w+\([^()]*\))\s+AND\s+(?:[^\s()]+|\w+\([^()]*\))", re.IGNORECASE
)
_WHERE_RE = re.compile(r"\bWHERE\b", re.IGNORECASE)
_WHERE_END_RE = re.compile(
	r"\b(?:GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|UNION|FOR\s+UPDATE|LOCK\s+IN)\b", re.IGNORECASE
)
_EQ_RE = re.compile(r"^([\w.]+)\s*(?:=|<=>|IN\b|IS\s+NULL\b)", re.IGNORECASE)
_PLAIN_RE = re.compile(r"^([\w.]+)\s*(?:[<>!]|BETWEEN\b|NOT\b|LIKE\b|IS\b|REGEXP\b|RLIKE\b)", re.IGNORECASE)
_FUNC_LEAF_RE = re.compile(r"^\w+\s*\(")
_IDENT_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*(?:\.\w+)?)(?!\s*\(|\w)")
_NOT_COLUMNS = frozenset({"null", "true", "false", "as", "and", "or", "not", "unsigned", "signed", "char"})


@dataclass
class WhereShape:
	"""Lowercase columns of one table, by role in the WHERE clause."""

	# Outside any OR, which can't be assumed to narrow by that column.
	equality: set = field(default_factory=set)
	plain: set = field(default_factory=set)  # col used bare in any comparison
	wrapped: set = field(default_factory=set)  # col inside a function call, e.g. DATE(col)
	has_or: bool = False


def parse_where(sql: str, table: str, aliases: dict | None = None) -> WhereShape:
	"""Classify the WHERE columns of ``sql`` that belong to ``table``."""
	shape = WhereShape()
	text = _where_text(_clean(sql or ""))
	if not text:
		return shape
	aliases = {k.lower(): _bare_table(v) for k, v in (aliases or {}).items()}
	own = _bare_table(table)
	for leaf, under_or in _leaves(text, False):
		shape.has_or = shape.has_or or under_or
		_classify_leaf(leaf, under_or, own, aliases, shape)
	return shape


def is_served(column: str, shape: WhereShape, indexes: list) -> bool:
	"""True if a b-tree index serves ``column``: it leads, or every earlier column is an equality."""
	col = (column or "").lower()
	for ix in indexes:
		if str(getattr(ix, "index_type", "BTREE") or "").upper() not in USABLE_INDEX_TYPES:
			continue
		cols = [c.lower() for c in ix.columns if c]
		if col not in cols:
			continue
		if all(c in shape.equality for c in cols[: cols.index(col)]):
			return True
	return False


def _clean(sql: str) -> str:
	sql = _COMMENT_RE.sub(" ", sql)
	sql = STRING_RE.sub("'?'", sql).replace("`", "")
	return re.sub(r"\s+", " ", sql).strip()


def _depths(text: str) -> list[int]:
	depth, out = 0, []
	for ch in text:
		if ch == ")":
			depth -= 1
		out.append(depth)
		if ch == "(":
			depth += 1
	return out


def _find_top(text: str, pattern: re.Pattern) -> re.Match | None:
	depths = _depths(text)
	for m in pattern.finditer(text):
		if depths[m.start()] == 0:
			return m
	return None


def _where_text(text: str) -> str:
	m = _find_top(text, _WHERE_RE)
	if not m:
		return ""
	tail = text[m.end() :]
	end = _find_top(tail, _WHERE_END_RE)
	tail = tail[: end.start()] if end else tail
	return _BETWEEN_RE.sub("BETWEEN ?", tail).strip()


def _split_top(text: str, word: str) -> list[str]:
	depths = _depths(text)
	parts, last = [], 0
	for m in re.finditer(rf"\b{word}\b", text, re.IGNORECASE):
		if depths[m.start()] == 0:
			parts.append(text[last : m.start()])
			last = m.end()
	parts.append(text[last:])
	return [p.strip() for p in parts if p.strip()]


def _wrapped_in_parens(text: str) -> bool:
	"""True iff the first "(" closes at the very last character."""
	if not (text.startswith("(") and text.endswith(")")):
		return False
	depth = 0
	for i, ch in enumerate(text):
		depth += (ch == "(") - (ch == ")")
		if depth == 0:
			return i == len(text) - 1
	return False


def _leaves(text: str, under_or: bool) -> list[tuple[str, bool]]:
	text = text.strip()
	while _wrapped_in_parens(text):
		text = text[1:-1].strip()
	for word, is_or in (("OR", True), ("AND", False)):
		parts = _split_top(text, word)
		if len(parts) > 1:
			return [leaf for p in parts for leaf in _leaves(p, under_or or is_or)]
	return [(text, under_or)]


def _resolve(ref: str, own: str, aliases: dict) -> str | None:
	"""Column name if ``ref`` belongs to table ``own`` (unqualified refs do), else None."""
	if "." not in ref:
		return ref.lower()
	qualifier, col = ref.lower().rsplit(".", 1)
	if _bare_table(qualifier) == own or aliases.get(qualifier) == own:
		return col
	return None


def _bare_table(name: str) -> str:
	return (name or "").strip().strip("`").lower()


def _classify_leaf(leaf: str, under_or: bool, own: str, aliases: dict, shape: WhereShape) -> None:
	if _FUNC_LEAF_RE.match(leaf):
		for ref in _IDENT_RE.findall(leaf):
			col = _resolve(ref, own, aliases)
			if col and col not in _NOT_COLUMNS:
				shape.wrapped.add(col)
		return
	if m := _EQ_RE.match(leaf):
		col = _resolve(m.group(1), own, aliases)
		if col:
			shape.plain.add(col)
			if not under_or:
				shape.equality.add(col)
	elif m := _PLAIN_RE.match(leaf):
		if col := _resolve(m.group(1), own, aliases):
			shape.plain.add(col)
