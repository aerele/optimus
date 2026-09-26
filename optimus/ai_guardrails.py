# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Output verification for AI fix suggestions. Pure: no frappe import, no I/O.

``verify_fix`` parses the model's Markdown answer, finds every block the report would
render as code (fences at any indent, ``~~~`` and long fences, indented code blocks,
in any section), walks that code with ``ast`` for Frappe rule violations, checks the
**Fix** diff against the source lines that were actually shown (verbatim grounding)
and returns a list of ``Violation``. ``ai_fix`` re-asks once with ``reask_message(violations)`` and then
``apply_fallback`` strips code that still breaks a rule and appends profiler notes.

Tiers (``Violation.action``):

- ``block``: the answer is wrong, unsafe or fabricated as code (ungrounded or
  no-op diffs, raw SQL or DDL, formatted SQL, dropped permission checks, eval,
  pickle / marshal loads, shell commands, switching to Administrator, manual
  commits, process-wide caches, enqueue without ``enqueue_after_commit``), breaks
  a Frappe rule a pinned semgrep rule checks (translations, whitelist type hints,
  keyword ``orderby``, Single DocType helpers, ``db_set`` in hooks, child rows
  while iterating, module or request-proxy state, an unchecked ``has_permission``),
  or has the wrong shape (headings). One re-ask lists every block rule; code that
  still breaks one is stripped (a heading problem alone never strips code).
- ``truncated``: the answer was cut off. Never re-asked; its code is stripped.
- ``advise``: a convention no pinned semgrep rule checks (a dynamic import, index
  advice led by a metadata column). Never re-asked, never strips code: the
  fallback appends one profiler note.
- ``note``: an operator note (Customize Form, a truncated prompt, removed images,
  off-domain links or echoed data tags). Never re-asked, never strips code.
