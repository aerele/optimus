# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""True N+1 detection: group by callsite, then by normalized SQL. A variant
recurring at least `n_plus_one_min_occurrences` (a settings value, default 10)
times WITHIN ONE request is a per-row loop — the distinction a bare copy-count
can't make."""

import json
from collections import Counter, defaultdict

from optimus.analyzers.base import (
	FRAMEWORK_PREFIXES,  # noqa: F401  (kept for any external importers)
	SEVERITY_ORDER,
	AnalyzerResult,
	is_framework_callsite,
	percentile,
	short_filename,
	walk_callsite,
)

# v0.7.x M4: surface a P95 readout next to the consolidated total
# when the sample is large enough to be meaningful. P95 of 3 hits
# is statistically meaningless and would mislead more than help.
P95_MIN_SAMPLES = 10

# A group is also required to spend at least this much total time before
# being flagged. Prevents tiny (<1ms each) queries from generating noisy
# findings even when they repeat many times. The per-occurrence
# threshold now lives in Optimus Settings as
# ``n_plus_one_min_occurrences`` (default 10) — see
# ``optimus.settings``.
DEFAULT_MIN_TOTAL_TIME_MS = 20

# Severity heuristics
HIGH_OCCURRENCES = 50
HIGH_TOTAL_TIME_MS = 200
MEDIUM_OCCURRENCES = 20

def _get_min_total_time() -> float:
	try:
		import frappe

		v = frappe.conf.get("optimus_n_plus_one_min_total_ms")
		if v is not None:
			return float(v)
	except Exception:
		pass
	return DEFAULT_MIN_TOTAL_TIME_MS


