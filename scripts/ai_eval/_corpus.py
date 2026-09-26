# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The committed AI eval corpus and the production-shaped finding builder.

The corpus is optimus/tests/fixtures/ai_corpus_2026-09-09.json: 15 real findings from
a captured optimus.local test flow, the answers qwen3-coder:30b gave on 2026-09-09, a
+-40-line source window per finding (framework lines masked, see ``is_masked_line``) and
the owner's label per stored answer.

``build_finding`` never re-implements the payload. It hands a finding-row-shaped object
and a pre-filled source cache to ``optimus.analyze._ai_payload_for_finding``, the same
function the product's refresh path uses, so the eval sends exactly what the product
sends. When a later PR changes what that function needs, the offline test in
optimus/tests/test_ai_eval_corpus.py fails and this module is updated in the same PR.

Standard library only at import time; ``build_finding`` imports optimus lazily.
"""

from __future__ import annotations

import json
import os
import re
from types import SimpleNamespace

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE_PATH = os.path.join(REPO_ROOT, "optimus", "tests", "fixtures", "ai_corpus_2026-09-09.json")
LABELS = ("correct", "safe-directional", "wrong", "harmful")
_MASKED_RE = re.compile(r"^[ \t]*x+$")


def load_corpus(path: str = FIXTURE_PATH) -> dict:
	with open(path, encoding="utf-8") as fh:
		return json.load(fh)


def cases(corpus: dict | None = None) -> list[dict]:
	return list((corpus or load_corpus())["cases"])


def case_by_name(name: str, corpus: dict | None = None) -> dict:
	for case in cases(corpus):
		if case["name"] == name:
			return case
	raise KeyError(name)


def is_masked_line(line: str) -> bool:
	"""True for a framework line the fixture masked (indent + a run of "x")."""
	return bool(_MASKED_RE.match(line))


def window_rows(case: dict, source_lines: list[str] | None = None) -> list[dict]:
	"""The fixture window as ``[{lineno, content, is_target}]``."""
	first = case["window_first_lineno"]
	lines = case["source_lines"] if source_lines is None else source_lines
	return [
		{"lineno": first + i, "content": line, "is_target": first + i == case["target_lineno"]}
		for i, line in enumerate(lines)
	]


def callsite_filename(case: dict) -> str:
	"""The filename the product reads source for: the callsite, or detail.file for Hot Lines."""
	detail = case["technical_detail"]
	return (detail.get("callsite") or {}).get("filename") or detail.get("file") or ""


def file_lines(case: dict, source_lines: list[str] | None = None) -> list[str]:
	"""A whole-file-shaped line list: the window at its real line numbers, "" elsewhere."""
	lines = case["source_lines"] if source_lines is None else source_lines
	return [""] * (case["window_first_lineno"] - 1) + list(lines)


def build_child(case: dict) -> SimpleNamespace:
	"""An object shaped like an ``Optimus Finding`` child row."""
	return SimpleNamespace(
		name=case["name"],
		finding_type=case["finding_type"],
		severity=case["severity"],
		title=case["title"],
		customer_description=case["customer_description"],
		estimated_impact_ms=case["estimated_impact_ms"],
		affected_count=case["affected_count"],
		action_ref=case.get("action_ref") or "",
		technical_detail_json=json.dumps(case["technical_detail"]),
		llm_fix_json=None,
	)


def phase2_index(case: dict) -> dict:
	"""The ``{(basename, function): hotline}`` index the refresh path passes, for this case."""
	hot = case.get("phase2_hotline")
	callsite = case["technical_detail"].get("callsite") or {}
	fn = (callsite.get("function") or "").strip()
	if not hot or not fn:
		return {}
	base = callsite_filename(case).replace("\\", "/").rsplit("/", 1)[-1]
	return {(base, fn): dict(hot)}


def build_finding(case: dict, *, source_lines: list[str] | None = None) -> dict:
	"""The dict the product hands ``ai_fix.suggest_fix`` for this finding on the refresh
	path (``analyze._run_ai_backfill``: phase-2 index, no recordings)."""
	from optimus import analyze

	file_cache = {callsite_filename(case): file_lines(case, source_lines)}
	return analyze._ai_payload_for_finding(build_child(case), file_cache, phase2_index=phase2_index(case))


def hydrate(case: dict, apps_dir: str) -> tuple[list[str], list[str]]:
	"""Fill masked framework lines from the local bench's source.

	Returns ``(lines, drift)``. A masked line is replaced only when the disk line has the
	same indent and length (the mask preserves both); an unmasked fixture line must equal
	the disk line. Any mismatch is reported in ``drift`` and the fixture lines are returned
	unchanged, so a drifted framework file never silently changes what the model sees.
	"""
	lines = list(case["source_lines"])
	if not case.get("masked"):
		return lines, []
	path = os.path.join(apps_dir, case["source_file"])
	try:
		with open(path, encoding="utf-8") as fh:
			disk = fh.read().splitlines()
	except OSError as exc:
		return lines, [f"cannot read {case['source_file']}: {type(exc).__name__}"]
	first = case["window_first_lineno"]
	out, drift = [], []
	for i, line in enumerate(lines):
		n = first + i
		real = disk[n - 1] if n - 1 < len(disk) else None
		if real is None:
			drift.append(f"line {n} missing on disk")
		elif is_masked_line(line):
			indent = real[: len(real) - len(real.lstrip())]
			if len(real) != len(line) or not line.startswith(indent) or line[len(indent) :].strip("x"):
				drift.append(f"line {n} changed shape")
			out.append(real)
			continue
		elif real != line:
			drift.append(f"line {n} differs")
		out.append(line if real is None else real)
	return (lines, drift) if drift else (out, [])