"""

from __future__ import annotations

import ast
import html
import re
import textwrap
from dataclasses import dataclass

from optimus import ai_prompts
from optimus.analyzers.base import FRAPPE_METADATA_COLUMNS

FIX_HEADINGS = ("Diagnosis", "Fix", "Why it works", "Verify")

BLOCK = "block"
TRUNCATED = "truncated"
ADVISE = "advise"
NOTE = "note"

# Every code verify_fix / ai_fix can emit, with its tier.
CODE_ACTIONS: dict[str, str] = {
	# block: re-asked once, then the code is stripped.
	"ungrounded": BLOCK,
	"no-source-diff": BLOCK,
	"no-op-diff": BLOCK,
	"raw-sql": BLOCK,
	"raw-ddl": BLOCK,
	"sql-format-injection": BLOCK,
	"ignore-permissions": BLOCK,
	"allow-guest": BLOCK,
	"permission-downgrade": BLOCK,
	"eval-exec": BLOCK,
	"unsafe-deserialize": BLOCK,
	"shell-exec": BLOCK,
	"set-user-admin": BLOCK,
	"manual-commit": BLOCK,
	"multitenant-cache": BLOCK,
	"enqueue-without-after-commit": BLOCK,
	"headings": BLOCK,
	# block since D-SEMGREP (owner, plan-check cycle 2): every code a pinned semgrep
	# rule maps to is block.
	"untranslated": BLOCK,
	"whitelist-type-hints": BLOCK,
	"qb-orderby-positional": BLOCK,
	"single-doctype-value": BLOCK,
	"modify-not-saved": BLOCK,
	"child-modify-while-iterating": BLOCK,
	"module-state": BLOCK,
	"local-state": BLOCK,
	"unchecked-permission": BLOCK,
	# truncated: never re-asked (the rewrite would be cut off too); code stripped.
	"truncated": TRUNCATED,
	# advise: a profiler note only; never re-asked, never strips code (no pinned semgrep rule).
	"dynamic-import": ADVISE,
	"metadata-index": ADVISE,
	# note: an operator note; never re-asked, never strips code.
	"customize-form-index": NOTE,
	"context-truncated": NOTE,
	"markdown-image": NOTE,
	"external-link": NOTE,
	"echoed-data-tag": NOTE,
}
CODES: frozenset[str] = frozenset(CODE_ACTIONS)

# A block rule about the answer's format, not its code: it triggers the re-ask and
# a rewrite must fix it to be adopted, but on its own it never strips code.
_FORMAT_ONLY = frozenset({"headings"})


@dataclass(frozen=True)
class Violation:
	code: str
	detail: str = ""
	action: str = ""  # empty = the tier registered in CODE_ACTIONS

	def __post_init__(self):
		if not self.action:
			object.__setattr__(self, "action", CODE_ACTIONS.get(self.code, BLOCK))


def _v(code: str, detail: str = "") -> Violation:
	"""Build a Violation with the tier registered for ``code``."""
	return Violation(code, detail)


# --------------------------------------------------------------- markdown parsing
# A fence at ANY indent: a list-nested fence indented 4 or more spaces still renders
# as code, so it must be checked (and stripped) like a top-level one.
_FENCE_OPEN = re.compile(r"^([ \t]*)(`{3,}|~{3,})\s*([^`\s{]*)[^`]*$")
_INDENTED = re.compile(r"^(?: {4}|\t)")


@dataclass
class Block:
	info: str
	lines: list[str]
	start: int  # line index of the opening fence (first code line of an indented block)
	end: int  # line index of the closing fence (len(lines) when unclosed; last code line of an indented block)
	closed: bool
	fenced: bool = True


def _dedent(line: str, indent: str) -> str:
	"""``line`` without up to ``len(indent)`` leading whitespace characters."""
	n = 0
	while n < len(indent) and n < len(line) and line[n] in " \t":
		n += 1
	return line[n:]


def fenced_blocks(text: str) -> list[Block]:
	"""Fences of ``` or ~~~ (3 or more) at any indent, with an info string that may
	carry attributes (```python {linenos}); the closer is the same character, at least
	as long as the opener, at any indent, with no info string. Body lines lose the
	opener's indent."""
	out: list[Block] = []
	lines = text.splitlines()
	i = 0
	while i < len(lines):
		m = _FENCE_OPEN.match(lines[i])
		if not m:
			i += 1
			continue
		indent, fence, info = m.group(1), m.group(2), (m.group(3) or "").lower()
		closer = re.compile(r"^[ \t]*" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*$")
		j = i + 1
		while j < len(lines) and not closer.match(lines[j]):
			j += 1
		out.append(Block(info, [_dedent(ln, indent) for ln in lines[i + 1 : j]], i, j, j < len(lines)))
		i = j + 1
	return out


def code_blocks(text: str) -> list[Block]:
	"""Candidate code blocks, in text order: the fences above plus indented runs
	(outside any fence, a run of lines indented 4 spaces or a tab that starts the text
	or follows a blank line). The grammar only locates blocks: ``rendered_blocks``
	keeps an indented candidate only when the renderer shows it as code (a 4-space
	list continuation, for example, renders as a paragraph)."""
	fences = fenced_blocks(text)
	inside = {n for b in fences for n in range(b.start, b.end + 1)}
	lines = text.splitlines()
	out = list(fences)
	i = 0
	while i < len(lines):
		starts = (
			i not in inside and lines[i].strip() and _INDENTED.match(lines[i]) and (i == 0 or not lines[i - 1].strip())
		)
		if not starts:
			i += 1
			continue
		j = i
		while j < len(lines) and j not in inside and (not lines[j].strip() or _INDENTED.match(lines[j])):
			j += 1
		k = j
		while not lines[k - 1].strip():
			k -= 1
		body = [ln[4:] if ln.startswith("    ") else ln[1:] if ln.startswith("\t") else ln for ln in lines[i:k]]
		out.append(Block("", body, i, k - 1, True, fenced=False))
		i = j
	return sorted(out, key=lambda b: b.start)


def _rendered_code(text: str) -> list[str]:
	"""The text of every ``<pre>`` block the report renders for ``text``: PR-0c's
	``_markdown_to_safe_html`` is the reference for code a reader sees (ruling
	R-RENDERED). Imported lazily: this module stays pure."""
	from optimus.renderer.finding_enrichment import _markdown_to_safe_html

	out = []
	for body in re.findall(r"<pre\b[^>]*>(.*?)</pre>", _markdown_to_safe_html(text) or "", re.S | re.I):
		body = re.sub(r'<span class="dh-line[^"]*">', "\n", body)  # the diff highlighter: one span per line
		code = html.unescape(re.sub(r"<[^>]+>", "", body)).replace("\u200b", "")
		out.append(re.sub(r"<[^>\n]+>", "", code).strip("\n"))  # tag text escaped inside the block
	return out


def _line_set(lines) -> set[str]:
	return {" ".join(ln.split()) for ln in lines if ln.strip()}


def rendered_blocks(text: str) -> list[Block]:
	"""Every block the report shows as code: fenced candidates, indented candidates the
	renderer shows as ``<pre>``, and one extra block per rendered ``<pre>`` no candidate
	covers (a blockquoted fence, raw ``<pre>`` HTML, an indented run under a heading)."""
	raws = _rendered_code(text)
	pres = [_line_set(r.splitlines()) for r in raws]
	kept = [b for b in code_blocks(text) if b.fenced or any(_line_set(b.lines) <= p for p in pres if p)]
	covered = [_line_set(b.lines) for b in kept]
	extra = [
		Block("", [ln for ln in raw.splitlines() if ln.strip()], -1, -1, True, fenced=False)
		for raw, p in zip(raws, pres, strict=True)
		if p and not any(p <= c for c in covered)
	]
	return kept + extra


def _heading_re(name: str) -> re.Pattern:
	# **Fix**, **Fix**:, **Fix:** at a line start (up to 3 spaces of indent).
	return re.compile(r"^ {0,3}\*\*" + re.escape(name) + r":?\*\*", re.MULTILINE)


def heading_positions(text: str, names) -> dict[str, list[int]]:
	return {n: [m.start() for m in _heading_re(n).finditer(text)] for n in names}


def check_headings(text: str, names=FIX_HEADINGS, optional=()) -> list[Violation]:
	pos = heading_positions(text, names)
	bad = [n for n in names if n not in optional and len(pos[n]) != 1]
	bad += [n for n in optional if len(pos[n]) > 1]
	if not bad:
		order = [pos[n][0] for n in names if pos[n]]
		if order != sorted(order):
			bad.append("order")
		elif text.strip() and not text.lstrip().startswith("**" + names[0]):
			bad.append("text before " + names[0])
	return [_v("headings", ", ".join(bad))] if bad else []


def section(text: str, name: str, names) -> str:
	pos = heading_positions(text, names)
	if not pos.get(name):
		return ""
	start = pos[name][0]
	later = [p for n in names for p in pos[n] if p > start]
	return text[start : min(later) if later else len(text)]


# --------------------------------------------------------------- diff analysis
_LINENO_PREFIX = re.compile(r"^\s*(?:>>\s*)?\d+:\s?")


def _norm(s: str) -> str:
	return " ".join(_LINENO_PREFIX.sub("", s, count=1).split())


def _is_diff(b: Block) -> bool:
	if b.info in ("diff", "patch", "udiff"):
		return True
	body = [ln for ln in b.lines if ln.strip()]
	return bool(body) and all(ln[:1] in "+- @" for ln in body) and any(ln[:1] in "+-" for ln in body)


def diff_rows(b: Block) -> list[tuple[str, str]]:
	"""[(tag, code)] with tag in '+', '-', ' '. Untagged lines count as context."""
	rows = []
	for raw in b.lines:
		if raw.startswith(("+++", "---", "@@")):
			continue
		tag = raw[:1]
		if tag in "+-":
			rows.append((tag, raw[1:]))
		elif tag == " ":
			rows.append((" ", raw[1:]))
		else:
			rows.append((" ", raw))
	return rows


def check_grounding(blocks: list[Block], source_lines: list[str]) -> list[Violation]:
	src = [_norm(s) for s in source_lines]
	out: list[Violation] = []
	for b in blocks:
		if not _is_diff(b):
			continue
		rows = diff_rows(b)
		old = [c for t, c in rows if t in "- " and _norm(c) not in ("", "...", "…")]
		before = [_norm(c) for t, c in rows if t in "- " and _norm(c)]
		after = [_norm(c) for t, c in rows if t in "+ " and _norm(c)]
		if before == after:  # nothing added, removed or moved (a hoist is a move)
			out.append(_v("no-op-diff"))
		if old and not src:
			out.append(_v("no-source-diff"))
			continue
		pos, missing = 0, []
		for c in old:
			try:
				pos = src.index(_norm(c), pos) + 1
			except ValueError:
				missing.append(c.strip()[:60])
		if missing:
			out.append(_v("ungrounded", "; ".join(f"`{m}`" for m in missing[:3])))
	return out


# --------------------------------------------------------------- AST rules
_RAW_SQL = {
	"frappe.db.sql",
	"frappe.db.multisql",
	"frappe.db.sql_list",
	"frappe.db.sql_ddl",
	"frappe.local.db.sql",
	"frappe.local.db.multisql",
}
_CTRL_HOOKS = {"on_submit", "on_cancel", "after_insert", "on_update", "on_update_after_submit"}
_DDL_RE = re.compile(
	r"\b(?:ALTER\s+TABLE\b[^;]*\bADD\s+(?:UNIQUE\s+)?(?:INDEX|KEY)|CREATE\s+(?:UNIQUE\s+)?INDEX)\b", re.I
)
_PERM_CALLS = ("frappe.get_list(", "has_permission(", "check_permission(", "frappe.client.get_list(")
_DESERIALIZE = {"pickle.loads", "pickle.load", "cPickle.loads", "marshal.loads", "dill.loads"}
_SHELL = {"os.system", "os.popen", "os.execv", "os.execl"}
_DYNAMIC_IMPORT = {"__import__", "importlib.import_module"}


def _dotted(n) -> str:
	parts = []
	while isinstance(n, ast.Attribute):
		parts.append(n.attr)
		n = n.value
	if isinstance(n, ast.Name):
		parts.append(n.id)
	elif isinstance(n, ast.Call):
		parts.append(_dotted(n.func) + "()")
	return ".".join(reversed(parts))


def _parse(code: str):
	"""(tree, text_parsed, line_offset) or (None, '', 0). Tries the dedented code,
	then the code wrapped in a def; a hunk ending on a block opener gets a body."""
	code = textwrap.dedent(code.replace("\t", "    "))
	last = next((ln for ln in reversed(code.splitlines()) if ln.strip()), "")
	if last.rstrip().endswith(":"):
		code += "\n" + " " * (len(last) - len(last.lstrip()) + 4) + "pass"
	for cand, off in ((code, 0), ("def _f():\n" + textwrap.indent(code, "    "), 1)):
		try:
			return ast.parse(cand), cand, off
		except SyntaxError:
			pass
	return None, "", 0


def _code_units(blocks: list[Block], source_lines: list[str]):
	"""Yield (after_rows, minus_text) per code block. after_rows = [(code, is_new)]."""
	src = {_norm(s) for s in source_lines}
	for b in blocks:
		if b.info in ("sql", "text", "json", "bash", "shell", "console", "sh"):
			continue
		if _is_diff(b):
			rows = diff_rows(b)
			after = [(c, t == "+") for t, c in rows if t != "-"]
			minus = {_norm(c) for t, c in rows if t == "-"}
		else:  # a plain python block: lines verbatim from the source are context
			after = [(ln, _norm(ln) not in src) for ln in b.lines]
			minus = set()
		yield after, minus


def check_code(blocks: list[Block], source_lines: list[str]) -> list[Violation]:
	out: list[Violation] = []
	for after, minus in _code_units(blocks, source_lines):
		text = "\n".join(c for c, _ in after)
		new_idx = {i for i, (_, new) in enumerate(after) if new}
		if not new_idx:
			continue
		tree, parsed, off = _parse(text)
		if tree is None:  # fallback: line regex over new lines only
			for i in sorted(new_idx):
				c = after[i][0]
				if _norm(c) and _norm(c) in minus:
					continue  # moved line
				if re.search(r"frappe\s*\.\s*(?:local\s*\.\s*)?db\s*\.\s*(?:multi)?sql(?:_list|_ddl)?\s*\(", c):
					out.append(_v("raw-sql", "unparseable block"))
				if re.search(r"frappe\.db\.(?:commit|rollback)\s*\(", c):
					out.append(_v("manual-commit"))
			continue

		def introduced(node, _off=off, _new=new_idx, _after=after, _minus=minus) -> bool:
			start = getattr(node, "lineno", 0) - 1 - _off
			end = getattr(node, "end_lineno", start + 1 + _off) - _off
			# A kept compound statement does not become new when its body changes.
			if isinstance(node, (ast.For, ast.AsyncFor, ast.While, ast.FunctionDef,
					ast.AsyncFunctionDef, ast.ClassDef)):
				end = start + 1
			return any(i in _new and _norm(_after[i][0]) not in _minus
				for i in range(max(0, start), min(len(_after), end)))

		# A kept SQL opener also pays for a safe parameterisation of its arguments.
		prior = "\n".join([*minus, *(c for c, new in after if not new)])
		reshapes = [len(re.findall(r"frappe\.(?:local\.)?db\.(?:multi)?sql\s*\(", prior))]
		aliases = {
			t.id
			for n in ast.walk(tree)
			if isinstance(n, ast.Assign)
			for t in n.targets
			if isinstance(t, ast.Name) and _dotted(n.value) in ("frappe.db", "frappe.local.db")
		}
		for n in ast.walk(tree):
			if isinstance(n, ast.Call):
				name = _dotted(n.func)
				head, _, tail = name.rpartition(".")
				if name in _RAW_SQL or (head in aliases and tail in ("sql", "multisql", "sql_list")):
					if introduced(n):
						a0 = n.args[0] if n.args else None
						if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
							lit = a0.value
						elif isinstance(a0, ast.JoinedStr):
							lit = "".join(
								v.value for v in a0.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
							)
						elif isinstance(a0, ast.Dict):  # frappe.db.multisql({"mariadb": "...", "postgres": "..."})
							lit = " ".join(
								v.value for v in a0.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
							)
						else:
							lit = ""
						unsafe = (
							isinstance(a0, ast.JoinedStr)
							or (isinstance(a0, ast.BinOp) and isinstance(a0.op, (ast.Mod, ast.Add)))
							or (isinstance(a0, ast.Call) and getattr(a0.func, "attr", "") == "format")
						)
						if unsafe:
							out.append(_v("sql-format-injection", name))
						if _DDL_RE.search(lit):
							pass  # raw-ddl comes from check_prose_ddl, which reads every rendered block
						elif reshapes[0] > 0 and not unsafe:
							reshapes[0] -= 1  # a kept `-` SQL reshaped into a parameterised call
						else:
							out.append(_v("raw-sql", name))
				elif name in ("frappe.db.commit", "frappe.db.rollback") and introduced(n):
					out.append(_v("manual-commit"))
				elif name in ("frappe.enqueue", "frappe.enqueue_doc", "enqueue", "enqueue_doc") and introduced(n):
					kws = {k.arg: k.value for k in n.keywords}
					if "enqueue_after_commit" not in kws and not (
						isinstance(kws.get("now"), ast.Constant) and kws["now"].value is True
					):
						out.append(_v("enqueue-without-after-commit", name))
				elif name in ("eval", "exec", "safe_exec", "safe_eval") and introduced(n):
					out.append(_v("eval-exec", name))
				elif name in ("frappe.db.get_value", "frappe.db.set_value") and introduced(n) and len(n.args) >= 2:
					a0, a1 = n.args[0], n.args[1]
					if (isinstance(a1, ast.Constant) and a1.value is None) or ast.dump(a0) == ast.dump(a1):
						if not (
							name.endswith("get_value") and len(n.args) > 2 and isinstance(n.args[2], (ast.List, ast.Tuple))
						):
							out.append(_v("single-doctype-value", name))
				elif tail == "orderby" and introduced(n) and len(n.args) >= 2:
					last = n.args[-1]
					if (isinstance(last, ast.Constant) and str(last.value).lower() in ("asc", "desc")) or _dotted(
						last
					).endswith((".desc", ".asc")):
						out.append(_v("qb-orderby-positional"))
				elif name in _DESERIALIZE and introduced(n):
					out.append(_v("unsafe-deserialize", name))
				elif (name in _SHELL or name.startswith("subprocess.")) and introduced(n):
					out.append(_v("shell-exec", name))
				elif name in _DYNAMIC_IMPORT and introduced(n):
					out.append(_v("dynamic-import", name))
				elif (
					name == "frappe.set_user"
					and introduced(n)
					and n.args
					and isinstance(n.args[0], ast.Constant)
					and n.args[0].value == "Administrator"
				):
					out.append(_v("set-user-admin"))
				for k in n.keywords:
					if (
						k.arg == "ignore_permissions"
						and isinstance(k.value, ast.Constant)
						and k.value.value
						and introduced(n)
					):
						out.append(_v("ignore-permissions"))
					if k.arg == "allow_guest" and isinstance(k.value, ast.Constant) and k.value.value and introduced(n):
						out.append(_v("allow-guest"))
			elif (
				isinstance(n, ast.Expr)
				and isinstance(n.value, ast.Call)
				and introduced(n)
				and _dotted(n.value.func) == "frappe.has_permission"
				and not any(k.arg == "throw" for k in n.value.keywords)
			):
				out.append(_v("unchecked-permission"))
			elif (
				isinstance(n, ast.Assign)
				and introduced(n)
				and any(
					isinstance(t, ast.Attribute) and _dotted(t) == "frappe.flags.ignore_permissions" for t in n.targets
				)
			):
				out.append(_v("ignore-permissions"))
			elif isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef):
				arguments = [*n.args.posonlyargs, *n.args.args, *n.args.kwonlyargs]
				# A kept def line can gain a whitelist decorator or lose an argument
				# annotation on a later line. A body edit alone keeps the signature old.
				signature_changed = introduced(n) or any(introduced(a) for a in arguments)
				for d in n.decorator_list:
					dn = _dotted(d.func if isinstance(d, ast.Call) else d)
					if dn.split(".")[-1] in ("lru_cache", "cache") and introduced(d):
						out.append(_v("multitenant-cache", dn))
					if dn == "frappe.whitelist" and (signature_changed or introduced(d)):
						untyped = [
							a.arg for a in arguments if a.arg not in ("self", "cls") and a.annotation is None
						]
						if untyped:
							out.append(_v("whitelist-type-hints", ", ".join(untyped)))
				if n.name in _CTRL_HOOKS and any(introduced(s) for s in ast.walk(n) if hasattr(s, "lineno")):
					body = ast.dump(n)
					sets = [
						s
						for s in ast.walk(n)
						if isinstance(s, ast.Assign)
						and any(
							isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self"
							for t in s.targets
						)
					]
					if sets and "attr='db_set'" not in body and "attr='save'" not in body:
						out.append(_v("modify-not-saved", n.name))
			elif (
				isinstance(n, ast.For)
				and _dotted(n.iter).startswith("self.")
				and any(
					isinstance(c, ast.Call)
					and _dotted(c.func) in ("self.remove", "self.append")
					and (introduced(n) or introduced(n.iter) or introduced(c))
					for c in ast.walk(n)
				)
			):
				out.append(_v("child-modify-while-iterating"))
		# Module-level state is provable only when the fragment itself shows module
		# scope (a top-level def or class next to the assignment).
		if off == 0 and any(isinstance(st, ast.FunctionDef | ast.ClassDef | ast.AsyncFunctionDef) for st in tree.body):
			for st in tree.body:
				if (
					isinstance(st, ast.Assign)
					and introduced(st)
					and _dotted(getattr(st.value, "func", st.value)).startswith(
						("frappe.get_", "frappe.db.", "frappe.cache", "frappe.qb", "frappe.local", "frappe.conf")
					)
				):
					out.append(_v("module-state", (ast.get_source_segment(parsed, st) or "")[:50]))
		# Hand-rolled state on frappe.local and overrides of the request proxies.
		for n in ast.walk(tree):
			if isinstance(n, ast.Assign | ast.AugAssign) and introduced(n):
				for t in n.targets if isinstance(n, ast.Assign) else [n.target]:
					base = t.value if isinstance(t, ast.Subscript) else t
					d = _dotted(base)
					if d.startswith("frappe.local.") or d in (
						"frappe.db",
						"frappe.qb",
						"frappe.session",
						"frappe.flags",
						"frappe.form_dict",
						"frappe.response",
					):
						out.append(_v("local-state", d))
		# Untranslated user-facing throw / msgprint.
		for n in ast.walk(tree):
			if (
				isinstance(n, ast.Call)
				and _dotted(n.func) in ("frappe.throw", "frappe.msgprint")
				and introduced(n)
				and n.args
				and isinstance(n.args[0], ast.Constant | ast.JoinedStr)
			):
				out.append(_v("untranslated", _dotted(n.func)))
	# Permission downgrade: a permission-bearing call in `-` lines with none after.
	for after, minus in _code_units(blocks, source_lines):
		new_text = " ".join(_norm(c) for c, new in after if new)
		kept_text = " ".join(_norm(c) for c, new in after if not new)
		for p in _PERM_CALLS:
			if any(p in line for line in minus) and p not in new_text and p not in kept_text:
				out.append(_v("permission-downgrade", p.rstrip("(")))
	return out


