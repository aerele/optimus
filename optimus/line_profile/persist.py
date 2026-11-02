# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Apply a phase-2 result to a session's Findings table: merge Hot Lines, recheck phase 1."""

from __future__ import annotations

import json

from optimus.analyzers.base import phase2_run_display_status
from optimus.line_profile import recheck
from optimus.line_profile.function_index import build_function_index

_TITLE_MAX = 140
_ROW_FIELDS = ("finding_type", "severity", "estimated_impact_ms", "technical_detail_json")
# Start of the warning a time-budgeted (partial) run carries.
BUDGET_WARNING_PREFIX = "Line profiling was time-budgeted"


def merge_phase2_findings(parent, result) -> None:
	"""Merge result.findings into parent.findings and recheck phase 1; the caller saves."""
	_clamp_titles(result.findings)
	profiled = {
		s.get("dotted_path")
		for s in result.aggregate.get("phase2_functions", [])
		if s.get("hot_lines")  # only functions that actually ran
	}
	_merge_hot_lines(parent, result.findings, profiled)
	try:
		_recheck_phase1_rows(parent)
	except Exception:
		# The recheck is advisory: never fail the whole phase-2 save over it.
		_log_error("optimus phase2 recheck")


def plan_hot_line_merge(existing_keys: list, new_keys: list, profiled_keys: set) -> dict:
	"""Plan a Hot Line merge keyed by dotted_path: update in place, append, or remove duplicates and stale rows."""
	update: dict[int, int] = {}
	remove: list[int] = []
	first_row: dict = {}
	for i, key in enumerate(existing_keys):
		if key is None:
			continue
		if key in first_row:
			remove.append(i)
		else:
			first_row[key] = i
	new_by_key: dict = {}
	append: list[int] = []
	for j, key in enumerate(new_keys):
		if key is not None and key in new_by_key:
			continue
		if key is not None:
			new_by_key[key] = j
		if key in first_row:
			update[first_row[key]] = j
		else:
			append.append(j)
	for key, i in first_row.items():
		if key in profiled_keys and key not in new_by_key:
			remove.append(i)
	return {"update": update, "append": append, "remove": sorted(set(remove))}


def hot_line_key(finding: dict) -> str | None:
	"""The function dotted_path a Hot Line finding is about."""
	return (recheck.load_detail(finding) or {}).get("dotted_path") or None


def _merge_hot_lines(parent, new_findings: list[dict], profiled: set) -> None:
	hot_rows = [r for r in (parent.findings or []) if r.finding_type == "Hot Line"]
	plan = plan_hot_line_merge(
		[hot_line_key(_view(r)) for r in hot_rows],
		[hot_line_key(f) for f in new_findings],
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
	# The run being saved is still Analyzing but already holds results; it counts as Ready.
	ready = [c for c in (parent.phase_2_runs or []) if phase2_run_display_status(c) == "Ready"]
	ready.sort(key=_run_time)  # oldest first, newest wins
	runs, truncated = [], set()
	for child in ready:
		results = _loads(child.results_json)
		if not isinstance(results, list):
			if child.results_json:
				_log("warning", f"optimus phase2 recheck: unreadable results for run {child.run_uuid}")
			continue
		runs.append((child.run_uuid, results))
		if BUDGET_WARNING_PREFIX in (child.warnings_json or ""):
			truncated.add(child.run_uuid)
	index = build_function_index(runs)
	rows = list(parent.findings or [])
	views = [_view(r) for r in rows]
	chains = recheck.collect_call_chains(views)
	stats = recheck.recheck_findings(views, index, chains, frozenset(truncated))
	_log("info", f"optimus phase2 recheck: {stats} from {len(runs)} run(s)")
	changes = [
		(row, key, view[key])
		for row, view in zip(rows, views, strict=True)
		for key in _ROW_FIELDS
		if row.get(key) != view[key]
	]
	_apply_all_or_none(changes)


def _run_time(child):
	# A run still being saved has no ended_at yet; its start time keeps it the newest.
	stamp = child.ended_at or getattr(child, "started_at", None)
	return (stamp is not None, stamp or 0)


def _apply_all_or_none(changes: list) -> None:
	# On a failed write, put back the values already written, then re-raise.
	written = []
	try:
		for row, key, value in changes:
			old = row.get(key)
			row.set(key, value)
			written.append((row, key, old))
	except Exception:
		for row, key, old in reversed(written):
			row.set(key, old)
		raise


def _clamp_titles(findings: list[dict]) -> None:
	# Finding.title is Data(140); phase-2 findings bypass the analyze-time truncation.
	for finding in findings:
		title = finding.get("title") or ""
		if len(title) > _TITLE_MAX:
			finding["title"] = title[: _TITLE_MAX - 3].rstrip() + "..."


def _view(row) -> dict:
	return {key: row.get(key) for key in _ROW_FIELDS}


def _lineno(raw):
	detail = recheck.load_detail({"technical_detail_json": raw})
	return detail.get("lineno") if detail else None


def _loads(raw):
	try:
		return json.loads(raw) if raw else None
	except (TypeError, ValueError):
		return None


def _log(level: str, message: str) -> None:
	try:
		import frappe

		getattr(frappe.logger("optimus"), level)(message)
	except Exception:
		pass


def _log_error(title: str) -> None:
	try:
		import frappe

		frappe.log_error(title=title)
	except Exception:
		pass
