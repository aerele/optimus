# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Checks over one AI answer (Markdown) as the report renders it, and its source window.

The code a reader sees is taken from the ``<pre>`` elements of the report's own
rendering, ``optimus.renderer.finding_enrichment._markdown_to_safe_html`` (PR-0c's
strict sanitizer; imported lazily), so list-nested fences indented 4+ spaces, indented
code blocks, 4-backtick fences and code under **Verify** all count. A fence the
renderer shows as plain text (a ``~~~`` fence) still counts: its code is on screen.
Deliberately independent of optimus.ai_guardrails: the BEFORE run happens on a develop
that has no guardrail module, and the eval must judge every run the same way.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

_FENCE_OPEN = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>[^`\n]*)$")
_FENCE_ONLY = re.compile(r"^[ \t]*(?:`{3,}|~{3,})[^`\n]*$")
_ESCAPED_PRE = re.compile(r'^<pre><code(?: class="(?P<cls>[^"]*)")?>')
# #54's A3 DDL regex: index DDL counts as code in any block, whatever its info string.
DDL_RE = re.compile(r"\b(?:ALTER\s+TABLE\b[^;]*\bADD\s+(?:UNIQUE\s+)?(?:INDEX|KEY)|CREATE\s+(?:UNIQUE\s+)?INDEX)\b", re.I)
_LINENO_PREFIX = re.compile(r"^\s*\d+:\s?")
_LOOP_HEAD = re.compile(r"^(?:async\s+)?(?:for|while)\b")
_DB_CALL = re.compile(
	r"\bfrappe\.(?:db\.(?:sql|multisql|get_value|get_values|get_all|get_list|exists|count|get_single_value)"
	r"|get_all|get_list|get_doc|get_cached_doc|get_value|qb)\b"
)
_REDUNDANT_CALL = {
	"get_doc": re.compile(r"\bget_(?:cached_)?doc\s*\("),
	"has_permission": re.compile(r"\bhas_permission\s*\("),
	"cache_get": re.compile(r"\b(?:get_cached_value|cache(?:\(\))?\.get_value|cache(?:\(\))?\.hget)\s*\("),
}
CODE_LANGS = {"", "python", "py", "python3", "diff", "js", "javascript"}


@dataclass
class Block:
	info: str
	body: list[str]


@dataclass
class DiffImage:
	"""A diff block split into what it claims exists and what it proposes."""

	old: list[str] = field(default_factory=list)  # context + "-" lines, markers dropped
	old_is_context: list[bool] = field(default_factory=list)
	after: list[tuple[str, bool]] = field(default_factory=list)  # (line, is_new) for context + "+"


def fenced_blocks(text: str) -> list[Block]:
	"""Fenced blocks in the Markdown source at any indent (list-nested fences too), each
	body dedented by its opener's indent. A closer uses the same character and is at
	least as long as the opener, so a ```` fence may contain ``` lines."""
	blocks: list[Block] = []
	lines = (text or "").splitlines()
	i = 0
	while i < len(lines):
		m = _FENCE_OPEN.match(lines[i])
		if not m:
			i += 1
			continue
		fence, width = m.group("fence"), len(m.group("indent"))
		info = m.group("info").strip().split()[0].lower() if m.group("info").strip() else ""
		body: list[str] = []
		i += 1
		while i < len(lines):
			stripped = lines[i].strip()
			if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
				break
			line = lines[i]
			body.append(line[width:] if line[:width].strip() == "" else line.lstrip())
			i += 1
		blocks.append(Block(info=info, body=body))
		i += 1
	return blocks


class _PreText(HTMLParser):
	"""Text of every ``<pre>`` in sanitized HTML as ``(info, text)``; ``info`` is the first
	class of its ``<code>``. The diff highlighter joins lines as ``<span class="dh-line
	...">`` elements, so each such span ends a line."""

	def __init__(self):
		super().__init__(convert_charrefs=True)
		self.blocks: list[tuple[str, str]] = []
		self._depth, self._info, self._buf, self._spans = 0, "", [], []

	def handle_starttag(self, tag, attrs):
		cls = (dict(attrs).get("class") or "").split()
		if tag == "pre":
			if not self._depth:
				self._info, self._buf = "", []
			self._depth += 1
		elif self._depth and tag == "code":
			self._info = cls[0].lower().removeprefix("language-") if cls else ""
		elif self._depth and tag == "span":
			self._spans.append("dh-line" in cls)

	def handle_endtag(self, tag):
		if tag == "pre" and self._depth:
			self._depth -= 1
			if not self._depth:
				self.blocks.append((self._info, "".join(self._buf).replace("\u200b", "")))
		elif self._depth and tag == "span" and self._spans and self._spans.pop():
			self._buf.append("\n")

	def handle_data(self, data):
		if self._depth:
			self._buf.append(data)


def rendered_blocks(text: str) -> list[Block]:
	"""Every ``<pre>`` block of the report's own rendering of ``text``. markdown2 renders a
	fence indented 4+ spaces outside a list as an indented block that shows the fence's
	own ``<pre><code>`` HTML around the code; that wrapper is removed so the code is
	judged, not the markup."""
	from optimus.renderer.finding_enrichment import _markdown_to_safe_html

	parser = _PreText()
	parser.feed(_markdown_to_safe_html(text or ""))
	parser.close()
	blocks = []
	for info, body in parser.blocks:
		lines = body.split("\n")
		while lines and not lines[-1].strip():
			lines.pop()
		m = _ESCAPED_PRE.match(lines[0]) if lines else None
		if m and lines[-1].strip() == "</code></pre>":
			cls = (m.group("cls") or "").split()
			info = cls[0].lower().removeprefix("language-") if cls else ""
			lines = [lines[0][m.end() :], *lines[1:-1]]
		blocks.append(Block(info=info, body=lines))
	return blocks


def _key(lines: list[str]) -> tuple:
	return tuple(_norm(ln) for ln in lines if ln.strip())


def _merge(rendered: list[Block], source: list[Block]) -> list[Block]:
	"""Rendered blocks plus source fences the renderer showed as plain text. A rendered
	block whose language the sanitizer dropped (an unknown info such as ``console``)
	takes its source fence's info, so it is judged by what it is."""
	by_key = {_key(b.body): b for b in source}
	out = []
	for b in rendered:
		src = by_key.get(_key(b.body))
		out.append(Block(info=src.info, body=b.body) if src and src.info and not b.info else b)
	seen = {_key(b.body) for b in rendered}
	return out + [b for b in source if _key(b.body) not in seen]


def is_fence_marker(line: str) -> bool:
	return bool(_FENCE_ONLY.match(line))


def is_diff(block: Block) -> bool:
	if block.info.startswith("diff"):
		return True
	return any(ln.startswith(("+", "-")) and not ln.startswith(("+++", "---")) for ln in block.body[:6])


def code_blocks(text: str) -> list[Block]:
	"""The code blocks a reader sees (see the module docstring): Python / JS / diff blocks,
	and any block holding index DDL (``DDL_RE``) whatever its info string (sql, bash,
	text, ...); a plain SELECT or EXPLAIN block is not code."""
	blocks = _merge(rendered_blocks(text), fenced_blocks(text))
	return [b for b in blocks if b.info in CODE_LANGS or is_diff(b) or DDL_RE.search("\n".join(b.body))]


def has_code(text: str) -> bool:
	"""True when the rendered answer shows at least one code block (the Q1 bar counts
	wrong/harmful/fabricated answers only when they are rendered as code)."""
	return any(any(ln.strip() for ln in b.body) for b in code_blocks(text))


def split_diff(block: Block) -> DiffImage:
	img = DiffImage()
	for raw in block.body:
		if raw.startswith(("+++", "---", "@@")):
			continue
		if raw.startswith("+"):
			img.after.append((raw[1:], True))
		elif raw.startswith("-"):
			img.old.append(_strip_lineno(raw[1:]))
			img.old_is_context.append(False)
		else:
			line = _strip_lineno(raw[1:] if raw.startswith(" ") else raw)
			if line.strip() in ("...", "\u2026"):
				continue
			img.old.append(line)
			img.old_is_context.append(True)
			img.after.append((line, False))
	return img


def _strip_lineno(line: str) -> str:
	return _LINENO_PREFIX.sub("", line, count=1) if _LINENO_PREFIX.match(line) else line


def _norm(line: str) -> str:
	return " ".join(line.split())


def fabricated(text: str, source_lines: list[str]) -> bool:
	"""True when any diff block's context or "-" line is not in the source window
	(whitespace-normalised, line-number prefixes allowed), or when a diff claims
	existing code although no source was shown."""
	shown = {_norm(ln) for ln in source_lines if ln.strip()}
	for block in code_blocks(text):
		if not is_diff(block):
			continue
		for line in split_diff(block).old:
			if line.strip() and _norm(line) not in shown:
				return True
	return False


def _indent(line: str) -> int:
	expanded = line.expandtabs(4)
	return len(expanded) - len(expanded.lstrip())


def _locate(old: list[str], window: list[str]) -> int | None:
	"""Index in ``window`` where the non-blank ``old`` lines start as a contiguous run
	(blank window lines skipped), or None."""
	want = [_norm(x) for x in old if x.strip()]
	if not want:
		return None
	norm = [_norm(x) for x in window]
	for start in range(len(window)):
		j, k = start, 0
		while j < len(window) and k < len(want):
			if not norm[j]:
				j += 1
				continue
			if norm[j] != want[k]:
				break
			j, k = j + 1, k + 1
		if k == len(want):
			return start
	return None


def patched_window(text: str, window: list[str]) -> list[tuple[str, bool]] | None:
	"""The window with the first diff block applied, as ``(line, is_new)`` pairs, or None
	when there is no diff or its old lines cannot be located. Context lines keep the
	window's real text; new lines are re-indented by the offset between the diff and
	the window so loop membership can be read from indentation."""
	block = next((b for b in code_blocks(text) if is_diff(b)), None)
	if block is None:
		return None
	img = split_diff(block)
	start = _locate(img.old, window)
	if start is None:
		return None
	while not window[start].strip():
		start += 1
	old = [(ln, ctx) for ln, ctx in zip(img.old, img.old_is_context, strict=True) if ln.strip()]
	span, j = [], start
	while len(span) < len(old):
		if window[j].strip():
			span.append(window[j])
		j += 1
	context_lines = [real for real, (_, ctx) in zip(span, old, strict=True) if ctx]
	shift = _indent(span[0]) - _indent(old[0][0])
	after: list[tuple[str, bool]] = []
	for line, is_new in img.after:
		if not line.strip():
			after.append(("", is_new))
		elif is_new:
			after.append((" " * max(0, _indent(line) + shift) + line.expandtabs(4).lstrip(), True))
		elif context_lines:
			after.append((context_lines.pop(0), False))
	return [(ln, False) for ln in window[:start]] + after + [(ln, False) for ln in window[j:]]


def _enclosing_loop(lines: list[str], idx: int) -> int | None:
	"""Index of the nearest for/while header whose block contains ``lines[idx]``."""
	level = _indent(lines[idx])
	for i in range(idx - 1, -1, -1):
		if not lines[i].strip():
			continue
		ind = _indent(lines[i])
		if ind < level:
			if _LOOP_HEAD.match(lines[i].strip()):
				return i
			level = ind
	return None


def _loop_body(lines: list[tuple[str, bool]], head: int) -> list[tuple[str, bool]]:
	base = _indent(lines[head][0])
	body = []
	for line, is_new in lines[head + 1 :]:
		if line.strip() and _indent(line) <= base:
			break
		body.append((line, is_new))
	return body


def still_in_loop(case: dict, text: str) -> bool | None:
	"""For N+1 Query and Redundant Call: True when the flagged call (or a DB call that
	replaced it) is still inside the loop after applying the answer's diff; False when
	it left the loop; None when not applicable or not decidable (no diff, fabricated
	diff, the flagged call is not in the window). Pure."""
	ftype = case["finding_type"]
	if ftype not in ("N+1 Query", "Redundant Call"):
		return None
	window = list(case["source_lines"])
	patched = patched_window(text, window)
	if patched is None:
		return None
	first = case["window_first_lineno"]
	if ftype == "N+1 Query":
		target = case["target_lineno"] - first
		head = _enclosing_loop(window, target)
		if head is None:
			return None
		header = window[head]
		heads = [i for i, (ln, _) in enumerate(patched) if ln == header]
		if not heads:
			return False  # the loop itself is gone
		target_text = window[target]
		for line, is_new in _loop_body(patched, heads[0]):
			if (not is_new and line == target_text) or (is_new and _DB_CALL.search(line)):
				return True
		return False
	pattern = _REDUNDANT_CALL.get(case["technical_detail"].get("fn_name") or "")
	if pattern is None:
		return None
	calls = [i for i, ln in enumerate(window) if pattern.search(ln) and _enclosing_loop(window, i) is not None]
	if not calls:
		return None  # the flagged call is outside the window (pre-fix callsite anchors)
	lines = [ln for ln, _ in patched]
	for i, (line, _) in enumerate(patched):
		if line.strip() and pattern.search(line) and _enclosing_loop(lines, i) is not None:
			return True
	return False