# --------------------------------------------------------------- index advice
_NEG = re.compile(
	r"\b(?:not|never|avoid|don'?t|no need|already|existing|skip|covered|primary[- ]key|instead of)\b|n't\b", re.I
)
_INDEX_PHRASE = re.compile(
	r"\b(?:add|create|recommend|composite|search)\b[^.\n]{0,40}\bindex\b|\bindex\s+(?:on|for)\b", re.I
)
_TUPLE = re.compile(r"\(\s*`?([A-Za-z_]\w*)`?(?:\s*,\s*`?[A-Za-z_]\w*`?)*\s*\)")
_BACKTICK = re.compile(r"`([A-Za-z_]\w*)`")
_ADD_INDEX = re.compile(r"add_index\(\s*['\"][^'\"]+['\"]\s*,\s*[\[(]\s*['\"]([A-Za-z_]\w*)", re.I)
_DDL_COLS = re.compile(
	r"(?:ADD\s+(?:UNIQUE\s+)?(?:INDEX|KEY)|CREATE\s+(?:UNIQUE\s+)?INDEX)[^(]*?\(\s*`?([A-Za-z_]\w*)", re.I
)
_PROP_SETTER = re.compile(
	r"make_property_setter\(\s*['\"][^'\"]+['\"]\s*,\s*['\"]([A-Za-z_]\w*)['\"]\s*,\s*['\"]search_index", re.I
)


