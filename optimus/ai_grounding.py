# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""What the AI fix prompt may claim about a finding's source (pure: no site access, no I/O).

- ``grounding_window``: the one source-window helper. The whole enclosing function when
  it fits ``max_lines``, else ``before`` / ``after`` lines around the target, plus the
  whole file's parsed tree.
- ``loop_facts_from_tree``: the chain of loops around a callsite, from that whole-file
  tree, with the line of every fact, so the prompt keeps only facts about lines it still
  shows after its budget trims the window.
- ``format_loop_facts``: those facts as sentences for the shown lines.
- ``hot_line_gate``: why a Hot Line gets no AI call, decided by Phase 2's measured time
  per hit (owner decision A4) or Phase 1's named callee.
- ``analyzed_before_callsite_fix``: Redundant Call findings built by the older stack walk.
"""

from __future__ import annotations

import ast
import builtins
import json
import re
import sys
import textwrap
from typing import NamedTuple

from optimus.analyzers.base import (
	CALLSITE_WALK_FIXED,
	CALLSITE_WALK_KEY,
	FRAMEWORK_APPS,
	is_framework_callsite,
)

LOOP_FACT_TYPES: frozenset[str] = frozenset({"N+1 Query", "Redundant Call", "Hot Line"})
# Repetition of these types can come from a loop in a caller outside the window (P7).
CALLER_HINT_TYPES: frozenset[str] = frozenset({"N+1 Query", "Redundant Call"})

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
_SAFE_NAME_RE = re.compile(r"^[A-Za-z_][\w.()\[\]]*$")
_COMPS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)
_LOOPS = (ast.For, ast.AsyncFor, ast.While, *_COMPS)
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_KIND_WORDS = {"for": "for loop", "while": "while loop", "comprehension": "comprehension"}


class GroundingWindow(NamedTuple):
	"""``rows`` are ``{"lineno", "content", "is_target"}``; ``start`` / ``end`` are the
	first and last line shown (1-based, 0 when there are no rows); ``tree`` is the whole
	file's AST, or None when it does not parse."""

	rows: list[dict]
	start: int
	end: int
	tree: ast.Module | None


def grounding_window(
	path_lines: list[str],
	target_lineno,
	before: int,
	after: int,
	max_lines: int = 80,
	*,
	max_line_chars: int | None = None,
) -> GroundingWindow:
	"""The source window the AI fix prompt shows for ``target_lineno`` (1-based in
	``path_lines``): the largest enclosing function (decorators included) that fits
	``max_lines``, else ``target - before`` .. ``target + after`` clamped to the file,
	which is also the answer when the file does not parse or the line is in no function.
	A line longer than ``max_line_chars`` is cut and ends with "..."."""
	lines = [str(x) for x in (path_lines or [])]
	n = len(lines)
	if isinstance(target_lineno, bool) or not isinstance(target_lineno, int) or not 1 <= target_lineno <= n:
		return GroundingWindow([], 0, 0, None)
	start = max(1, target_lineno - max(0, before))
	end = min(n, target_lineno + max(0, after))
	try:
		tree = ast.parse("\n".join(lines))
	except (SyntaxError, ValueError):
		tree = None
	if tree is not None:
		spans = []
		for node in ast.walk(tree):
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
				first = min([node.lineno] + [d.lineno for d in node.decorator_list])
				last = node.end_lineno or node.lineno
				if first <= target_lineno <= last and last - first + 1 <= max_lines:
					spans.append((first, last))
		if spans:
			start, end = min(spans, key=lambda span: (span[0], -span[1]))
	rows = []
	for lineno in range(start, end + 1):
		content = lines[lineno - 1]
		if max_line_chars and len(content) > max_line_chars:
			content = content[:max_line_chars] + "..."
		rows.append({"lineno": lineno, "content": content, "is_target": lineno == target_lineno})
	return GroundingWindow(rows, start, end, tree)


