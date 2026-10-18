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
from collections import Counter

from optimus.analyzers.base import (
	FRAMEWORK_PREFIXES,
	AnalyzerResult,
	humanize_duration_ms,
	short_filename,
	walk_callsite,
)

# A single contended callsite can fail on several distinct statements; show them
# all but cap so a pathological retry storm can't bloat the finding.
_MAX_EXAMPLE_QUERIES = 10


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


_KNOWN_KINDS = frozenset({"deadlock", "lock_wait_timeout"})


def analyze(recordings, context):
	"""Bucket user-code lock events by callsite across recordings, one finding each."""
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
				continue  # unknown kind: don't guess a label
			frame = _user_callsite(ev.get("caller_stack"))
			if frame is None:
				continue  # framework-only / benign contention not a finding
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

	# "Deadlock" wins the label when the callsite hit both kinds, since it's the
	# stricter failure (the DB chose a victim and rolled its transaction back).
	has_deadlock = "deadlock" in kinds
	kind_label = "Deadlock" if has_deadlock else "Lock-wait timeout"
	where = f"{short_filename(filename)}:{lineno}" if filename else None

	# ALL distinct normalized queries at this callsite, not just the first. One
	# doc.save() can deadlock on several different statements at the same line, so
	# showing only the first would point the developer (and the AI fix prompt) at
	# the wrong table. Stored under ``example_queries`` the key the finding card
	# ("Affected queries") and the AI-fix prompt actually read; ``sample_queries``
	# is rendered nowhere. Capped so a pathological bucket can't bloat the report.
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

	# Wall-time the failed queries burned before raising (a lock-wait timeout can
	# sit for the full innodb_lock_wait_timeout, ~50s). None when no event carries
	# a duration (older recordings / the pure-analyzer fixtures).
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
		# The card renders example_queries; the failed query is not in the recorder.
		# All distinct queries at the callsite (not just the first) so a multi-
		# statement doc.save() points at the right table.
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
		# Reliability issue: the query failed and its transaction was aborted or
		# retried. Always High it's a correctness/availability problem, not a
		# "this is a bit slow" one, so impact-in-ms doesn't capture its weight
		# (the report sorts severity-first, floating it above timed findings).
		"severity": "High",
		"title": _title(kind_label, count, where),
		"customer_description": _description(has_deadlock, count, where, max_wait_ms),
		"technical_detail_json": json.dumps(findings_detail, default=str),
		# No measured ms to RECLAIM on the happy path: the cost is a failed /
		# retried transaction, not wall-time a fix gives back, so this stays 0 and
		# severity carries the ranking. The wait burned is surfaced in the
		# description + technical detail instead (total_wait_ms / max_wait_ms).
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
		# Mechanism is phrased as the COMMON cause, not the only one: the DB layer
		# also reports a snapshot / optimistic-lock conflict (MariaDB err 1020,
		# "record changed since last read") as a deadlock, which is not a classic
		# lock-order cycle, so the old "locking rows in opposite orders" wording
		# over-claimed.
		return (
			f"A database query{loc} hit a deadlock {count} time{plural} during this "
			"flow. The database picked this transaction as the victim and rolled it "
			"back to break the lock, so the write failed or had to be retried. This "
			"usually means two transactions locked the same rows in a different "
			"order, or a row changed underneath the transaction under a strict "
			f"isolation level.{wait}"
		)
	# DB-agnostic: the wait-timeout knob is innodb_lock_wait_timeout on
	# MariaDB/MySQL but the same QueryTimeoutError also covers a Postgres statement
	# timeout, so the description names the MariaDB knob as an example, not a fact
	# about every backend.
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