def check_metadata_index(text: str) -> list[Violation]:
	hits: list[str] = []
	for rx in (_ADD_INDEX, _DDL_COLS, _PROP_SETTER):  # code: first column only
		hits += [m.group(1) for m in rx.finditer(text)]
	for sent in re.split(r"(?<=[.!?])\s+|\n", text):  # prose: first column of the advised index
		phrase = _INDEX_PHRASE.search(sent)
		if not phrase or _NEG.search(sent):
			continue
		m = _TUPLE.search(sent[phrase.start() :]) or _BACKTICK.search(sent[phrase.end() :])
		if m:
			hits.append(m.group(1))
	bad = sorted({h.lower() for h in hits if h.lower() in FRAPPE_METADATA_COLUMNS})
	return [_v("metadata-index", ", ".join(f"`{b}`" for b in bad))] if bad else []


_CUSTOMIZE_INDEX = re.compile(
	r"customi[sz]e\s+form\b[^.\n]{0,80}\bsearch\s+index|search\s+index\b[^.\n]{0,80}\bcustomi[sz]e\s+form", re.I
)


def check_customize_form(text: str) -> list[Violation]:
	# Frappe v16 Customize Form has no Search Index option: correct it with a note.
	return [_v("customize-form-index")] if _CUSTOMIZE_INDEX.search(text) else []


