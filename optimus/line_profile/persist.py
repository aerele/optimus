# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Apply a phase-2 result to a session's Findings table: merge Hot Lines, recheck phase 1."""

from __future__ import annotations

import json

from optimus.line_profile import recheck

_TITLE_MAX = 140
_ROW_FIELDS = ("finding_type", "severity", "estimated_impact_ms", "technical_detail_json")


def merge_phase2_findings(parent, result) -> None:
	"""Merge result.findings into parent.findings and recheck phase 1; the caller saves."""
	_clamp_titles(result.findings)
	profiled = {
		s.get("dotted_path")
		for s in result.aggregate.get("phase2_functions", [])
		if s.get("hot_lines")  # only functions that actually ran
	}
	_merge_hot_lines(parent, result.findings, profiled)
	_recheck_phase1_rows(parent)


def _merge_hot_lines(parent, new_findings: list[dict], profiled: set) -> None:
	hot_rows = [r for r in (parent.findings or []) if r.finding_type == "Hot Line"]
	plan = recheck.plan_hot_line_merge(
		[recheck.hot_line_key(_view(r)) for r in hot_rows],
		[recheck.hot_line_key(f) for f in new_findings],
		profiled,
	)
	for i, j in plan["update"].items():
		_update_hot_line_row(hot_rows[i], new_findings[j])
	for j in plan["append"]:
		parent.append("findings", new_findings[j])
	gone = {id(hot_rows[i]) for i in plan["remove"]}
	if gone:
		parent.set("findings", [r for r in parent.findings if id(r) not in gone])


def _update_hot_line_row(row, finding: dict) -> None:
	if _lineno(row.technical_detail_json) != _lineno(finding.get("technical_detail_json")):
		row.set("llm_fix_json", None)  # the AI fix was written for a different line
	for key, value in finding.items():
		row.set(key, value)


def _recheck_phase1_rows(parent) -> None:
	runs = [_loads(c.results_json) for c in (parent.phase_2_runs or []) if c.status == "Ready"]
	index = recheck.build_function_index([r for r in runs if isinstance(r, list)])
	rows = list(parent.findings or [])
	views = [_view(r) for r in rows]
	chains = recheck.collect_call_chains(views)
	recheck.recheck_findings(views, index, chains)
	for row, view in zip(rows, views, strict=True):
		for key in _ROW_FIELDS:
			if row.get(key) != view[key]:
				row.set(key, view[key])


def _clamp_titles(findings: list[dict]) -> None:
	# Finding.title is Data(140); phase-2 findings bypass the analyze-time truncation.
	for finding in findings:
		title = finding.get("title") or ""
		if len(title) > _TITLE_MAX:
			finding["title"] = title[: _TITLE_MAX - 3].rstrip() + "..."


def _view(row) -> dict:
	return {key: row.get(key) for key in _ROW_FIELDS}


def _lineno(raw):
	detail = _loads(raw)
	return detail.get("lineno") if isinstance(detail, dict) else None


def _loads(raw):
	try:
		return json.loads(raw) if raw else None
	except (TypeError, ValueError):
		return None
