# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Scan the code an answer renders with the pinned frappe/semgrep-rules.

The blocks are the ones a reader sees (``_answer.code_blocks``: the report's own
rendering). Only code the answer introduces is scanned (ruling R-INTRODUCED, the same
rule as #54's guardrail): a line is introduced unless its whitespace-normalised text is
among the diff's "-" lines (a moved line), or, in a plain block, it is verbatim from the
shown source window (a quoted line); diff context lines are never introduced. A block
with no introduced line produces no unit; a DDL-only sql / bash block counts as code for
the report but has no Python or JavaScript to scan. Each block is written
as a module inside a DocType folder (so DocType-scoped rules apply) twice when it
parses both ways: at module level and inside a Document class, so module-level and
method-level rules both get a fair chance; hits are de-duplicated per source line.
A block that parses neither way is reported as unparsed, never as clean.

Configuration: ``OPTIMUS_SEMGREP_RULES_DIR`` (the ``rules`` directory of a
frappe/semgrep-rules checkout at the pinned commit) and a ``semgrep`` CLI on PATH.
Standard library only.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from dataclasses import dataclass, field

import _answer

RULES_ENV = "OPTIMUS_SEMGREP_RULES_DIR"


@dataclass
class Unit:
	"""One code block as ``(line, is_new)`` pairs."""

	lang: str
	lines: list[tuple[str, bool]]


@dataclass
class ScanResult:
	hits: list[dict] = field(default_factory=list)  # {"rule", "line", "unit"}
	unparsed: int = 0
	units: int = 0


def rules_dir() -> str:
	return os.environ.get(RULES_ENV, "")


def available(rules: str | None = None) -> bool:
	rules = rules if rules is not None else rules_dir()
	return bool(rules) and os.path.isdir(rules) and shutil.which("semgrep") is not None


def units(text: str, source_lines: list[str] | None = None) -> list[Unit]:
	shown = {_answer._norm(ln) for ln in source_lines or () if ln.strip()}
	out = []
	for block in _answer.code_blocks(text):
		if not (block.info in _answer.CODE_LANGS or _answer.is_diff(block)):
			continue
		if _answer.is_diff(block):
			img = _answer.split_diff(block)
			removed = {_answer._norm(ln) for ln, ctx in zip(img.old, img.old_is_context, strict=True) if not ctx}
			pairs = [(ln, is_new and _answer._norm(ln) not in removed) for ln, is_new in img.after]
			lang = "python"
		else:
			pairs = [(ln, _answer._norm(ln) not in shown) for ln in block.body]
			lang = "javascript" if block.info in ("js", "javascript") else "python"
		pairs = [(ln, is_new) for ln, is_new in pairs if not _answer.is_fence_marker(ln)]  # e.g. ``` inside ````
		if any(is_new and ln.strip() for ln, is_new in pairs):
			out.append(Unit(lang=lang, lines=pairs))
	return out


def _variants(unit: Unit) -> list[tuple[str, int]]:
	"""(source, first_line_offset) variants that parse; offset maps a file line back to
	the unit line (file line = unit index + 1 + offset)."""
	code = textwrap.dedent("\n".join(ln.expandtabs(4) for ln, _ in unit.lines))
	if unit.lang != "python":
		return [(code + "\n", 0)]
	header = "import frappe\nfrom frappe.model.document import Document\n\n\n"
	body = textwrap.indent(code, "    ", lambda s: True)
	candidates = [
		(header + code + "\n", header.count("\n")),
		(header + "class OptimusEvalCase(Document):\n" + body + "\n", header.count("\n") + 1),
		(header + "class OptimusEvalCase(Document):\n    def optimus_eval_fragment(self):\n"
			+ textwrap.indent(code, "        ", lambda s: True) + "\n", header.count("\n") + 2),
	]
	ok = []
	for source, offset in candidates:
		try:
			ast.parse(source)
		except SyntaxError:
			continue
		ok.append((source, offset))
		if len(ok) == 2:
			break
	return ok


def scan_texts(
	texts: dict[str, str], rules: str | None = None, sources: dict[str, list[str]] | None = None
) -> dict[str, ScanResult]:
	"""Scan every answer in ``texts`` ({key: markdown}) in one semgrep run; ``sources``
	({key: source window lines}) decides which plain-block lines are quoted, not introduced."""
	rules = rules if rules is not None else rules_dir()
	results = {key: ScanResult() for key in texts}
	files: dict[str, tuple[str, int, Unit, int]] = {}
	with tempfile.TemporaryDirectory(prefix="optimus-ai-eval-") as tmp:
		folder = os.path.join(tmp, "optimus_eval", "optimus_eval", "doctype", "optimus_eval_case")
		os.makedirs(folder)
		for key, text in texts.items():
			for u_idx, unit in enumerate(units(text, (sources or {}).get(key))):
				results[key].units += 1
				variants = _variants(unit)
				if not variants:
					results[key].unparsed += 1
					continue
				for v_idx, (source, offset) in enumerate(variants):
					ext = ".py" if unit.lang == "python" else ".js"
					path = os.path.join(folder, f"case_{len(files)}_{v_idx}{ext}")
					with open(path, "w", encoding="utf-8") as fh:
						fh.write(source)
					files[os.path.realpath(path)] = (key, u_idx, unit, offset)
		if not files:
			return results
		proc = subprocess.run(
			["semgrep", "scan", "--config", rules, "--json", "--quiet", "--metrics=off",
				"--disable-version-check", tmp],
			capture_output=True, text=True, check=False,
		)
		if proc.returncode not in (0, 1):
			raise RuntimeError(f"semgrep failed (exit {proc.returncode}): {proc.stderr[-2000:]}")
		payload = json.loads(proc.stdout or "{}")
		if not isinstance(payload.get("results"), list):
			raise RuntimeError("semgrep returned no results list")
		# Semgrep can return exit 0 with parse errors, timeouts, or skipped files.
		# Count each affected answer block once across its wrapper variants.
		scanned = {os.path.realpath(p) for p in payload.get("paths", {}).get("scanned", [])}
		incomplete = {(key, idx) for path, (key, idx, _unit, _offset) in files.items() if path not in scanned}
		for error in payload.get("errors", []):
			path = error.get("path")
			where = files.get(os.path.realpath(path)) if path else None
			if where is None:
				raise RuntimeError("semgrep reported an unattributed scan error")
			incomplete.add((where[0], where[1]))
		for key, _idx in incomplete:
			results[key].unparsed += 1
	seen = set()
	for res in payload.get("results", []):
		where = files.get(os.path.realpath(res["path"]))
		if where is None:
			continue
		key, u_idx, unit, offset = where
		rule = res["check_id"].rsplit(".", 1)[-1]
		start, end = res["start"]["line"], res["end"]["line"]
		new_lines = [
			i + 1 for i in range(start - 1 - offset, end - offset)
			if 0 <= i < len(unit.lines) and unit.lines[i][1] and unit.lines[i][0].strip()
		]
		if not new_lines:
			continue
		mark = (key, u_idx, rule, new_lines[0])
		if mark in seen:
			continue
		seen.add(mark)
		results[key].hits.append({"rule": rule, "line": new_lines[0], "unit": u_idx})
	return results


def pinned_rule_ids(rules: str | None = None) -> list[str]:
	"""Every rule id in the pinned rules checkout (a line-anchored ``- id:`` read of the
	YAML files; the frappe rules files use that exact shape)."""
	rules = rules if rules is not None else rules_dir()
	ids = []
	for root, _dirs, names in sorted(os.walk(rules)):
		for name in sorted(names):
			if name.endswith((".yml", ".yaml")):
				with open(os.path.join(root, name), encoding="utf-8") as fh:
					ids += re.findall(r"^\s*-\s+id:\s*([A-Za-z0-9_.-]+)\s*$", fh.read(), re.M)
	return ids
