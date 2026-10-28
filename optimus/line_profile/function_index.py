# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""One (file basename, function name) index over phase-2 results, shared by the drilldown and the recheck (pure)."""

from __future__ import annotations

import os


def build_function_index(runs: list[tuple[str, list]]) -> dict:
	"""Map (basename, qualname) and (basename, bare name) to a function entry.

	``runs`` is oldest first: a function seen in several runs keeps its newest result. Distinct functions
	sharing a key keep the one with the larger hot line and the entry is marked ``collides``.
	"""
	latest: dict = {}
	for run_uuid, results in runs:
		for fn in results or []:
			entry = _entry(fn, run_uuid)
			if entry is not None:
				latest[entry["dotted_path"] or (entry["file"], entry["qualname"])] = entry

	index: dict[tuple, dict] = {}
	for entry in latest.values():
		basename = os.path.basename(entry["file"])
		bare = entry["qualname"].rsplit(".", 1)[-1]
		for key in {(basename, entry["qualname"]), (basename, bare)}:
			other = index.get(key)
			if other is None:
				index[key] = entry
				continue
			winner = entry if entry["total_ms"] > other["total_ms"] else other
			index[key] = dict(winner, collides=True)
	return index


def num(value) -> float:
	try:
		return float(value or 0)
	except (TypeError, ValueError):
		return 0.0


def _entry(fn: dict, run_uuid: str) -> dict | None:
	if not isinstance(fn, dict):
		return None
	file_path = fn.get("file") or ""
	dotted = fn.get("dotted_path") or ""
	qualname = fn.get("qualname") or (dotted.rsplit(".", 1)[-1] if dotted else "")
	lines = [ln for ln in (fn.get("lines") or []) if isinstance(ln, dict)]
	if not file_path or not qualname:
		return None
	hot = max(lines, key=lambda ln: num(ln.get("total_ms")), default={})
	return {
		"lineno": hot.get("lineno"),
		"content": hot.get("content") or "",
		"total_ms": num(hot.get("total_ms")),
		"hits": hot.get("hits") or 0,
		"run_uuid": run_uuid,
		"dotted_path": dotted,
		"file": file_path,
		"qualname": qualname,
		"function_total_ms": round(sum(num(ln.get("total_ms")) for ln in lines), 2),
		"invoked": any(num(ln.get("hits")) or num(ln.get("total_ms")) for ln in lines),
	}
