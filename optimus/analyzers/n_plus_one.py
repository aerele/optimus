# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""True N+1 detection: group by callsite, then by normalized SQL. A variant
recurring at least `n_plus_one_min_occurrences` (a settings value, default 10)
times WITHIN ONE request is a per-row loop — the distinction a bare copy-count
can't make."""

import json
from collections import Counter, defaultdict
from dataclasses import dataclass

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


@dataclass(frozen=True)
class _LoopScope:
	"""One N+1 callsite's scope numbers, passed to the builders as one arg. ``total_*``
	is session-cumulative (every occurrence); ``loop_*`` is scoped to the requests
	that actually looped (≥ min_occurrences)."""

	total_count: int  # every occurrence of the query this session
	loop_count: int  # peak repeats within a single request (the "N" in N+1)
	run_count: int  # how many requests the query actually looped in
	total_time: float  # cumulative time across all occurrences
	loop_time: float  # time in the looping requests only (recoverable)
	per_hit_durations: list  # every occurrence's duration (framework P95)
	loop_durations: list  # looping-request durations (user P95 + projection)


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
	# Clamp to >= 2: a "loop" needs at least two repeats, and loop_count is
	# always >= 1, so a misconfigured 0/1 would defeat the within-request gate
	# (line ``loop_count < min_occurrences``) and re-admit the cross-request
	# false positive the analyzer exists to prevent.
	min_occurrences = max(2, cfg.n_plus_one_min_occurrences)
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
		# v0.7.x: multi-variant N+1 ("Callsite ran X queries (N variants) at …")
		# is suppressed — the wording reads as jargon (developers don't think in
		# "variants"), the fix hint is generic, and the dominant variant is already
		# surfaced elsewhere (top queries, table breakdown). Only single-variant
		# "Same query ran N× at …" is actionable, so bail before any per-variant
		# work when this callsite has more than one query shape. (A fan-out call
		# site — 10 different queries each run once — is multi-variant too, and is
		# excluded here rather than by a separate per-variant max.)
		if len(variants) > 1:
			continue

		# The one query variant at this callsite (every bucket has ≥1 occurrence).
		canonical_query, canonical_occurrences = next(iter(variants.items()))
		total_count = len(canonical_occurrences)
		function_name = bucket["function_name"]

		# N+1 signal: the query must repeat ≥ min_occurrences WITHIN A SINGLE
		# request. Counting occurrences ACROSS requests would mis-flag a normal
		# per-request query (run once per request × N requests) as a loop, emitting
		# a confident-but-wrong "ran N× in a row" at High severity. The dominant-
		# action Counter is built ONCE here and reused for the gate, action_ref,
		# run_count and loop scoping.
		dominant_action_counts = Counter(o["action_idx"] for o in canonical_occurrences)
		# loop_count IS the "N+1 magnitude": how many times the loop ran within a
		# single request — the busiest one — never the cross-request total (which
		# would inflate the count, the "…in a row" wording, AND the severity).
		# ``action_ref`` must point at the request where the loop actually ran (the
		# dominant action), NOT the first request the query merely appeared in — the
		# report walks THAT action's call tree for inner-loop detail. Taking both
		# from the same most_common(1) keeps them aligned.
		dominant_action_idx, loop_count = dominant_action_counts.most_common(1)[0]
		if loop_count < min_occurrences:
			continue
		action_idx = dominant_action_idx

		per_hit_durations = [o["duration"] for o in canonical_occurrences]
		total_time = sum(per_hit_durations)
		# ``run_count`` = requests in which the query actually REPEATED (a real
		# loop to batch), not merely appeared. A query that loops in one request
		# but also runs once in 100 others has run_count == 1, so the "ran in N
		# requests" wording and the post-fix projection don't over-count the
		# single-run requests — those carry no loop to collapse.
		# A request "looped" only if it hit the N+1 threshold (min_occurrences) — the
		# same bar that qualifies the finding. Counting requests with a mere 2–9
		# sub-threshold repeats would inflate run_count / loop_time / severity and
		# make the "looped in N requests" wording dishonest (those requests never
		# held the flagged loop).
		looping_actions = {a for a, c in dominant_action_counts.items() if c >= min_occurrences}
		run_count = len(looping_actions)
		# Time + P95 for the finding are scoped to the requests where the query
		# genuinely LOOPED at that magnitude, NOT to a single "busiest" request: a
		# loop that legitimately recurs in 5 requests keeps all 5 in scope, so its
		# cumulative-but-recoverable cost still rates severity honestly. A tiny loop
		# is not rated High just because the same query, run below the threshold
		# across many unrelated requests, sums past the time floor — that cost is
		# NOT recoverable by batching this loop.
		# ``total_time`` (all occurrences) stays for cumulative reporting.
		loop_durations = [
			o["duration"] for o in canonical_occurrences if o["action_idx"] in looping_actions
		]
		loop_time = sum(loop_durations)

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

		scope = _LoopScope(
			total_count=total_count,
			loop_count=loop_count,
			run_count=run_count,
			total_time=total_time,
			loop_time=loop_time,
			per_hit_durations=per_hit_durations,
			loop_durations=loop_durations,
		)
		builder = _build_framework_finding if is_framework else _build_user_finding
		findings.append(builder(
			scope,
			short_fn=short_fn,
			filename=filename,
			lineno=lineno,
			function_name=function_name,
			normalized=canonical_query,
			action_idx=action_idx,
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


def _p95(durations) -> float | None:
	"""P95 of ``durations``, or None below ``P95_MIN_SAMPLES`` (too small to mean anything)."""
	if durations and len(durations) >= P95_MIN_SAMPLES:
		return percentile(durations, 95)
	return None


def _title_for_callsite(short_fn, lineno, count, run_count=1) -> str:
	"""'Same query ran N×' — hedged 'up to N×' for a multi-request loop, where
	``count`` is the busiest request's peak, not a uniform figure."""
	prefix = "Same query ran up to" if run_count > 1 else "Same query ran"
	return f"{prefix} {count}× at {short_fn}:{lineno}"


def _build_user_finding(
	scope: _LoopScope,
	*,
	short_fn: str,
	filename: str,
	lineno,
	function_name: str,
	normalized: str,
	action_idx: int,
) -> dict:
	"""User-code N+1 finding — severity by loop size & the loop's own time, with a fix hint."""
	total_count = scope.total_count
	loop_count = scope.loop_count
	run_count = scope.run_count
	total_time = scope.total_time
	loop_time = scope.loop_time
	loop_durations = scope.loop_durations
	# loop_avg = the loop's own per-query cost, used for the post-fix projection
	# so unrelated single-run requests don't skew it. loop_durations is always
	# non-empty here (a user finding is only built once the loop cleared
	# min_occurrences in ≥1 request); the ``or 0`` is defence-in-depth so the
	# analyze pass can never divide by zero.
	loop_avg = loop_time / len(loop_durations) if loop_durations else 0

	# Cost quoted is the LOOP's own time (loop_time), never the cross-request
	# cumulative total — a query that loops in one request but also runs once
	# elsewhere must not fold those single runs into "the loop".
	tail = (
		"This is usually a Python loop that should fetch its data in one "
		"query instead of one-at-a-time. Typical fix: a few hours of dev work."
	)
	# A sub-1ms loop (only reachable when the min-time gate is configured below
	# the 20ms default) would render as a misleading "0ms" — say "<1ms" instead.
	# Guard on ``>= 1`` not ``>= 0.5``: f"{0.5:.0f}" is "0" (round-half-to-even),
	# so 0.5 must take the "<1ms" branch too.
	cost = f"{loop_time:.0f}ms" if loop_time >= 1 else "<1ms"
	if run_count > 1:
		# The loop spanned several requests; loop_count is the WORST request's
		# count (the peak), not a uniform per-request figure — "up to N" so it
		# isn't read as N × run_count.
		desc = (
			f"We noticed the same query repeated up to {loop_count} times in a row "
			f"from the same line of code ({filename}:{lineno}). That loop ran in "
			f"{run_count} separate requests, costing about {cost}. {tail}"
		)
	else:
		# One looping request, so loop_count is exact.
		desc = (
			f"We noticed the same query was repeated {loop_count} times in a row "
			f"from the same line of code ({filename}:{lineno}), costing about "
			f"{cost}. {tail}"
		)

	# v0.7.x M4: P95 of the per-hit duration distribution, when the
	# sample is large enough to be meaningful. Gives the user a sense
	# of the tail beyond the average and consolidated total. Scoped to the
	# loop's own hits (not single-run appearances) so it describes the
	# thing being flagged.
	p95_ms = _p95(loop_durations)

	return {
		"finding_type": "N+1 Query",
		# Severity by the per-request loop size (loop_count) and the loop's OWN
		# recoverable time (loop_time) — never the cross-request cumulative
		# total, which single-run requests would inflate.
		"severity": _severity(loop_count, loop_time),
		"title": _title_for_callsite(short_fn, lineno, loop_count, run_count),
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
				# Single-variant is the only shape emitted (multi-variant bails
				# upstream); kept in the JSON for schema stability.
				"variant_count": 1,
				"p95_ms": round(p95_ms, 2) if p95_ms is not None else None,
				# The one query shape at this callsite (multi-variant is suppressed).
				"sample_queries": [normalized],
				"total_time_ms": round(total_time, 2),
				# No session-wide "~X each": for a bimodal loop (loop hits + unrelated
				# single runs) it blends two costs into a misleading average and would
				# contradict the impact box's loop-scoped "per hit". The loop's own
				# per-query cost is shown there instead.
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
				# Uses the LOOP's own per-query average (loop_avg = loop_time /
				# loop hits), not a session-wide average that single-run requests
				# would skew. Only the loop collapses — any non-loop appearances
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
	scope: _LoopScope,
	*,
	short_fn: str,
	filename: str,
	lineno,
	function_name: str,
	normalized: str,
	action_idx: int,
) -> dict:
	"""Framework-level N+1 — always Low, cumulative framing, full technical detail."""
	total_count = scope.total_count
	loop_count = scope.loop_count
	run_count = scope.run_count
	total_time = scope.total_time
	per_hit_durations = scope.per_hit_durations
	# Framework findings are Low + informational and framed around the
	# CUMULATIVE session cost (the description says "issued N queries in this
	# session"), so the title uses total_count too — keeping title and body on
	# the same number. (The user-code finding, by contrast, is about a
	# per-request loop and uses loop_count.) loop_count/run_count are still
	# carried in the detail JSON below for anyone optimising the framework.
	title = f"Framework query repeated {total_count}× at {short_fn}:{lineno}"

	p95_ms = _p95(per_hit_durations)

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
				"variant_count": 1,
				"p95_ms": round(p95_ms, 2) if p95_ms is not None else None,
				"sample_queries": [normalized],
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