def analyze(recordings: list[dict], context) -> AnalyzerResult:
	# Single settings read per analyze pass (see redundant_calls for
	# the same pattern).
	from optimus.settings import get_config
	cfg = get_config()
	tracked_apps = cfg.tracked_apps

	# v0.5.2 round 3: group by (filename, lineno) instead of
	# (normalized_query, filename, lineno). A single callsite that
	# generates 10 different query shapes in the same loop (e.g.
	# frappe/query_builder/utils.py:131 resolving DocField / DocPerm
	# / Custom Field metadata within one iteration) was emitting 10
	# separate findings — same fix, 10 rows — which spammed the
	# report. Collapsing at callsite level gives ONE finding per
	# loop with the query variants listed in the detail.
	#
	# Structure: {(filename, lineno): {"variants": {normalized_q:
	# [occurrence]}, "function_name": str}}
	callsite_groups: dict[tuple, dict] = defaultdict(
		lambda: {"variants": defaultdict(list), "function_name": ""}
	)
	min_occurrences = cfg.n_plus_one_min_occurrences
	min_total_time = _get_min_total_time()

	for action_idx, recording in enumerate(recordings):
		for call in recording.get("calls") or []:
			normalized = call.get("normalized_query") or ""
			callsite = walk_callsite(call.get("stack"))
			if not normalized or not callsite:
				continue
			key = (callsite["filename"], callsite["lineno"])
			bucket = callsite_groups[key]
			bucket["variants"][normalized].append({
				"duration": call.get("duration", 0),
				"action_idx": action_idx,
			})
			if not bucket["function_name"]:
				bucket["function_name"] = callsite.get("function") or ""

	findings = []
	for (filename, lineno), bucket in callsite_groups.items():
		variants: dict = bucket["variants"]
		# Total count across ALL variants at this callsite.
		total_count = sum(len(occ) for occ in variants.values())
		# N+1 signal: the MOST-repeated query variant must clear the
		# threshold WITHIN A SINGLE ACTION. An N+1 is a loop inside ONE
		# request/job — counting occurrences across actions would mis-flag a
		# normal per-request query (run once per request × N requests) as a
		# "loop", emitting a confident but wrong "Same query ran N× in a row"
		# at High severity. Mirrors redundant_calls' max_in_any_action guard.
		# If 10 different queries each ran once from this callsite, it's also
		# not an N+1 — it's a fan-out call site (still excluded by the per-
		# variant max).
		max_variant_in_action = max(
			(
				Counter(o["action_idx"] for o in occ).most_common(1)[0][1]
				for occ in variants.values()
				if occ
			),
			default=0,
		)
		if max_variant_in_action < min_occurrences:
			continue

		per_hit_durations = [
			o["duration"]
			for occ in variants.values()
			for o in occ
		]
		total_time = sum(per_hit_durations)

		# For the finding's "canonical" representative, use the query
		# variant with the most occurrences (highest-impact loop).
		top_variant = max(variants.items(), key=lambda kv: len(kv[1]))
		canonical_query, canonical_occurrences = top_variant
		function_name = bucket["function_name"]
		variant_count = len(variants)

		# v0.7.x: multi-variant N+1 ("Callsite ran X queries (N
		# variants) at …") is suppressed. The wording reads as jargon
		# (developers don't think in terms of "variants"), the fix
		# hint is generic, and the dominant variant of the loop is
		# already surfaced elsewhere (top queries, table breakdown).
		# Single-variant N+1 ("Same query ran N× at …") still fires
		# and is the actionable shape.
		if variant_count > 1:
			continue

		# The true "N+1" magnitude is how many times the loop ran WITHIN A
		# SINGLE request — not the cross-request total. A 12-query loop that
		# ran in 5 separate requests is "12× in a row", not "60× in a row":
		# reporting the cross-request total inflated the count, the "…in a
		# row" wording, AND the occurrence-based severity. ``total_count``
		# stays for cumulative cost/averages; ``loop_count`` drives the
		# count-based wording + severity.
		dominant_action_counts = Counter(o["action_idx"] for o in canonical_occurrences)
		# ``action_ref`` must point at the request where the loop actually ran
		# (the dominant action), NOT the first request the query merely appeared
		# in — the report walks THAT action's call tree for inner-loop detail.
		# Taking loop_count from the same most_common(1) keeps the two aligned.
		dominant_action_idx, loop_count = dominant_action_counts.most_common(1)[0]
		action_idx = dominant_action_idx
		# ``run_count`` = requests in which the query actually REPEATED (a real
		# loop to batch), not merely appeared. A query that loops in one request
		# but also runs once in 100 others has run_count == 1, so the "ran in N
		# requests" wording and the post-fix projection don't over-count the
		# single-run requests — those carry no loop to collapse.
		looping_actions = {a for a, c in dominant_action_counts.items() if c >= 2}
		run_count = len(looping_actions)
		# Time + P95 for the finding are scoped to the requests where the query
		# genuinely LOOPED (count >= 2), NOT to a single "busiest" request: a
		# loop that legitimately recurs in 5 requests keeps all 5 in scope, so
		# its cumulative-but-recoverable cost still rates severity honestly.
		# Conversely a tiny 10× loop is not rated High just because the same
		# query, run once each across 100 unrelated requests, sums past the time
		# threshold — that cost is NOT recoverable by batching this loop.
		# ``total_time`` (all occurrences) stays for cumulative reporting.
		loop_occurrences = [o for o in canonical_occurrences if o["action_idx"] in looping_actions]
		loop_time = sum(o["duration"] for o in loop_occurrences)
		loop_durations = [o["duration"] for o in loop_occurrences]

		short_fn = short_filename(filename)
		is_framework = is_framework_callsite(filename, tracked_apps=tracked_apps)

		# Minimum time before we flag, so 10 × 0.1 ms queries aren't reported as an
		# N+1. Gate on the same time each finding type reports: a user finding is
		# rated on the loop's OWN recoverable time (a trivial loop isn't surfaced
		# just because the same query also runs once across many unrelated requests),
		# while a framework finding stays cumulative/informational and gates on the
		# session-wide total_time its text quotes — so a framework callsite with a
		# high cumulative cost but a cheap per-request loop isn't silently dropped.
		gate_time = total_time if is_framework else loop_time
		if gate_time < min_total_time:
			continue

		builder = _build_framework_finding if is_framework else _build_user_finding
		findings.append(builder(
			short_fn=short_fn,
			filename=filename,
			lineno=lineno,
			function_name=function_name,
			normalized=canonical_query,
			total_count=total_count,
			loop_count=loop_count,
			run_count=run_count,
			total_time=total_time,
			loop_time=loop_time,
			per_hit_durations=per_hit_durations,
			loop_durations=loop_durations,
			action_idx=action_idx,
			# v0.5.2 round 3: expose variant list so the detail can
			# show "10 query variants observed" with sample queries.
			all_variants=sorted(
				variants.keys(),
				key=lambda q: -len(variants[q]),
			),
			variant_count=variant_count,
		))

	# Sort: highest severity first, then highest impact within severity
	findings.sort(key=lambda f: (SEVERITY_ORDER.get(f["severity"], 3), -f["estimated_impact_ms"]))

	return AnalyzerResult(findings=findings)


def _severity(count: int, total_time_ms: float) -> str:
	if count >= HIGH_OCCURRENCES or total_time_ms > HIGH_TOTAL_TIME_MS:
		return "High"
	if count >= MEDIUM_OCCURRENCES:
		return "Medium"
	return "Low"