def call_name(func: ast.AST) -> str | None:
	"""A call target as text: ``frappe.db.get_value``, ``super().validate``,
	``self.items[].db_update`` (a subscript shows as ``[]``); None for other shapes."""
	parts: list[str] = []
	node = func
	while True:
		if isinstance(node, ast.Attribute):
			parts.append("." + node.attr)
			node = node.value
		elif isinstance(node, ast.Subscript):
			parts.append("[]")
			node = node.value
		elif isinstance(node, ast.Call):
			parts.append("()")
			node = node.func
		else:
			break
	if not isinstance(node, ast.Name):
		return None
	return node.id + "".join(reversed(parts))


def _own_walk(node: ast.AST):
	"""Every node under ``node`` except the bodies of nested functions, lambdas and
	classes (they run later, not on this pass)."""
	stack = list(ast.iter_child_nodes(node))
	while stack:
		child = stack.pop()
		if isinstance(child, _SCOPES):
			continue
		yield child
		stack.extend(ast.iter_child_nodes(child))


def _names(node: ast.AST) -> set[str]:
	return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _bound(target: ast.AST) -> set[str]:
	"""Names a target rebinds (``x``, ``(a, b)``, ``*rest``); an attribute or a
	subscript (``self.total``, ``rows[i]``) binds no name."""
	if isinstance(target, ast.Name):
		return {target.id}
	if isinstance(target, (ast.Tuple, ast.List)):
		out: set[str] = set()
		for element in target.elts:
			out |= _bound(element)
		return out
	if isinstance(target, ast.Starred):
		return _bound(target.value)
	return set()


def _per_iteration(loop: ast.AST) -> list[ast.AST]:
	"""The children of ``loop`` that run on every pass (a for loop's iterable runs once)."""
	if isinstance(loop, (ast.For, ast.AsyncFor)):
		return list(loop.body)
	if isinstance(loop, ast.While):
		return [loop.test, *loop.body]
	parts = [loop.key, loop.value] if isinstance(loop, ast.DictComp) else [loop.elt]
	for index, generator in enumerate(loop.generators):
		parts.extend(generator.ifs)
		if index:
			parts.append(generator.iter)
	return parts


def _kind(loop: ast.AST) -> str:
	if isinstance(loop, _COMPS):
		return "comprehension"
	return "while" if isinstance(loop, ast.While) else "for"


def _is_db_call(call: ast.Call) -> bool:
	name = call_name(call.func) or ""
	return any(name == suffix or name.endswith("." + suffix) for suffix in _DB_CALL_SUFFIXES)


def _sql_head(arg: ast.AST) -> str | None:
	"""The leading text of a SQL argument: a literal, an f-string, a ``.format`` call on a
	literal, or the left side of ``+`` / ``%``."""
	if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
		return arg.value
	if isinstance(arg, ast.JoinedStr):
		first = arg.values[0] if arg.values else None
		return first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None
	if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute) and arg.func.attr == "format":
		return _sql_head(arg.func.value)
	if isinstance(arg, ast.BinOp):
		return _sql_head(arg.left)
	return None


def _loop_bindings(loop: ast.AST) -> list[tuple[str, int]]:
	"""(name, line) for every name that takes a new value on each pass of ``loop``."""
	out: set[tuple[str, int]] = set()
	if isinstance(loop, (ast.For, ast.AsyncFor)):
		out |= {(name, loop.target.lineno) for name in _bound(loop.target)}
	elif isinstance(loop, _COMPS):
		for generator in loop.generators:
			out |= {(name, generator.target.lineno) for name in _bound(generator.target)}
	for part in _per_iteration(loop):
		if isinstance(part, _SCOPES):
			continue
		for node in (part, *_own_walk(part)):
			if isinstance(node, ast.Assign):
				for target in node.targets:
					out |= {(name, node.lineno) for name in _bound(target)}
			elif isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.NamedExpr, ast.For, ast.AsyncFor)):
				out |= {(name, node.lineno) for name in _bound(node.target)}
			elif isinstance(node, ast.withitem) and node.optional_vars is not None:
				out |= {(name, node.optional_vars.lineno) for name in _bound(node.optional_vars)}
	return sorted(out, key=lambda item: (item[1], item[0]))


