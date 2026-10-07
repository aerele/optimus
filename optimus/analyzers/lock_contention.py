# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Lock-contention analyzer: surfaces DB deadlocks and lock-wait timeouts.

Reads ``rec["lock_events"]`` captured by :mod:`optimus.capture`'s SQL wrapper
(the Frappe recorder only logs queries that SUCCEED, so a query that raised
``frappe.QueryDeadlockError`` / ``frappe.QueryTimeoutError`` is invisible to it).
Emits one High-severity ``Lock Contention`` finding per contended callsite.

Pure function over recording dicts: no Frappe, DB, Redis or IO, so it stays
fixture-testable like every other analyzer.
"""

import json

from optimus.analyzers.base import (
	FRAMEWORK_PREFIXES,
	AnalyzerResult,
	short_filename,
	walk_callsite,
)


def _user_callsite(stack) -> dict | None:
	"""The deepest USER-code frame that issued the query, or None.

	``walk_callsite`` skips framework frames but falls back to the deepest
	frappe/* frame when the whole stack is framework. We treat that fallback
	(and an unresolved / profiler-own stack) as "no user callsite" and drop the
	event: a deadlock with no user frame is either benign framework contention
	Frappe handles itself (a session touch, change-log write or dashboard
	refresh, wrapped in ``savepoint`` / ``suppress``) or something the app
	developer cannot act on. Only user-code contention becomes a finding."""
	frame = walk_callsite(stack)
	if not frame:
		return None
	filename = (frame.get("filename") or "").replace("\\", "/")
	if any(prefix in filename for prefix in FRAMEWORK_PREFIXES):
		return None
	return frame


def analyze(recordings, context):
	"""Bucket user-code lock events by callsite and build one finding each."""
	buckets: dict = {}
	for action_idx, rec in enumerate(recordings):
		for ev in rec.get("lock_events") or []:
			if not isinstance(ev, dict):
				continue
			frame = _user_callsite(ev.get("caller_stack"))
			if frame is None:
				continue  # framework-only / benign contention not a finding
			key = (action_idx, frame.get("filename"), frame.get("lineno"))
			bucket = buckets.get(key)
			if bucket is None:
				bucket = buckets[key] = {"site": frame, "events": [], "kinds": set()}
			bucket["events"].append(ev)
			bucket["kinds"].add(ev.get("kind") or "deadlock")

	findings = [
		_build_finding(action_idx, bucket)
		for (action_idx, _f, _l), bucket in buckets.items()
	]
	return AnalyzerResult(findings=findings)


def _build_finding(action_idx: int, bucket: dict) -> dict:
	events = bucket["events"]
	kinds = bucket["kinds"]
	site = bucket["site"] or {}
	filename = site.get("filename")
	lineno = site.get("lineno")
	function = site.get("function")
	count = len(events)

	# "Deadlock" wins the label when the callsite hit both kinds, since it's the
	# stricter failure (the DB chose a victim and rolled its transaction back).
	has_deadlock = "deadlock" in kinds
	kind_label = "Deadlock" if has_deadlock else "Lock-wait timeout"
	where = f"{short_filename(filename)}:{lineno}" if filename else None

	# First non-empty normalized query across the bucket (all share a callsite).
	normalized = next(
		(e.get("normalized_query") for e in events if e.get("normalized_query")), ""
	)
	sample_error = next((e.get("error") for e in events if e.get("error")), "")

	findings_detail = {
		"callsite": (
			{"filename": filename, "lineno": lineno, "function": function}
			if filename
			else {}
		),
		"normalized_query": normalized,
		"sample_queries": [normalized] if normalized else [],
		"lock_kinds": sorted(kinds),
		"occurrences": count,
		"error": sample_error,
		"fix_hint": _fix_hint(has_deadlock, where),
	}

	return {
		"finding_type": "Lock Contention",
		# Reliability issue: the query failed and its transaction was aborted or
		# retried. Always High it's a correctness/availability problem, not a
		# "this is a bit slow" one, so impact-in-ms doesn't capture its weight
		# (the report sorts severity-first, floating it above timed findings).
		"severity": "High",
		"title": _title(kind_label, count, where),
		"customer_description": _description(has_deadlock, count, where),
		"technical_detail_json": json.dumps(findings_detail, default=str),
		# No measured ms to reclaim: the cost is a failed/retried transaction,
		# not wall-time on the happy path. Severity carries the ranking.
		"estimated_impact_ms": 0.0,
		"affected_count": count,
		"action_ref": str(action_idx),
	}


def _title(kind_label: str, count: int, where: str | None) -> str:
	times = f" {count}×" if count > 1 else ""
	if where:
		return f"{kind_label} on a query{times} at {where}"
	return f"{kind_label} on a database query{times}"


def _description(has_deadlock: bool, count: int, where: str | None) -> str:
	loc = f" at {where}" if where else ""
	plural = "s" if count > 1 else ""
	if has_deadlock:
		return (
			f"A database query{loc} hit a deadlock {count} time{plural} during this "
			"flow. The database picked this transaction as the victim and rolled it "
			"back to break the lock, so the write failed or had to be retried. Two "
			"transactions were trying to lock the same rows in opposite orders."
		)
	return (
		f"A database query{loc} hit a lock-wait timeout {count} time{plural} during "
		"this flow. The query waited for rows another transaction was holding and "
		"gave up once it passed innodb_lock_wait_timeout, so the statement failed. "
		"This points to a long-held lock or heavy contention on those rows."
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
