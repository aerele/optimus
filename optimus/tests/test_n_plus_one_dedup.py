# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for callsite-level N+1 dedup.

n_plus_one groups by (filename, lineno). A multi-variant callsite (many
distinct query shapes) emits NO finding; only the single-variant classic
"Same query ran N× at file:line" case is flagged.
"""

import json

from optimus.analyzers import n_plus_one
from optimus.analyzers.base import AnalyzeContext


def _make_recording(stack, queries_per_variant, variants, per_query_ms=2.0):
	"""Build a recording where each of `variants` normalized queries
	appears `queries_per_variant` times from the same `stack`.

	Default `per_query_ms=2.0` so the fixture clears the 20ms
	total-time floor with `queries_per_variant=15` (15×2 = 30ms).
	"""
	calls = []
	for v_idx in range(variants):
		query_shape = f"SELECT * FROM `tab{v_idx}` WHERE name=?"
		for _ in range(queries_per_variant):
			calls.append({
				"normalized_query": query_shape,
				"duration": per_query_ms,
				"stack": stack,
				"exact_copies": 1,
				"normalized_copies": 1,
			})
	return {
		"uuid": "t", "path": "/", "method": "GET",
		"cmd": None, "event_type": "HTTP Request",
		"duration": 100.0, "calls": calls,
	}


def test_multi_variant_callsite_emits_no_finding():
	"""A callsite emitting many distinct SQL shapes (multi-variant) emits no N+1
	finding; the dominant variant is still visible in top-queries /
	table-breakdown, while single-repeated-shape hot loops are still flagged by
	the classic 'Same query ran N× at …' path."""
	stack = [
		{"filename": "apps/myapp/controllers/bulk.py", "lineno": 42, "function": "f"},
	]
	recording = _make_recording(stack, queries_per_variant=30, variants=10)
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = n_plus_one.analyze([recording], ctx)

	# Multi-variant case: no N+1 finding emitted.
	assert result.findings == [], (
		"Multi-variant callsite must NOT emit an N+1 finding. "
		f"Got: {[f['title'] for f in result.findings]}"
	)


def test_single_variant_keeps_classic_title():
	"""Backwards compat: 1 variant × N occurrences still reads as
	'Same query ran N× at file:line' the classic N+1 shape."""
	stack = [
		{"filename": "apps/myapp/foo.py", "lineno": 10, "function": "loop"},
	]
	recording = _make_recording(stack, queries_per_variant=15, variants=1)
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = n_plus_one.analyze([recording], ctx)

	assert len(result.findings) == 1
	f = result.findings[0]
	assert "Same query ran 15× at" in f["title"], (
		f"Single-variant title must match classic shape; got: {f['title']!r}"
	)


def test_max_variant_threshold_prevents_fanout_false_positives():
	"""A fan-out callsite 10 different queries, each called once
	isn't an N+1. It's a function that dispatches to multiple queries.
	We gate by the MOST-repeated variant, not the total, so these
	don't trigger a finding."""
	stack = [
		{"filename": "apps/myapp/dispatch.py", "lineno": 20, "function": "fan"},
	]
	recording = _make_recording(stack, queries_per_variant=1, variants=20)
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = n_plus_one.analyze([recording], ctx)

	# 20 queries total BUT no single query shape repeats ≥ threshold.
	# Must NOT emit a finding.
	assert result.findings == [], (
		"Fan-out callsite (20 unique queries × 1 each) must not flag "
		"as N+1 the max-variant threshold should filter it. "
		f"Got: {[f['title'] for f in result.findings]}"
	)


def test_multiple_callsites_each_get_their_own_finding():
	"""Two different loops in two different files → two findings."""
	stack_a = [
		{"filename": "apps/myapp/a.py", "lineno": 1, "function": "f"},
	]
	stack_b = [
		{"filename": "apps/myapp/b.py", "lineno": 50, "function": "g"},
	]
	ra = _make_recording(stack_a, queries_per_variant=15, variants=1)
	rb = _make_recording(stack_b, queries_per_variant=20, variants=1)

	ctx = AnalyzeContext(session_uuid="t", docname="t")
	result = n_plus_one.analyze([ra, rb], ctx)

	assert len(result.findings) == 2
	titles = sorted(f["title"] for f in result.findings)
	# Both callsites present.
	assert any("a.py:1" in t for t in titles)
	assert any("b.py:50" in t for t in titles)
