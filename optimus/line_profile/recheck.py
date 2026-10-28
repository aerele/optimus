# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Recheck phase-1 findings against phase-2 line evidence (pure, no Frappe).

A finding is downgraded one step only when the measured time is under DOWNGRADE_RATIO of its estimate;
its ``estimated_impact_ms`` stays the phase-1 estimate and the measured time is stored beside it.
"""

from __future__ import annotations

import json
import os

from optimus.line_profile.function_index import num as _num

DOWNGRADE_RATIO = 0.25
MAX_RECHECKED_FINDINGS = 500
CALL_CHAIN_CAP = 10
CONTENT_CAP = 200
RECHECK_KEY = "phase2_recheck"

_SEVERITY_STEP_DOWN = {"High": "Medium", "Medium": "Low"}
# Phase-1 count fields: an estimate summed over many calls is not comparable with one run's total.
_MULTI_CALL_KEYS = ("occurrences", "count", "call_count", "affected_count")


def collect_call_chains(findings: list[dict]) -> dict[str, list[dict]]:
	"""Map dotted_path to the longest call chain seen in Hot Line findings."""
	chains: dict[str, list[dict]] = {}
	for finding in findings:
		if finding.get("finding_type") != "Hot Line":
			continue
		chain = (load_detail(finding) or {}).get("call_chain")
		if not isinstance(chain, list):
			continue
		for step in chain:
			path = step.get("dotted_path") if isinstance(step, dict) else None
			if path and (path not in chains or len(chain) > len(chains[path])):
				chains[path] = chain
	return chains


def recheck_findings(
	findings: list[dict], index: dict, chains: dict | None = None, truncated_runs: frozenset = frozenset()
) -> dict:
	"""Recheck non-Hot-Line findings in place (top MAX_RECHECKED_FINDINGS by impact); return counts."""
	stats = {"confirmed": 0, "downgraded": 0, "measured": 0, "not_rechecked": 0}
	candidates = [f for f in findings if f.get("finding_type") != "Hot Line"]
	candidates.sort(key=lambda f: -(_num(f.get("estimated_impact_ms"))))
	for position, finding in enumerate(candidates):
		skip = "cap" if position >= MAX_RECHECKED_FINDINGS else None
		status = _recheck_one(finding, index, chains or {}, truncated_runs, skip)
		if status:
			stats[status] += 1
	return stats


def load_detail(finding: dict) -> dict | None:
	"""The finding's technical detail dict; {} when empty, None when unparseable."""
	raw = finding.get("technical_detail_json")
	if not raw:
		return {}
	try:
		detail = json.loads(raw)
	except (TypeError, ValueError):
		return None
	return detail if isinstance(detail, dict) else None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _recheck_one(finding: dict, index: dict, chains: dict, truncated: frozenset, skip: str | None) -> str | None:
	detail = load_detail(finding)
	if detail is None:
		# Unparseable legacy payload: leave the row alone.
		return None
	prior = detail.get(RECHECK_KEY) if isinstance(detail.get(RECHECK_KEY), dict) else {}
	base_sev = prior.get("phase1_severity") or finding.get("severity") or "Low"
	base_impact = _num(prior.get("phase1_impact_ms", finding.get("estimated_impact_ms")))

	fn, reason = (None, skip) if skip else _resolve_function(detail, index)
	blob: dict = {"phase1_severity": base_sev, "phase1_impact_ms": base_impact}
	severity = base_sev
	if fn is None:
		status = "not_rechecked"
		blob["reason"] = reason
	else:
		status, severity = _judge(base_sev, base_impact, fn, detail, truncated)
		blob.update(_evidence(fn, chains))

	blob["status"] = status
	detail[RECHECK_KEY] = blob
	finding["technical_detail_json"] = json.dumps(detail, default=str)
	finding["severity"] = severity
	finding["estimated_impact_ms"] = base_impact
	return status


def _judge(base_sev: str, base_impact: float, fn: dict, detail: dict, truncated: frozenset) -> tuple[str, str]:
	if base_impact <= 0 or fn["run_uuid"] in truncated or _is_multi_call(detail):
		return "measured", base_sev
	if fn["function_total_ms"] < base_impact * DOWNGRADE_RATIO and base_sev in _SEVERITY_STEP_DOWN:
		return "downgraded", _SEVERITY_STEP_DOWN[base_sev]
	return "confirmed", base_sev


def _is_multi_call(detail: dict) -> bool:
	return any(_num(detail.get(key)) > 1 for key in _MULTI_CALL_KEYS)


def _evidence(fn: dict, chains: dict) -> dict:
	chain = (chains.get(fn["dotted_path"]) or [])[:CALL_CHAIN_CAP]
	return {
		"dotted_path": fn["dotted_path"],
		"function_total_ms": fn["function_total_ms"],
		"hot_line": {
			"lineno": fn["lineno"],
			"content": fn["content"][:CONTENT_CAP],
			"total_ms": round(fn["total_ms"], 2),
			"hits": fn["hits"],
		},
		"call_chain": chain,
	}


def _resolve_function(detail: dict, index: dict) -> tuple[dict | None, str]:
	target = _callsite_target(detail)
	if target is None:
		return None, "no_callsite"
	filename, name = target
	base = os.path.basename(filename)
	entry = index.get((base, name))
	if entry is None:
		entry = index.get((base, name.rsplit(".", 1)[-1]))
		# Both qualified but different (Bar.process vs Foo.process): another function.
		if entry is not None and "." in name and "." in entry["qualname"] and name != entry["qualname"]:
			entry = None
	if entry is None:
		return None, "not_profiled"
	if entry.get("collides"):
		return None, "ambiguous"
	if not _same_package_dir(filename, entry["file"]):
		return None, "not_profiled"
	if not entry["invoked"]:
		return None, "not_exercised"
	return entry, ""


def _same_package_dir(callsite_file: str, profiled_file: str) -> bool:
	"""Whether the parent directory names agree; a bare filename has no directory to compare."""
	wanted = os.path.basename(os.path.dirname(callsite_file.rstrip("/")))
	return not wanted or wanted == os.path.basename(os.path.dirname(profiled_file))


def _callsite_target(detail: dict) -> tuple[str, str] | None:
	callsite = detail.get("callsite")
	if isinstance(callsite, dict):
		filename, function = callsite.get("filename"), callsite.get("function")
	else:
		filename, function = detail.get("filename") or detail.get("file"), detail.get("function")
	if isinstance(filename, str) and isinstance(function, str) and filename and function:
		return filename, function
	return None
