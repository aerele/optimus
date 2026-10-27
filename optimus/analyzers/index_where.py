# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""WHERE-clause parsing and existing-index coverage checks. Pure: no Frappe, no DB."""

import re
from dataclasses import dataclass, field

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
STRING_RE = re.compile(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.)*\"")
_BACKTICKED_RE = re.compile(r"`([^`]*)`")
_TABLE_REF_RE = re.compile(r"\b(?:FROM|JOIN)\s+([\w.]+)", re.IGNORECASE)
_BETWEEN_RE = re.compile(
	r"\bBETWEEN\s+(?:[^\s()]+|\w+\([^()]*\))\s+AND\s+(?:[^\s()]+|\w+\([^()]*\))", re.IGNORECASE
)
_WHERE_RE = re.compile(r"\bWHERE\b", re.IGNORECASE)
_WHERE_END_RE = re.compile(
	r"\b(?:GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|UNION|FOR\s+UPDATE|LOCK\s+IN)\b", re.IGNORECASE
)
_EQ_RE = re.compile(r"^([\w.]+)\s*(?:=|<=>|IN\b|IS\s+NULL\b)", re.IGNORECASE)
_PLAIN_RE = re.compile(r"^([\w.]+)\s*(?:[<>!]|BETWEEN\b|NOT\b|LIKE\b|IS\b|REGEXP\b|RLIKE\b)", re.IGNORECASE)
_LEADING_WILDCARD_RE = re.compile(r"^([\w.]+)\s*(?:NOT\s+)?LIKE\s+'%", re.IGNORECASE)
_FUNC_LEAF_RE = re.compile(r"^(?!(?:NOT|EXISTS|ANY|ALL|SOME|IN)\b)\w+\s*\(", re.IGNORECASE)
_NOT_PREFIX_RE = re.compile(r"^NOT\s*(?=\()|^NOT\s+(?=EXISTS\b)", re.IGNORECASE)
_WEAK_RE = re.compile(r"^[\w.]+\s*(?:!=|<>|NOT\s+(?:IN|LIKE|BETWEEN|REGEXP|RLIKE)\b|IS\s+NOT\b)", re.IGNORECASE)
_SUBQUERY_RE = re.compile(r"\bSELECT\b", re.IGNORECASE)
_IDENT_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*(?:\.\w+)?)(?!\s*\(|\w)")
_NOT_COLUMNS = frozenset({"null", "true", "false", "as", "and", "or", "not", "unsigned", "signed", "char"})


@dataclass
class WhereShape:
	"""Lowercase columns of one table, by role in the WHERE clause."""

	# Outside any OR, which can't be assumed to narrow by that column.
	equality: set = field(default_factory=set)
	plain: set = field(default_factory=set)  # col used bare in any comparison
	wrapped: set = field(default_factory=set)  # col inside a function call, e.g. DATE(col)
	scan: set = field(default_factory=set)  # col filtered by LIKE '%...', which an index can't narrow
	weak: set = field(default_factory=set)  # col under !=, NOT IN, NOT LIKE or NOT (...)
	narrow: set = field(default_factory=set)  # col under a predicate an index can narrow
	has_or: bool = False
	# Repeated table or unqualified columns in a join: the columns can't be attributed safely.
	ambiguous: bool = False


def parse_where(sql: str, table: str, aliases: dict | None = None) -> WhereShape:
	"""Classify the WHERE columns of ``sql`` that belong to ``table``."""
	shape = WhereShape()
	text = _where_text(_clean(sql or ""))
	if not text:
		return shape
	aliases = {_bare_table(k): _bare_table(v) for k, v in (aliases or {}).items()}
	own = _bare_table(table)
	targets = [_bare_table(t) for t in _TABLE_REF_RE.findall(_clean(sql))]
	shape.ambiguous = targets.count(own) > 1
	multi = len(set(targets)) > 1
	for leaf, under_or in _leaves(text, False):
		shape.has_or = shape.has_or or under_or
		_classify_leaf(leaf, under_or, own, aliases, shape, multi)
	return shape


def is_served(column: str, shape: WhereShape, indexes: list) -> bool:
	"""True if a b-tree index serves ``column``: it leads, or every earlier column is an equality."""
	col = (column or "").lower()
	if shape.ambiguous or (col in shape.scan and col not in shape.equality):
		return False
	for ix in indexes:
		cols = covered_columns(ix)
		if col not in cols:
			continue
		if all(c in shape.equality for c in cols[: cols.index(col)]):
			return True
	return False


def is_weak_only(column: str, shape: WhereShape) -> bool:
	"""True if the only predicates on ``column`` are negations an index may not be able to use."""
	col = (column or "").lower()
	return col in shape.weak and col not in shape.narrow


def covered_columns(ix) -> list[str]:
	"""Lowercase key columns a b-tree lookup can use: all of them, or the usable leading part."""
	cols = ix.columns if getattr(ix, "eligible", True) else getattr(ix, "usable_prefix", ())
	return [c.lower() for c in cols if c]


def _mask_string(match: re.Match) -> str:
	return "'%?'" if match.group(0)[1:2] == "%" else "'?'"


def _clean(sql: str) -> str:
	sql = _COMMENT_RE.sub(" ", sql)
	sql = STRING_RE.sub(_mask_string, sql)
	sql = _BACKTICKED_RE.sub(lambda m: m.group(1).replace(" ", "_"), sql)
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


def _resolve(ref: str, own: str, aliases: dict, shape: WhereShape, multi: bool) -> str | None:
	"""Column name if ``ref`` belongs to table ``own`` (unqualified refs do), else None."""
	if "." not in ref:
		shape.ambiguous = shape.ambiguous or multi
		return ref.lower()
	qualifier, col = ref.lower().rsplit(".", 1)
	if _bare_table(qualifier) == own or aliases.get(qualifier) == own:
		return col
	return None


def _bare_table(name: str) -> str:
	return (name or "").strip().strip("`").replace(" ", "_").lower()


def _wrapped_refs(leaf: str) -> list[str]:
	"""Identifiers inside the function call that opens ``leaf``; its right-hand side is not scanned."""
	depth = 0
	for i, ch in enumerate(leaf):
		depth += (ch == "(") - (ch == ")")
		if depth == 0 and ch == ")":
			return _IDENT_RE.findall(leaf[: i + 1])
	return []


def _classify_leaf(
	leaf: str, under_or: bool, own: str, aliases: dict, shape: WhereShape, multi: bool = False
) -> None:
	if _NOT_PREFIX_RE.match(leaf):
		_classify_negated(leaf, own, aliases, shape, multi)
		return
	if _FUNC_LEAF_RE.match(leaf):
		if _SUBQUERY_RE.search(leaf):
			return
		for ref in _wrapped_refs(leaf):
			col = _resolve(ref, own, aliases, shape, multi)
			if col and col not in _NOT_COLUMNS:
				shape.wrapped.add(col)
		return
	if m := _LEADING_WILDCARD_RE.match(leaf):
		if col := _resolve(m.group(1), own, aliases, shape, multi):
			shape.scan.add(col)
			shape.plain.add(col)
	elif m := _EQ_RE.match(leaf):
		col = _resolve(m.group(1), own, aliases, shape, multi)
		if col:
			shape.plain.add(col)
			shape.narrow.add(col)
			if not under_or:
				shape.equality.add(col)
	elif m := _PLAIN_RE.match(leaf):
		if col := _resolve(m.group(1), own, aliases, shape, multi):
			shape.plain.add(col)
			(shape.weak if _WEAK_RE.match(leaf) else shape.narrow).add(col)


def _classify_negated(leaf: str, own: str, aliases: dict, shape: WhereShape, multi: bool) -> None:
	"""Classify the operand of a leading NOT: its columns are plain but never equalities."""
	inner = _NOT_PREFIX_RE.sub("", leaf, count=1).strip()
	scratch = WhereShape()
	for sub, _ in _leaves(inner, True):
		_classify_leaf(sub, True, own, aliases, scratch, multi)
	shape.ambiguous = shape.ambiguous or scratch.ambiguous
	shape.wrapped |= scratch.wrapped
	shape.scan |= scratch.scan
	shape.plain |= scratch.plain
	shape.weak |= scratch.plain - scratch.wrapped