def _title_for_callsite(short_fn, lineno, count) -> str:
	"""'Same query ran N×' — multi-variant callsites are suppressed upstream (analyze())."""
	return f"Same query ran {count}× at {short_fn}:{lineno}"


def _build_user_finding(
	*,
	short_fn: str,
	filename: str,
	lineno,
	function_name: str,
	normalized: str,
	total_count: int,
	loop_count: int,
	run_count: int = 1,
	total_time: float,
	loop_time: float = 0.0,
	per_hit_durations: list[float] | None = None,
	loop_durations: list[float] | None = None,
	action_idx: int,
	all_variants: list[str] | None = None,
	variant_count: int = 1,
) -> dict:
	"""User-code N+1 finding — severity by loop size & the loop's own time, with a fix hint."""
	all_variants = all_variants or [normalized]
	# avg_ms = session-wide average per occurrence (rendered as "~X each" next
	# to the total occurrence count). loop_avg = the loop's own per-query cost,
	# used only for the post-fix projection so unrelated single-run requests
	# don't skew it (falls back to avg_ms when no loop rows were captured).
	avg_ms = total_time / total_count if total_count else 0
	loop_avg = loop_time / len(loop_durations) if loop_durations else avg_ms

	# Cost quoted is the LOOP's own time (loop_time), never the cross-request
	# cumulative total — a query that loops in one request but also runs once
	# elsewhere must not fold those single runs into "the loop".
	tail = (
		"This is usually a Python loop that should fetch its data in one "
		"query instead of one-at-a-time. Typical fix: a few hours of dev work."
	)
	if run_count > 1:
		# The loop spanned several requests; loop_count is the WORST request's
		# count (the peak), not a uniform per-request figure — "up to N" so it
		# isn't read as N × run_count.
		desc = (
			f"We noticed the same query repeated up to {loop_count} times in a row "
			f"from the same line of code ({filename}:{lineno}). That loop ran in "
			f"{run_count} separate requests, costing about {loop_time:.0f}ms. {tail}"
		)
	else:
		# One looping request, so loop_count is exact.
		desc = (
			f"We noticed the same query was repeated {loop_count} times in a row "
			f"from the same line of code ({filename}:{lineno}), costing about "
			f"{loop_time:.0f}ms. {tail}"
		)

	# v0.7.x M4: P95 of the per-hit duration distribution, when the
	# sample is large enough to be meaningful. Gives the user a sense
	# of the tail beyond the average and consolidated total. Scoped to the
	# loop's own hits (not single-run appearances) so it describes the
	# thing being flagged.
	p95_ms = (
		percentile(loop_durations, 95)
		if loop_durations and len(loop_durations) >= P95_MIN_SAMPLES
		else None
	)

	return {
		"finding_type": "N+1 Query",
		# Severity by the per-request loop size (loop_count) and the loop's OWN
		# recoverable time (loop_time) — never the cross-request cumulative
		# total, which single-run requests would inflate.
		"severity": _severity(loop_count, loop_time),
		"title": _title_for_callsite(short_fn, lineno, loop_count),
		"customer_description": desc,
		"technical_detail_json": json.dumps(
			{
				"callsite": {
					"filename": filename,
					"lineno": lineno,
					"function": function_name,
				},
				"normalized_query": normalized,
				# occurrences = total across the session (scope); loop_count =
				# the per-request "in a row" size; run_count = how many
				# requests the loop ran in.
				"occurrences": total_count,
				"loop_count": loop_count,
				"run_count": run_count,
				"variant_count": variant_count,
				"p95_ms": round(p95_ms, 2) if p95_ms is not None else None,
				# Up to 5 sample variants for the detail block —
				# enough to identify the loop, capped so we don't
				# blow out the 140-char title limit / DocType blob.
				"sample_queries": all_variants[:5],
				"total_time_ms": round(total_time, 2),
				"average_time_ms": round(avg_ms, 2),
				# Scope tag for the card's impact box: this finding's estimated_impact_ms
				# is the loop's own recoverable cost, not the session-wide total that
				# every other finding reports as "consolidated".
				"impact_scope_label": "recoverable",
				# v0.5.3: projected post-fix timing. Batching a loop's N
				# queries into ONE collapses that request's cost to roughly a
				# single query. Empirically a batched query with an IN (…)
				# filter or a JOIN costs ~2× a single tight query (same rows
				# scanned once, bigger result set). You still pay that batched
				# query once per LOOPING request, so multiply by run_count —
				# otherwise the projection collapses a whole session to one
				# query and overstates the win. Capped at the current total so
				# a projection can never read as WORSE than doing nothing.
				# Uses the LOOP's own per-query average (loop_avg), not the
				# session-wide avg_ms that single-run requests would skew. Only
				# the loop collapses — any non-loop appearances of this query
				# (total_time - loop_time) keep their cost, so they're added back
				# rather than assumed away. Capped at the current total.
				"projected_total_ms": round(
					min((total_time - loop_time) + loop_avg * 2 * max(run_count, 1), total_time),
					2,
				),
				"projected_avg_time_ms": round(loop_avg * 2, 2),
				"projected_speedup_label": (
					f"~{max(1, loop_count // 2)}× fewer queries" if loop_count >= 4 else None
				),
				"fix_hint": (
					"This is a classic N+1 pattern. The Python code at "
					f"{filename}:{lineno} is running the same query in a loop. "
					"Refactor to fetch all needed data in a single query — for "
					"Frappe specifically, that's usually frappe.get_all() with a "
					"name-IN filter, or a JOIN against the source table — instead "
					"of one row at a time."
				),
			},
			default=str,
		),
		# Headline economics — read by the TL;DR hero (renderer/_internal.py),
		# the card impact box (report.html), the report-wide sort, and the AI-fix
		# prompt (ai_fix.py). Both are scoped to the SAME occurrence set — the loop's
		# own hits (loop_durations) — so the generic `per_hit = impact / count`
		# consumers compute the loop's real per-query cost. estimated_impact_ms is the
		# loop's total time; affected_count is the loop's total hit COUNT (not
		# loop_count, the per-request peak — mixing the two denominators inflated
		# per-hit on multi-request loops). The "in a row" count (loop_count) drives
		# the title/hero; the session-wide totals stay in the detail JSON above.
		"estimated_impact_ms": round(loop_time, 2),
		"affected_count": len(loop_durations),
		"action_ref": str(action_idx),
	}