def check_prose_ddl(text: str, blocks: list[Block]) -> list[Violation]:
	# Raw DDL in any rendered code block, whatever its info string (bash, console, json,
	# a `bench mariadb -e "ALTER TABLE ..."` line, ...), or in inline code in ``text``.
	for b in blocks:
		if _DDL_RE.search("\n".join(b.lines)):
			return [_v("raw-ddl", "code block")]
	for span in re.findall(r"`([^`\n]+)`", text):
		if _DDL_RE.search(span):
			return [_v("raw-ddl", "inline code")]
	return []


# --------------------------------------------------------------- links, images, echoed tags
# Every repeat is bounded: the reply is capped only by the max_tokens a provider honours,
# and unbounded runs made unclosed "[...](" text rescan the rest of the reply from every
# "[" (quadratic). A link text, URL or title past these bounds is not a real Markdown link;
# the renderer's sanitizer still drops any image and any off-allowlist href.
_MD_LINK = re.compile(
	r"(!?)\[([^\]\n]{0,1000})\]\(\s{0,64}<?([^)\s>]{1,2048})>?(?:\s{1,64}\"[^\"\n]{0,1000}\")?\s{0,64}\)"
)
_AUTOLINK = re.compile(r"<(https?://[^>\s]+)>")
_DATA_TAG = re.compile(r"</?data-[0-9a-f]{6}\b[^>]*>", re.I)