def _path(node: ast.AST) -> tuple[str, ...] | None:
	"""``self.batch_id`` -> ("self", ".batch_id"); ``args["x"]`` -> ("args", "['x']");
	a subscript with a computed key is "[]" (any key). None without a Name base."""
	parts: list[str] = []
	while isinstance(node, (ast.Attribute, ast.Subscript)):
		if isinstance(node, ast.Attribute):
			parts.append("." + node.attr)
		else:
			parts.append(f"[{node.slice.value!r}]" if isinstance(node.slice, ast.Constant) else "[]")
		node = node.value
	return (node.id, *reversed(parts)) if isinstance(node, ast.Name) else None


def _overlaps(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
	"""One path is a prefix of the other (``args`` / ``args['x']``)."""
	return all(
		x == y or ("[]" in (x, y) and x.startswith("[") and y.startswith("[")) for x, y in zip(a, b, strict=False)
	)


def _read_paths(call: ast.Call, parent: dict) -> set[tuple[str, ...]]:
	"""The longest attribute / subscript path over each name the call reads."""
	out: set[tuple[str, ...]] = set()
	for root in [call.func, *call.args, *(keyword.value for keyword in call.keywords)]:
		for name in (n for n in ast.walk(root) if isinstance(n, ast.Name)):
			top = name
			while isinstance(parent.get(top), (ast.Attribute, ast.Subscript)) and parent[top].value is top:
				top = parent[top]
			path = _path(top)
			if path:
				out.add(path)
	return out


def _stored_paths(loop: ast.AST) -> set[tuple[tuple[str, ...], int]]:
	"""(path, line) for every attribute or subscript a pass of ``loop`` assigns."""
	out: set[tuple[tuple[str, ...], int]] = set()
	for part in _per_iteration(loop):
		if isinstance(part, _SCOPES):
			continue
		for node in (part, *_own_walk(part)):
			targets = node.targets if isinstance(node, ast.Assign) else (
				[node.target] if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else []
			)
			for target in targets:
				for sub in ast.walk(target):
					if isinstance(sub, (ast.Attribute, ast.Subscript)) and isinstance(sub.ctx, ast.Store):
						path = _path(sub)
						if path:
							out.add((path, node.lineno))
	return out


def _qb_write(call: ast.Call) -> tuple[str | None, list[ast.Call]]:
	"""The write a query-builder chain that ends in ``.run()`` makes, rooted in
	``frappe.qb`` or ``qb``: ``frappe.qb.update``, ``frappe.qb.into`` or
	``frappe.qb.from_().delete``, plus the calls the chain is made of. ``(None, [])``
	for any other call (a ``select`` chain reads)."""
	if not (isinstance(call.func, ast.Attribute) and call.func.attr == "run"):
		return None, []
	attrs: list[str] = []
	inner: list[ast.Call] = []
	node: ast.AST = call.func
	while isinstance(node, (ast.Attribute, ast.Call)):
		if isinstance(node, ast.Attribute):
			attrs.append(node.attr)
			node = node.value
		else:
			inner.append(node)
			node = node.func
	if not isinstance(node, ast.Name):
		return None, []
	chain = [node.id, *reversed(attrs)]
	start = 1 if chain[0] == "qb" else 2 if chain[:2] == ["frappe", "qb"] else 0
	verb = chain[start] if start and len(chain) > start else ""
	if verb in ("update", "into"):
		return f"frappe.qb.{verb}", inner
	if verb == "from_" and "delete" in chain[start + 1 :]:
		return "frappe.qb.from_().delete", inner
	return None, []


def _loop_writes(loop: ast.AST) -> list[tuple[str, int]]:
	"""(call, line) for every write the profiler can name inside ``loop``'s passes."""
	hits: set[tuple[str, int]] = set()
	in_chain: set[ast.AST] = set()  # calls already named as part of a query-builder write
	for part in _per_iteration(loop):
		if isinstance(part, _SCOPES):
			continue
		for node in (part, *_own_walk(part)):
			if not isinstance(node, ast.Call) or node in in_chain:
				continue
			qb_write, chain = _qb_write(node)
			if qb_write:
				hits.add((qb_write, node.lineno))
				in_chain.update(chain)
				continue
			name = call_name(node.func) or ""
			last = node.func.attr if isinstance(node.func, ast.Attribute) else name
			if name and last in _WRITE_ATTRS and not (last == "insert" and len(node.args) >= 2):
				hits.add((name, node.lineno))
			if name.endswith("db.sql") and node.args:
				match = _SQL_VERB_RE.match(_sql_head(node.args[0]) or "")
				if match and match.group(1).upper() not in _READ_SQL_VERBS:
					hits.add((f"frappe.db.sql({match.group(1).upper()})", node.lineno))
	return sorted((h for h in hits if _SAFE_NAME_RE.match(h[0])), key=lambda h: (h[1], h[0]))


def loop_facts_from_tree(tree: ast.AST | None, target_lineno) -> dict:
	"""Facts about the loops that run ``target_lineno`` (1-based, in the file ``tree``
	was parsed from) on every pass, innermost first, up to the enclosing function or class.

	``{}`` when nothing can be said; ``{"in_loop": False}`` when no loop of the
	enclosing function runs the line on every pass; otherwise ``{"in_loop": True,
	"call", "call_line", "uses", "result_used", "loops": [{"kind", "line", "bound",
	"writes"}]}``. ``bound`` and ``writes`` are ``[name, line]`` pairs; identifiers only,
	never values."""
	if tree is None or isinstance(target_lineno, bool) or not isinstance(target_lineno, int) or target_lineno < 1:
		return {}
	parent: dict[ast.AST, ast.AST] = {}
	for node in ast.walk(tree):
		for child in ast.iter_child_nodes(node):
			parent[child] = node
	spanning = [
		n for n in ast.walk(tree)
		if isinstance(n, ast.Call) and n.lineno <= target_lineno <= (n.end_lineno or n.lineno)
	]
	starting = sorted(
		[c for c in spanning if c.lineno == target_lineno] or spanning, key=lambda c: (c.lineno, c.col_offset),
	)
	call = next((c for c in starting if _is_db_call(c)), None) or (starting[0] if starting else None)
	anchor: ast.AST | None = call
	if anchor is None:
		statements = [
			n for n in ast.walk(tree)
			if isinstance(n, ast.stmt) and n.lineno <= target_lineno <= (n.end_lineno or n.lineno)
		]
		if not statements:
			return {}
		anchor = max(statements, key=lambda s: (s.lineno, -(s.end_lineno or s.lineno)))
	loops: list[ast.AST] = []
	grand, child, node = None, anchor, parent.get(anchor)
	while node is not None and not isinstance(node, _SCOPES):
		probe = grand if isinstance(child, ast.comprehension) else child
		if isinstance(node, _LOOPS) and any(probe is part for part in _per_iteration(node)):
			loops.append(node)
		grand, child, node = child, node, parent.get(node)
	if not loops:
		return {"in_loop": False}
	uses: set[str] = set()
	holder = None
	if call is not None:
		uses = _names(call.func)
		for argument in [*call.args, *(keyword.value for keyword in call.keywords)]:
			uses |= _names(argument)
		holder = parent.get(call)
		if isinstance(holder, ast.Await):
			holder = parent.get(holder)
	name = call_name(call.func) if call is not None else None
	reads = _read_paths(call, parent) if call is not None else set()
	return {
		"in_loop": True,
		"call": name if name and _SAFE_NAME_RE.match(name) else None,
		"call_line": call.lineno if call is not None else target_lineno,
		"uses": sorted(n for n in uses if _SAFE_NAME_RE.match(n)),
		"result_used": None if call is None else not isinstance(holder, ast.Expr),
		"loops": [
			{
				"kind": _kind(loop),
				"line": loop.lineno,
				"bound": [list(item) for item in _loop_bindings_read(loop, reads)],
				"writes": [list(item) for item in _loop_writes(loop)],
			}
			for loop in loops
		],
	}


def _loop_bindings_read(loop: ast.AST, reads: set[tuple[str, ...]]) -> list[tuple[str, int]]:
	"""``_loop_bindings`` plus the base name of a stored path the call reads
	(``args["id"] = ...`` then ``.format(**args)``); ``self.total += ...`` leaves a
	call that reads ``self.company`` alone (P11b)."""
	extra = {(path[0], line) for path, line in _stored_paths(loop) if any(_overlaps(path, r) for r in reads)}
	return sorted(set(_loop_bindings(loop)) | extra, key=lambda item: (item[1], item[0]))


def _shift(facts: dict, offset: int) -> dict:
	if not facts.get("in_loop") or not offset:
		return facts
	shifted = dict(facts, call_line=facts["call_line"] + offset)
	shifted["loops"] = [
		dict(
			loop,
			line=loop["line"] + offset,
			bound=[[name, line + offset] for name, line in loop["bound"]],
			writes=[[name, line + offset] for name, line in loop["writes"]],
		)
		for loop in facts["loops"]
	]
	return shifted


def loop_facts_from_window(rows: list[dict], target_lineno: int) -> dict:
	"""``loop_facts_from_tree`` over the window itself, for a finding that carries no
	precomputed facts; a window that does not parse on its own gives ``{}``, and so does
	"not in a loop" when no function or lambda holding the line starts in the window."""
	usable = [r for r in rows or [] if isinstance(r, dict) and isinstance(r.get("lineno"), int)]
	if not usable or isinstance(target_lineno, bool) or not isinstance(target_lineno, int):
		return {}
	first = usable[0]["lineno"]
	try:
		tree = ast.parse(textwrap.dedent("\n".join(str(r.get("content") or "") for r in usable)))
	except (SyntaxError, ValueError):
		return {}
	local = target_lineno - first + 1
	facts = loop_facts_from_tree(tree, local)
	# A window that starts inside a body cannot see a loop header above it: "not in a
	# loop" holds only when the function or lambda holding the line starts in the window.
	if facts.get("in_loop") is False and not any(
		isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
		and node.lineno <= local <= (node.end_lineno or node.lineno)
		for node in ast.walk(tree)
	):
		return {}
	return _shift(facts, first - 1)


def _shown(line, first_line: int, last_line: int) -> bool:
	return isinstance(line, int) and first_line <= line <= last_line


def format_loop_facts(facts: dict, *, first_line: int, last_line: int, caller_hint: bool = False) -> str:
	"""Sentences about ``facts`` for the source lines ``first_line``..``last_line`` the
	prompt still shows. A loop whose header is not shown, and a variable or a write on a
	line not shown, are left out (and a call whose variable changes only on a line not
	shown is not called invariant); nothing is said when no loop is shown (P12).
	``caller_hint`` adds that a line in no loop may repeat because a caller loops (P7)."""
	if not isinstance(facts, dict) or not facts:
		return ""
	if facts.get("in_loop") is False:
		text = "The marked line is not inside a for loop, while loop or comprehension of its own function."
		if caller_hint:
			text += (
				" The repetition may come from a caller that is not shown, for example a loop in a "
				"function that calls this one."
			)
		return text
	loops = [
		loop for loop in facts.get("loops") or []
		if isinstance(loop, dict) and _shown(loop.get("line"), first_line, last_line)
	]
	if not loops:
		return ""
	sentence = f"The marked line runs inside the {_KIND_WORDS.get(loops[0].get('kind'), 'loop')} on line {loops[0]['line']}"
	for loop in loops[1:]:
		sentence += f", which runs inside the {_KIND_WORDS.get(loop.get('kind'), 'loop')} on line {loop['line']}"
	parts = [sentence + "."]
	call = facts.get("call")
	if call:
		uses = set(facts.get("uses") or [])
		for loop in loops:
			names = sorted({name for name, line in loop.get("bound") or [] if _shown(line, first_line, last_line)} & uses)
			hidden = {name for name, line in loop.get("bound") or [] if not _shown(line, first_line, last_line)} & uses
			where = "that loop" if len(loops) == 1 else f"the loop on line {loop['line']}"
			if names:
				parts.append(f"The call {call} uses variables that change in {where}: {', '.join(names)}.")
			elif not hidden:
				# Only when no used name changes on a line the prompt no longer shows.
				parts.append(f"The call {call} uses no variable that changes in {where}.")
		if facts.get("result_used") is True:
			parts.append(f"The result of {call} is used.")
		elif facts.get("result_used") is False:
			parts.append(f"The result of {call} is not used.")
	scope = "this loop" if len(loops) == 1 else "these loops"
	writes = sorted({name for name, line in loops[-1].get("writes") or [] if _shown(line, first_line, last_line)})
	if writes:
		parts.append(f"Inside {scope} the code also writes through: {', '.join(writes)}.")
	else:
		parts.append(f"The profiler sees no database write inside {scope} in the code shown.")
	return " ".join(parts)


INDEX_TYPE_NOTE = (
	"Index advice is built by Optimus from the DocType metadata, without the AI. "
	"See the recipe on this finding in the report."
)
FRAMEWORK_N1_NOTE = (
	"A Framework N+1 finding points at a loop inside framework code, which your app "
	"cannot change, so Optimus does not ask the AI about it."
)
NOT_ELIGIBLE_NOTE = "This finding type does not carry enough code or SQL context for an AI suggestion."
GATE_CHECK_FAILED_NOTE = (
	"Optimus could not check this Hot Line, so it does not ask the AI about it. Look at what the "
	"line calls and how often it runs."
)

# A line whose Phase 2 time per hit reaches this, and whose statement calls a
# non-builtin, spends its time inside that callee (owner decision A4).
HOT_LINE_CALLEE_US = 1000.0

UNSTAMPED_REDUNDANT_CALL_NOTE = (
	"This Redundant Call finding was analyzed before the callsite fix, so its line may point at "
	"the outer hook call instead of the loop, and an AI suggestion made for it may change the "
	"wrong loop. Optimus no longer asks the AI about it: re-record the flow to get a corrected "
	"finding."
)

_BUILTIN_NAMES: frozenset[str] = frozenset(dir(builtins))
_WRAP = "_optimus_wrap_"
# gettext's ``_`` (Frappe's translate, also called as ``frappe._``), installed into
# builtins by gettext.install: a translation lookup, never the callee that holds a line's
# time; nor is ``.format`` called on the text it returned (a str method).
_GETTEXT_CALLEES: frozenset[str] = frozenset({"_", "frappe._"})
_NO_CALL_HEADER_RE = re.compile(r"^(?:(?:else|finally|try)\s*:|except\b|case\b)")
_OPENERS = {"(": ")", "[": "]", "{": "}"}
_OWN_RECEIVERS = frozenset({"self", "cls", "super"})


def finding_detail(finding: dict) -> dict:
	"""``technical_detail`` of a render dict, or the parsed ``technical_detail_json`` of a
	row-shaped dict; ``{}`` when unreadable."""
	detail = finding.get("technical_detail")
	if isinstance(detail, dict):
		return detail
	try:
		parsed = json.loads(finding.get("technical_detail_json") or "{}")
	except (TypeError, ValueError):
		return {}
	return parsed if isinstance(parsed, dict) else {}


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


def _closers(src: str) -> str:
	"""The brackets that close what ``src`` opened (strings skipped), or "" when ``src``
	ends inside a string."""
	stack: list[str] = []
	quote: str | None = None
	i = 0
	while i < len(src):
		if quote:
			if src[i] == "\\":
				i += 2
				continue
			if src.startswith(quote, i):
				i += len(quote)
				quote = None
				continue
			i += 1
			continue
		if src.startswith(('"""', "'''"), i):
			quote = src[i : i + 3]
			i += 3
			continue
		ch = src[i]
		if ch in ("'", '"'):
			quote = ch
		elif ch in _OPENERS:
			stack.append(_OPENERS[ch])
		elif stack and ch == stack[-1]:
			stack.pop()
		i += 1
	return "" if quote else "".join(reversed(stack))


def _parse_statement(line: str) -> ast.Module | None:
	"""The statement on one physical line, parsed: the line itself, the line with its
	open brackets closed, a block header with a body, or a continuation line wrapped
	in a call (``_WRAP``). None for a header that calls nothing or an unparseable line."""
	src = _strip_comment(line).strip()
	if not src or _NO_CALL_HEADER_RE.match(src):
		return None
	if src.startswith("elif "):
		src = src[2:]
	elif src.startswith("match ") and src.endswith(":"):
		src = src[len("match ") : -1]
	elif src.startswith("@"):
		src = src[1:]
	src = src.lstrip(")]} ")
	closed = src + _closers(src)
	candidates = [src, closed, closed + " pass" if closed.endswith(":") else closed + ": pass"]
	candidates += [f"{_WRAP}({closed.rstrip(',')})", f"{_WRAP}({closed})"]
	for candidate in candidates:
		try:
			return ast.parse(candidate)
		except (SyntaxError, ValueError):
			continue
	return None


def _is_gettext(func: ast.AST) -> bool:
	"""True for ``_`` / ``frappe._`` and for ``.format`` on the text one of them returned."""
	if call_name(func) in _GETTEXT_CALLEES:
		return True
	return (
		isinstance(func, ast.Attribute) and func.attr == "format" and isinstance(func.value, ast.Call)
		and call_name(func.value.func) in _GETTEXT_CALLEES
	)


class StatementCalls(NamedTuple):
	calls: bool
	callee: str | None


def statement_calls(line: str) -> StatementCalls:
	"""Whether the statement on ``line`` calls something other than a Python builtin, and
	the first such callee, outermost first. A ``raise X(...)`` constructor is skipped (only
	the raised exception, never its ``from`` cause), so ``raise X(foo())`` names ``foo``
	and ``raise X(a) from bar(y)`` names ``bar``; gettext's ``_`` (or ``frappe._``) and
	``.format`` on the text it returned are skipped like builtins. A fragment Python
	cannot parse counts as calling, with no name, when it holds a parenthesis."""
	tree = _parse_statement(line)
	if tree is None:
		return StatementCalls("(" in _strip_comment(line), None)
	skip = {
		id(node.exc) for node in ast.walk(tree) if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
	}
	for node in ast.walk(tree):
		if not isinstance(node, ast.Call) or id(node) in skip:
			continue
		if isinstance(node.func, ast.Name) and (node.func.id in _BUILTIN_NAMES or node.func.id == _WRAP):
			continue
		if _is_gettext(node.func):
			continue
		name = call_name(node.func)
		return StatementCalls(True, name if name and _SAFE_NAME_RE.match(name) else None)
	return StatementCalls(False, None)


def _short_path(filename: str) -> str:
	norm = filename.replace("\\", "/")
	if "/apps/" in norm:
		return norm.rsplit("/apps/", 1)[1]
	if norm.startswith("apps/"):
		return norm[len("apps/") :]
	return norm.rsplit("/", 1)[-1]


def _per_hit_us(detail: dict) -> float:
	try:
		value = float(detail.get("per_hit_us") or 0)
	except (TypeError, ValueError):
		return 0.0
	return value if value == value else 0.0  # NaN is not a measurement


def _callee_scope(name: str, *, tracked_apps: tuple[str, ...], from_phase1: bool) -> str:
	"""``"library"`` (stdlib, framework or a known third-party library: Phase 2 cannot
	point at a line of the developer's app inside it), ``"yours"`` (a function of the
	developer's app) or ``"unknown"`` (a method of this class or a parent, a bare name)."""
	root = re.split(r"[.(\[]", name, maxsplit=1)[0]
	if root in _OWN_RECEIVERS:
		return "unknown"
	if (
		root in _BUILTIN_NAMES or root in sys.stdlib_module_names or root in FRAMEWORK_APPS
		or is_framework_callsite(f"{root}/", None, None)
	):
		return "library"
	if tracked_apps and root in tracked_apps:
		return "yours"
	if from_phase1:
		# Phase 1 names only user-code descendants; with Tracked Apps set, any other app is
		# not the developer's.
		return "library" if tracked_apps else "yours"
	return "unknown"


def _callee_note(callee: str | None, scope: str) -> str:
	if not callee:
		return (
			"Most of this line's time is spent inside a function it calls, so Optimus does not ask the AI "
			"about the line itself. Find that call in the source: re-run Phase 2 with it picked if it is in "
			"your app, otherwise call it less often."
		)
	head = (
		f"Most of this line's time is spent inside {callee}, which the line calls, so Optimus does not ask "
		"the AI about the line itself."
	)
	less = "call it less often, for example once before the loop, through a cached value, or in one batched call."
	if scope == "yours":
		return f"{head} Re-run Phase 2 with {callee} picked to see which of its lines is slow, or {less}"
	if scope == "library":
		return (
			f"{head} {callee} is standard library, framework or third-party code, so a Phase 2 run cannot "
			f"point at a line of your app inside it: {less}"
		)
	return f"{head} If {callee} is defined in your app, re-run Phase 2 with it picked to see which of its lines is slow; otherwise {less}"


def hot_line_gate(
	finding: dict,
	*,
	tracked_apps: tuple[str, ...] = (),
	installed_apps: frozenset[str] | None = None,
) -> str | None:
	"""Why a Hot Line must not go to the AI, as a report-ready note, or None.

	Gated when the line sits in framework or library code (the report's own
	``is_framework_callsite``), when Phase 1 named the hot callee, or when Phase 2
	measured ``per_hit_us >= HOT_LINE_CALLEE_US`` and the line's statement calls a
	non-builtin (owner decision A4)."""
	if (finding.get("finding_type") or "") != "Hot Line":
		return None
	detail = finding_detail(finding)
	callsite = detail.get("callsite") if isinstance(detail.get("callsite"), dict) else {}
	filename = str(callsite.get("filename") or detail.get("file") or "")
	if filename and is_framework_callsite(filename, tracked_apps or None, installed_apps):
		return (
			f"This line is in framework or library code ({_short_path(filename)}), which your app "
			"cannot change, so Optimus does not ask the AI about it. Follow the call chain above to "
			"the first line in your own app and reduce how often that path runs."
		)
	hint = detail.get("phase1_hint") if isinstance(detail.get("phase1_hint"), dict) else {}
	named = str(hint.get("next_hot_callee") or "").strip()
	scope_apps = tuple(tracked_apps or ())
	if named:
		return _callee_note(named, _callee_scope(named, tracked_apps=scope_apps, from_phase1=True))
	if _per_hit_us(detail) < HOT_LINE_CALLEE_US:
		return None
	shape = statement_calls(str(detail.get("line_content") or ""))
	if not shape.calls:
		return None
	scope = _callee_scope(shape.callee, tracked_apps=scope_apps, from_phase1=False) if shape.callee else "unknown"
	return _callee_note(shape.callee, scope)


def analyzed_before_callsite_fix(finding: dict) -> bool:
	"""True for a Redundant Call finding (render dict or row-shaped dict) whose technical
	detail lacks the fixed analyzer's callsite stamp."""
	if (finding.get("finding_type") or "") != "Redundant Call":
		return False
	return finding_detail(finding).get(CALLSITE_WALK_KEY) != CALLSITE_WALK_FIXED