def _build_framework_finding(
	*,
	short_fn: str,
	filename: str,
	lineno,
	function_name: str,
	normalized: str,
	total_count: int,
	loop_count: int,
	run_count: int = 1,
	total_time: float,
	loop_time: float = 0.0,
	per_hit_durations: list[float] | None = None,
	loop_durations: list[float] | None = None,
	action_idx: int,
	all_variants: list[str] | None = None,
	variant_count: int = 1,
) -> dict:
	"""Framework-level N+1 — always Low, cumulative framing, full technical detail."""
	all_variants = all_variants or [normalized]
	# Framework findings are Low + informational and framed around the
	# CUMULATIVE session cost (the description says "issued N queries in this
	# session"), so the title uses total_count too — keeping title and body on
	# the same number. (The user-code finding, by contrast, is about a
	# per-request loop and uses loop_count.) loop_count/run_count are still
	# carried in the detail JSON below for anyone optimising the framework.
	title = f"Framework query repeated {total_count}× at {short_fn}:{lineno}"

	p95_ms = (
		percentile(per_hit_durations, 95)
		if per_hit_durations and len(per_hit_durations) >= P95_MIN_SAMPLES
		else None
	)

	return {
		"finding_type": "Framework N+1",
		"severity": "Low",
		"title": title,
		"customer_description": (
			f"Frappe's own code at **{filename}:{lineno}** issued "
			f"{total_count} queries in this session, totalling "
			f"{total_time:.0f}ms. This is typically the framework "
			"resolving metadata, permissions, or building queries for "
			"different inputs — it's rarely something you can change "
			"in your application code. Listed here for transparency, "
			"not as an action item. If the cumulative cost is high, "
			"the fix usually lives in the Frappe codebase itself."
		),
		"technical_detail_json": json.dumps(
			{
				"callsite": {
					"filename": filename,
					"lineno": lineno,
					"function": function_name,
				},
				"normalized_query": normalized,
				"occurrences": total_count,
				"loop_count": loop_count,
				"run_count": run_count,
				"variant_count": variant_count,
				"p95_ms": round(p95_ms, 2) if p95_ms is not None else None,
				"sample_queries": all_variants[:5],
				"total_time_ms": round(total_time, 2),
				"average_time_ms": round(total_time / total_count, 2) if total_count else 0,
				"is_framework": True,
				"fix_hint": (
					"This repetition is inside Frappe framework code at "
					f"{filename}:{lineno}. Application developers can "
					"rarely change it. If this is a hot spot in your "
					"profile, consider (1) whether your usage pattern is "
					"triggering unnecessary framework work (e.g. loading "
					"DocType meta in a loop instead of once), (2) whether "
					"a Frappe upgrade has already optimized it, or (3) "
					"raising it as an upstream issue."
				),
			},
			default=str,
		),
		"estimated_impact_ms": round(total_time, 2),
		"affected_count": total_count,
		"action_ref": str(action_idx),
	}