def _link_allowed(url: str) -> bool:
	"""Whether the report keeps this href: PR-0c's public matcher decides (its host
	allowlist), on the entity-decoded href the renderer will see. The note and the
	rendered link differ only in the safe direction: a raw-HTML anchor gets no note
	but the sanitizer drops its off-allowlist href anyway, and a link inside inline
	code gets a note although it never renders as a link. Imported lazily: this
	module stays pure."""
	from optimus.renderer.finding_enrichment import ai_link_allowed

	return ai_link_allowed(html.unescape(url))


def check_markup(text: str) -> list[Violation]:
	out: list[Violation] = []
	for m in _MD_LINK.finditer(text):
		if m.group(1):
			out.append(_v("markdown-image"))
		elif not _link_allowed(m.group(3)):
			out.append(_v("external-link", m.group(3)[:60]))
	for m in _AUTOLINK.finditer(text):
		if not _link_allowed(m.group(1)):
			out.append(_v("external-link", m.group(1)[:60]))
	if _DATA_TAG.search(text):
		out.append(_v("echoed-data-tag"))
	return out


def _neutralise_markup(text: str) -> str:
	"""Drop images and strip echoed data tags. Links stay as written: PR-0c's sanitizer
	drops an href outside its allowlist when the report renders; the note says so."""
	text = _MD_LINK.sub(lambda m: m.group(2) if m.group(1) else m.group(0), text)
	return _DATA_TAG.sub("", text)


