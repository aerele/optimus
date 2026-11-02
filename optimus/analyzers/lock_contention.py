# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Lock-contention analyzer: turns captured deadlocks and lock-wait timeouts into findings.

Pure function over recording dicts (reads ``rec["lock_events"]``): no Frappe, DB or IO.
"""

import json
from collections import Counter

from optimus.analyzers.base import (
	AnalyzerResult,
	humanize_duration_ms,
	installed_apps_allowlist,
	short_filename,
	user_callsite,
)

_MAX_EXAMPLE_QUERIES = 10
_KNOWN_KINDS = frozenset({"deadlock", "lock_wait_timeout"})


def analyze(recordings, context):
	"""One finding per user-code callsite that hit a deadlock or lock-wait timeout."""
	from optimus.settings import get_config

	tracked_apps = get_config().tracked_apps
	installed_apps = installed_apps_allowlist()
	buckets: dict = {}
	truncated = False
	for action_idx, rec in enumerate(recordings):
		for ev in rec.get("lock_events") or []:
			if not isinstance(ev, dict):
				continue
			if ev.get("_truncated"):
				truncated = True
				continue
			if ev.get("kind") not in _KNOWN_KINDS:
				continue
			frame = user_callsite(
				ev.get("caller_stack"), tracked_apps=tracked_apps, installed_apps=installed_apps
			)
			if frame is None:
				continue  # framework-only contention
			key = (frame.get("filename"), frame.get("lineno"))
			bucket = buckets.get(key)
			if bucket is None:
				bucket = buckets[key] = {
					"site": frame, "events": [], "kinds": set(), "actions": Counter(),
				}
			bucket["events"].append(ev)
			bucket["kinds"].add(ev["kind"])
			bucket["actions"][action_idx] += 1

	findings = [_build_finding(bucket) for bucket in buckets.values()]
	warnings = []
	if truncated:
		warnings.append(
			"Lock Contention: a recording hit the per-recording event cap, so "
			"later deadlocks or lock-wait timeouts were not counted."
		)
	return AnalyzerResult(findings=findings, warnings=warnings)


def _build_finding(bucket: dict) -> dict:
	# Point at the recording with the most events (ties: earliest).
	action_idx = min(bucket["actions"].items(), key=lambda kv: (-kv[1], kv[0]))[0]
	events = bucket["events"]
	kinds = bucket["kinds"]
	site = bucket["site"] or {}
	filename = site.get("filename")
	lineno = site.get("lineno")
	function = site.get("function")
	count = len(events)

	# Deadlock wins the label when a callsite hit both kinds.
	has_deadlock = "deadlock" in kinds
	kind_label = "Deadlock" if has_deadlock else "Lock-wait timeout"
	where = f"{short_filename(filename)}:{lineno}" if filename else None

	# Every distinct query at the callsite: one doc.save() can deadlock on several.
	example_queries: list = []
	seen: set = set()
	for e in events:
		q = e.get("normalized_query")
		if q and q not in seen:
			seen.add(q)
			example_queries.append(q)
	example_queries = example_queries[:_MAX_EXAMPLE_QUERIES]
	normalized = example_queries[0] if example_queries else ""
	sample_error = next((e.get("error") for e in events if e.get("error")), "")

	# Wall-time the failed queries burned; None when no event carries a duration.
	durations = [e.get("duration_ms") for e in events if isinstance(e.get("duration_ms"), (int, float))]
	total_wait_ms = round(sum(durations), 2) if durations else None
	max_wait_ms = round(max(durations), 2) if durations else None

	findings_detail = {
		"callsite": (
			{"filename": filename, "lineno": lineno, "function": function}
			if filename
			else {}
		),
		"normalized_query": normalized,
		"example_queries": example_queries,
		"lock_kinds": sorted(kinds),
		"occurrences": count,
		"affected_actions": sorted(bucket["actions"]),
		"total_wait_ms": total_wait_ms,
		"max_wait_ms": max_wait_ms,
		"error": sample_error,
		"fix_hint": _fix_hint(has_deadlock, where),
	}

	return {
		"finding_type": "Lock Contention",
		# A failed write is a reliability problem, so always High.
		"severity": "High",
		"title": _title(kind_label, count, where),
		"customer_description": _description(has_deadlock, count, where, max_wait_ms),
		"technical_detail_json": json.dumps(findings_detail, default=str),
		# No ms to reclaim; the wait is in total_wait_ms / max_wait_ms instead.
		"estimated_impact_ms": 0.0,
		"affected_count": count,
		"action_ref": str(action_idx),
	}


def _title(kind_label: str, count: int, where: str | None) -> str:
	times = f" {count}×" if count > 1 else ""
	if where:
		return f"{kind_label} on a query{times} at {where}"
	return f"{kind_label} on a database query{times}"


def _description(has_deadlock: bool, count: int, where: str | None, max_wait_ms: float | None = None) -> str:
	loc = f" at {where}" if where else ""
	plural = "s" if count > 1 else ""
	wait = ""
	if max_wait_ms and max_wait_ms >= 1.0:
		wait = f" The slowest attempt waited {humanize_duration_ms(max_wait_ms)} before failing."
	if has_deadlock:
		# MariaDB also reports snapshot conflicts as deadlocks, so lock order is only the usual cause.
		return (
			f"A database query{loc} hit a deadlock {count} time{plural} during this "
			"flow. The database aborted the query to break the lock (usually rolling "
			"back the whole transaction), so the write failed or had to be retried. This "
			"usually means two transactions locked the same rows in a different "
			"order, or a row changed underneath the transaction under a strict "
			f"isolation level.{wait}"
		)
	# The MariaDB setting is an example only; Postgres reports timeouts differently.
	return (
		f"A database query{loc} hit a lock-wait timeout {count} time{plural} during "
		"this flow. The query waited for rows another transaction was holding and "
		"gave up once it passed the configured lock-wait timeout "
		"(innodb_lock_wait_timeout on MariaDB/MySQL), so the statement failed. "
		f"This points to a long-held lock or heavy contention on those rows.{wait}"
	)


def _fix_hint(has_deadlock: bool, where: str | None) -> str:
	loc = f" The contended query is at {where}." if where else ""
	if has_deadlock:
		cause = (
			"Deadlocks happen when two transactions lock the same rows in a "
			"different order and each waits on the other."
		)
	else:
		cause = (
			"A lock-wait timeout means this query waited too long for rows another "
			"transaction was holding, then gave up."
		)
	return (
		f"{cause}{loc} To fix: keep write transactions short, update rows in a "
		"consistent order across every code path, add an index so the lock is "
		"held over fewer rows for less time and make the operation safe to "
		"retry. Avoid doing slow work (API calls, large loops) between the first "
		"write and the commit."
	)
