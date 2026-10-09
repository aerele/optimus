# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The WHERE-clause scanner behind ``index_recipes``: a conservative token scan that
decides which WHERE columns a composite index can use and how. Pure string work, no
Frappe import. Split out of ``index_recipes`` unchanged; ``index_recipes`` imports the
names it needs and re-exports the module's public ones."""

from __future__ import annotations

import re
from collections import defaultdict

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
# The comparisons an index uses as an equality prefix ("in1": an IN list Frappe collapsed).
_EQUALITY_KINDS: frozenset[str] = frozenset({"eq", "in", "in1"})
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
	"not": "compared only by !=, <> or NOT, which usually matches most of the table's rows",
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
	"""``"eq"`` when the plain reference ``tokens[i..j]`` is compared by =, <=> or IS NULL;
	``"in"`` for IN with several values or a subquery (equality on several values);
	``"in1"`` for IN with one placeholder, which Frappe's recorder writes for any IN list
	(normalize_query turns IN (?, ?, ?) into IN (?)), so it may hold one value; ``"not"``
	for !=, <> or NOT ..., which match most rows and never narrow an index; else
	``"range"`` (<, >, BETWEEN, IS NOT NULL, a prefix LIKE, or a shape the scan does not
	know, which is never treated as equality). A bare NOT right before the reference negates
	the whole comparison (``NOT name = ?`` is ``name != ?``), so it is ``"not"`` too."""
	if i > 0 and tokens[i - 1].lower() == "not":
		return "not"
	after = [tok.lower() for tok in tokens[j + 1 : j + 3]]
	first = after[0] if after else ""
	if first == "in":
		rest = tokens[j + 2 : j + 5]
		single = (
			len(rest) == 3 and rest[0] == "(" and rest[2] == ")" and _VALUE_RE.fullmatch(rest[1]) is not None
		) or (len(rest) >= 1 and rest[0] != "(" and _VALUE_RE.fullmatch(rest[0]) is not None)
		return "in1" if single else "in"
	if first in _EQUALITY:
		return "eq"
	if first == "is":
		return "eq" if after[1:] == ["null"] else "range"
	if first in ("!=", "<>", "not"):
		return "not"
	if not first or first in _AND_WORDS:
		before = tokens[i - 1].lower() if i > 0 else ""
		if before in ("!=", "<>"):
			return "not"
		return "eq" if before in _EQUALITY else "range"
	return "range"


def _value_token(tok: str) -> bool:
	"""True for a placeholder or a literal. A double-quoted token is a name, as everywhere in
	the scan (a Postgres identifier), never a value."""
	return _VALUE_RE.fullmatch(tok) is not None and not tok.startswith('"')


def _valued(tokens: list[str], i: int, j: int, pairs: dict[int, int], pinned=frozenset()) -> bool:
	"""True when the plain reference ``tokens[i..j]`` is compared with a value: = or <=> a
	placeholder, a literal or a ``(SELECT ...)`` (on either side), or IN a list of them or a
	subquery. A comparison with a column, such as the join condition ``pr_item.parent =
	pr.name``, is none (round 4, item 1), unless that column is in ``pinned``: another AND
	piece compares it with a value, so the database carries that value over
	(``mpa.parent = mp.name AND mp.name = ?`` finds mpa's rows by parent)."""
	after = tokens[j + 1].lower() if j + 1 < len(tokens) else ""
	if after in _EQUALITY:
		k = j + 2
		if k < len(tokens) and tokens[k] == "(":
			return k + 1 < len(tokens) and tokens[k + 1].lower() in ("select", "with")
		if k < len(tokens) and _NAME_RE.fullmatch(tokens[k]) and not tokens[k].startswith('"'):
			return _ref_key(tokens, k, _chain_end(tokens, k)) in pinned
		return k < len(tokens) and _value_token(tokens[k])
	if after == "in":
		k = j + 2
		if k < len(tokens) and tokens[k] == "(" and k in pairs:
			inner = tokens[k + 1 : pairs[k]]
			if inner and inner[0].lower() in ("select", "with"):
				return True
			return bool(inner) and all(tok == "," or _value_token(tok) for tok in inner)
		return k < len(tokens) and _value_token(tokens[k])
	before = tokens[i - 1].lower() if i > 0 else ""
	if before not in _EQUALITY or i < 2:
		return False
	if _NAME_RE.fullmatch(tokens[i - 2]) and not tokens[i - 2].startswith('"'):
		return _ref_key(tokens, _chain_start(tokens, i - 2), i - 2) in pinned
	return _value_token(tokens[i - 2])