# --------------------------------------------------------------- entry points
def _truncation(blocks: list[Block], finish_reason: str | None) -> list[Violation]:
	if (finish_reason or "").lower() in ("length", "max_tokens"):
		return [_v("truncated", "output limit")]
	if blocks and not blocks[-1].closed:
		return [_v("truncated", "unclosed code fence")]
	return []


def verify_fix(text: str, *, source_lines: list[str], finish_reason: str | None = None) -> list[Violation]:
	"""Every rule the fix answer ``text`` breaks, given the source lines that were
	shown to the model. A truncated answer returns only the truncation violation."""
	text = text or ""
	blocks = fenced_blocks(text)
	out = _truncation(blocks, finish_reason)
	if out:
		return out  # never re-ask a cut-off answer: the re-ask would be cut off too
	out += check_headings(text)
	fix = section(text, "Fix", FIX_HEADINGS) or text
	out += check_grounding(code_blocks(fix), source_lines)  # the diff: **Fix** only
	every = rendered_blocks(text)  # every block the report renders as code, any section
	out += check_code(every, source_lines)  # lines verbatim from the shown source are context
	out += check_prose_ddl(fix, every)
	out += check_metadata_index(text)
	out += check_customize_form(text)
	out += check_markup(text)
	return _dedupe(out)



def _dedupe(vs: list[Violation]) -> list[Violation]:
	"""One violation per code (the first detail wins), so the stored code list and the
	re-ask carry each rule once."""
	seen, out = set(), []
	for v in vs:
		if v.code not in seen:
			seen.add(v.code)
			out.append(v)
	return out


def reaskable(violations: list[Violation]) -> list[Violation]:
	"""The block violations: the only ones a re-ask lists and adoption counts."""
	return [v for v in violations if v.action == BLOCK]


