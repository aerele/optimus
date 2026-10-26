# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Recheck phase-1 findings against phase-2 line evidence (pure, no Frappe).

A finding is downgraded one step only when the measured time is under DOWNGRADE_RATIO of its estimate.
"""

from __future__ import annotations

import json
import os

DOWNGRADE_RATIO = 0.25
MAX_RECHECKED_FINDINGS = 500
CALL_CHAIN_CAP = 10
CONTENT_CAP = 200
RECHECK_KEY = "phase2_recheck"

_SEVERITY_STEP_DOWN = {"High": "Medium", "Medium": "Low", "Low": "Low"}


def build_function_index(runs_results: list[list[dict]]) -> dict:
	"""Map (file basename, name) to a function summary; collisions are marked ambiguous."""
	by_dotted: dict[str, dict] = {}
	for results in runs_results:
		for fn in results or []:
			summary = _summarize_function(fn)
			if summary is None:
				continue
			old = by_dotted.get(summary["dotted_path"])
			if old is None or _prefer(summary, old):
				by_dotted[summary["dotted_path"]] = summary

	index: dict[tuple, dict] = {}
	for summary in by_dotted.values():
		base = os.path.basename(summary["file"])
		bare = summary["qualname"].rsplit(".", 1)[-1]
		for key in {(base, summary["qualname"]), (base, bare)}:
			other = index.get(key)
			index[key] = summary if other is None else {"ambiguous": True}
	return index


def collect_call_chains(findings: list[dict]) -> dict[str, list[dict]]:
	"""Map dotted_path to the longest call chain seen in Hot Line findings."""
	chains: dict[str, list[dict]] = {}
	for finding in findings:
		if finding.get("finding_type") != "Hot Line":
			continue
		chain = (_load_detail(finding) or {}).get("call_chain")
		if not isinstance(chain, list):
			continue
		for step in chain:
			path = step.get("dotted_path") if isinstance(step, dict) else None
			if path and (path not in chains or len(chain) > len(chains[path])):
				chains[path] = chain
	return chains


def recheck_findings(findings: list[dict], index: dict, chains: dict | None = None) -> dict:
	"""Recheck non-Hot-Line findings in place (top MAX_RECHECKED_FINDINGS by impact); return counts."""
	stats = {"confirmed": 0, "downgraded": 0, "measured": 0, "not_rechecked": 0, "skipped_cap": 0}
	candidates = [f for f in findings if f.get("finding_type") != "Hot Line"]
	candidates.sort(key=lambda f: -(_num(f.get("estimated_impact_ms"))))
	for position, finding in enumerate(candidates):
		if position >= MAX_RECHECKED_FINDINGS:
			stats["skipped_cap"] += 1
			continue
		status = _recheck_one(finding, index, chains or {})
		if status:
			stats[status] += 1
	return stats


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
	return (_load_detail(finding) or {}).get("dotted_path") or None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _recheck_one(finding: dict, index: dict, chains: dict) -> str | None:
	detail = _load_detail(finding)
	if detail is None:
		# Unparseable legacy payload: leave the row alone.
		return None
	prior = detail.get(RECHECK_KEY) if isinstance(detail.get(RECHECK_KEY), dict) else {}
	base_sev = prior.get("phase1_severity") or finding.get("severity") or "Low"
	base_impact = prior.get("phase1_impact_ms")
	if base_impact is None:
		base_impact = _num(finding.get("estimated_impact_ms"))

	fn, reason = _resolve_function(detail, index)
	blob: dict = {"phase1_severity": base_sev, "phase1_impact_ms": base_impact}
	severity, impact = base_sev, base_impact
	if fn is None:
		status = "not_rechecked"
		blob["reason"] = reason
	else:
		status, severity, impact = _judge(base_sev, base_impact, fn)
		blob.update(_evidence(fn, chains, base_impact))

	blob["status"] = status
	detail[RECHECK_KEY] = blob
	finding["technical_detail_json"] = json.dumps(detail, default=str)
	finding["severity"] = severity
	finding["estimated_impact_ms"] = impact
	return status


def _judge(base_sev: str, base_impact: float, fn: dict) -> tuple[str, str, float]:
	if base_impact <= 0:
		return "measured", base_sev, base_impact
	if fn["total_ms"] < base_impact * DOWNGRADE_RATIO:
		return "downgraded", _SEVERITY_STEP_DOWN.get(base_sev, base_sev), fn["total_ms"]
	return "confirmed", base_sev, base_impact


def _evidence(fn: dict, chains: dict, base_impact: float) -> dict:
	hot = fn["hot_line"]
	chain = (chains.get(fn["dotted_path"]) or [])[:CALL_CHAIN_CAP]
	return {
		"dotted_path": fn["dotted_path"],
		"function_total_ms": fn["total_ms"],
		"hot_line": {
			"lineno": hot.get("lineno"),
			"content": (hot.get("content") or "")[:CONTENT_CAP],
			"total_ms": round(hot.get("total_ms") or 0, 2),
			"hits": hot.get("hits") or 0,
		},
		"call_chain": chain,
	}


def _resolve_function(detail: dict, index: dict) -> tuple[dict | None, str]:
	target = _callsite_target(detail)
	if target is None:
		return None, "no_callsite"
	base, name = os.path.basename(target[0]), target[1]
	entry = index.get((base, name)) or index.get((base, name.rsplit(".", 1)[-1]))
	if entry is None:
		return None, "not_profiled"
	if entry.get("ambiguous"):
		return None, "ambiguous"
	if not entry["invoked"]:
		return None, "not_exercised"
	return entry, ""


def _callsite_target(detail: dict) -> tuple[str, str] | None:
	callsite = detail.get("callsite")
	if isinstance(callsite, dict):
		filename, function = callsite.get("filename"), callsite.get("function")
	else:
		filename, function = detail.get("filename") or detail.get("file"), detail.get("function")
	if isinstance(filename, str) and isinstance(function, str) and filename and function:
		return filename, function
	return None


def _summarize_function(fn: dict) -> dict | None:
	dotted = fn.get("dotted_path") if isinstance(fn, dict) else None
	file_path = fn.get("file") if isinstance(fn, dict) else None
	if not dotted or not file_path:
		return None
	lines = [ln for ln in (fn.get("lines") or []) if isinstance(ln, dict)]
	total = sum(_num(ln.get("total_ms")) for ln in lines)
	invoked = any(_num(ln.get("hits")) or _num(ln.get("total_ms")) for ln in lines)
	hot = max(lines, key=lambda ln: _num(ln.get("total_ms"))) if lines else {}
	return {
		"dotted_path": dotted,
		"qualname": fn.get("qualname") or dotted.rsplit(".", 1)[-1],
		"file": file_path,
		"invoked": invoked,
		"total_ms": round(total, 2),
		"hot_line": hot,
	}


def _prefer(new: dict, old: dict) -> bool:
	return (new["invoked"], new["total_ms"]) > (old["invoked"], old["total_ms"])


def _load_detail(finding: dict) -> dict | None:
	raw = finding.get("technical_detail_json")
	if not raw:
		return {}
	try:
		detail = json.loads(raw)
	except (TypeError, ValueError):
		return None
	return detail if isinstance(detail, dict) else None


def _num(value) -> float:
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0