def _pinned(conjuncts: list[list[str]]) -> frozenset[tuple[str, str]]:
	"""``_ref_key`` of every column reference, of any table, that an AND piece of its own
	compares with a value: ``ref = value``, ``value = ref`` or ``ref IN (values or a
	subquery)``."""
	out: set[tuple[str, str]] = set()
	for part in conjuncts:
		if not part:
			continue
		pairs = _bracket_pairs(part)
		if _NAME_RE.fullmatch(part[0]) and not part[0].startswith('"'):
			j = _chain_end(part, 0)
			after = part[j + 1].lower() if j + 1 < len(part) else ""
			whole = (
				j + 3 == len(part) if after in _EQUALITY or (after == "in" and part[j + 2 : j + 3] != ["("])
				else after == "in" and pairs.get(j + 2) == len(part) - 1
			)
			if whole and _valued(part, 0, j, pairs):
				out.add(_ref_key(part, 0, j))
		elif len(part) >= 3 and _value_token(part[0]) and part[1].lower() in _EQUALITY and _NAME_RE.fullmatch(part[2]):
			j = _chain_end(part, 2)
			if j == len(part) - 1:
				out.add(_ref_key(part, 2, j))
	return frozenset(out)


def _conjunct_refs(tokens: list[str], qualifiers, pinned=frozenset()) -> list[tuple[str, set[str], str, bool]]:
	"""``(lowercase column, kinds, comparison, valued)`` for each column reference in one AND
	piece; empty kinds is a plain use, compared ``"eq"``, ``"in"`` (several values: IN, or
	a same-column ``IS NULL OR =``) or ``"range"``; ``valued`` marks an equality use compared
	with a value, never with a column (``_valued``). A reference qualified by another table
	(``acc.company``) and everything inside a ``(SELECT ...)`` group is skipped."""
	pairs = _bracket_pairs(tokens)
	out: list[tuple[str, set[str], str, bool]] = []

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
				comparison = "" if kinds else "in" if multi else _comparison(tokens, i, j)
				if comparison == "not":
					kinds.add("not")  # !=, <> or NOT: never narrows an index, left out like a shape
					comparison = ""
				# a same-column OR's branches each compare a value (_single_column_branch)
				valued = comparison in _EQUALITY_KINDS and (multi or _valued(tokens, i, j, pairs, pinned))
				out.append((name, kinds, comparison, valued))
			i = j + 1

	# the top level has an OR only when every branch compares one column (_conjuncts)
	top_or, top_key = _level_or(tokens, 0, len(tokens), pairs)
	walk(0, len(tokens), [(None, top_or, top_key)])
	return out


def _scan_where(query: str, labelled, qualifiers=None, *, truncated: bool = False):
	"""``(unusable, comparisons, valued)`` for the WHERE columns of ``labelled``: ``unusable``
	is ``{column: kinds}`` for each column a composite index cannot use (a kind is ``"or"``,
	``"like"``, ``"function:<NAME>"``, ``"expression"`` or ``"unsure"``, see the section
	comment), ``comparisons`` is ``{column: "eq" | "in" | "range"}`` for the usable ones and
	``valued`` the same for the equality uses compared with a value, never with a column
	(``_valued``: a key lookup needs one, round 4 item 1)."""
	where_cols: list[str] = []
	for label, col in labelled or []:
		if label == "WHERE" and col not in where_cols:
			where_cols.append(col)
	if not where_cols:
		return {}, {}, {}
	tokens = _where_tokens(query, truncated=truncated)
	if tokens is None:
		return {col: {"unsure"} for col in where_cols}, {}, {}
	conjuncts = _conjuncts(tokens)
	if conjuncts is None:
		return {col: {"or"} for col in where_cols}, {}, {}
	usable: dict[str, str] = {}
	valued: dict[str, str] = {}
	kinds: dict[str, set[str]] = defaultdict(set)
	rank = {"eq": 0, "in1": 1, "in": 1, "range": 2}  # the most selective plain use wins
	pinned = _pinned(conjuncts)
	for part in conjuncts:
		for name, ref_kinds, comparison, by_value in _conjunct_refs(part, qualifiers, pinned):
			if ref_kinds:
				kinds[name] |= ref_kinds
				continue
			if name not in usable or rank[comparison] < rank[usable[name]]:
				usable[name] = comparison
			if by_value and (name not in valued or rank[comparison] < rank[valued[name]]):
				valued[name] = comparison
	unusable = {
		col: set(kinds.get(col.lower()) or {"unsure"}) for col in where_cols if col.lower() not in usable
	}
	return (
		unusable,
		{col: usable[col.lower()] for col in where_cols if col.lower() in usable},
		{col: valued[col.lower()] for col in where_cols if col.lower() in valued},
	)