def strips_code(violations: list[Violation]) -> bool:
	"""Whether ``apply_fallback`` removes the answer's code: a residual block rule
	about the code, or a truncated answer."""
	return any(
		v.action == TRUNCATED or (v.action == BLOCK and v.code not in _FORMAT_ONLY) for v in violations
	)


def _rule_line(v: Violation) -> str:
	tmpl = ai_prompts.RULE_TEXT.get(v.code, v.code)
	if v.detail:
		return tmpl.replace("{detail}", v.detail)
	return re.sub(r"\s*\(\{detail\}\)|\{detail\}", "", tmpl)


def reask_message(violations: list[Violation]) -> str:
	"""The single combined re-ask: header, one rule line per block violation, footer."""
	lines = ["- " + _rule_line(v) for v in reaskable(violations)]
	return ai_prompts.REASK_HEADER + "\n".join(lines) + ai_prompts.REASK_FOOTER


_CODE_REMOVED = "_(code removed, see the profiler note below)_"


def strip_code(text: str, blocks: list[Block] | None = None) -> str:
	"""Replace every located code block (default: all ``code_blocks`` candidates) with
	one placeholder line; blocks the grammar cannot locate are left to
	``_neutralise_rendered_code``."""
	lines = text.splitlines()
	for b in reversed([b for b in (code_blocks(text) if blocks is None else blocks) if b.start >= 0]):
		lines[b.start : b.end + 1] = [_CODE_REMOVED]
	return "\n".join(lines)


def _code_key(line: str) -> str:
	# a line's text without tags, markup and spacing, so a source line matches its rendering
	return re.sub(r"<[^>]+>|[\s`*_]", "", html.unescape(line))


def _neutralise_rendered_code(text: str, unlocated: list[list[str]] = ()) -> str:
	"""After ``strip_code``: the code the grammar could not locate (``unlocated``: a
	blockquoted fence, raw ``<pre>`` HTML, an indented run under a heading) and any
	``<pre>`` the report would still render become the placeholder; blockquote markers
	and indents are dropped, raw ``<pre>`` / ``<code>`` / ``<details>`` escaped and the
	fences that exposes stripped. Last resort (never reached by the tested shapes): the
	body becomes the placeholder. The result renders no ``<pre>``."""
	raws = [raw.splitlines() for raw in _rendered_code(text)]
	if not raws and not unlocated:
		return text
	code = {_code_key(ln) for block in [*unlocated, *raws] for ln in block} - {""}
	lines: list[str] = []
	for ln in text.splitlines():
		ln = re.sub(r"^\s*(?:>\s?)+", "", ln).lstrip()
		if _code_key(ln) in code:
			if lines and lines[-1] == _CODE_REMOVED:
				continue
			ln = _CODE_REMOVED
		lines.append(ln)
	text = re.sub(r"<(/?)(pre|code|details)\b", r"&lt;\1\2", "\n".join(lines), flags=re.I)
	text = strip_code(text)
	return _CODE_REMOVED if _rendered_code(text) else text


def apply_fallback(text: str, violations: list[Violation]) -> str:
	"""Final answer: strip the code when a block rule about the code still holds (or
	the answer was cut off), add one note listing the advise rules when the code is
	kept, neutralise links, images and echoed data tags, and append one profiler
	note per problem."""
	codes = {v.code for v in violations}
	notes: list[str] = []
	stripped = strips_code(violations)
	if stripped:
		blocks = rendered_blocks(text)
		text = _neutralise_rendered_code(strip_code(text, blocks), [b.lines for b in blocks if b.start < 0])
		if "truncated" in codes:
			why = "was cut off at the output limit."
		else:
			rules = [_rule_line(v) for v in violations if v.action == BLOCK and v.code not in _FORMAT_ONLY]
			why = "broke these Frappe rules: " + " ".join(rules[:2])
			if len(rules) > 2:
				why += f" And {len(rules) - 2} more; see docs/AI-FIXING.md, Guardrail tiers."
		notes.append(f"the suggested code was removed because it {why} Treat the fix as a direction only.")
	advise = [v for v in violations if v.action == ADVISE and v.code != "metadata-index"]
	if advise and not stripped:
		notes.append("change the suggested code before you apply it: " + " ".join(_rule_line(v) for v in advise))
	if "metadata-index" in codes:
		cols = next(v.detail for v in violations if v.code == "metadata-index")
		notes.append(f"ignore any advice to index {cols}: Frappe writes these on every save or already indexes them.")
	if codes & {"markdown-image", "external-link", "echoed-data-tag"}:
		text = _neutralise_markup(text)
	for code in sorted(codes):
		if CODE_ACTIONS.get(code) == NOTE:
			notes.append(ai_prompts.RULE_TEXT[code])
	for n in notes:
		text = text.rstrip() + "\n\n> **Profiler note:** " + n
	return text
