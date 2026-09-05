# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Sanity tests for analyzer-base metric helpers.

``percentile`` is used by repetition-heavy analyzers (N+1, redundant calls) to
surface the tail of the per-hit duration distribution; a silent regression to
"p95 < p50" would corrupt every finding card that reads from it.

``fmt_ms`` is the report's single duration-formatter; its output is load-bearing
across every finding card, KPI strip and stat label.
"""

import math

import pytest

from optimus.analyzers.base import percentile


def test_percentile_handles_empty_input():
	assert percentile([], 95) == 0.0
	assert percentile([], 50) == 0.0


def test_percentile_p95_at_least_p50_for_ascending_input():
	values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
	assert percentile(values, 95) >= percentile(values, 50)


def test_percentile_p100_returns_max():
	values = [3, 1, 4, 1, 5, 9, 2, 6, 5, 3]
	assert percentile(values, 100) == max(values)


def test_percentile_p0_returns_min():
	values = [3, 1, 4, 1, 5, 9, 2, 6, 5, 3]
	assert percentile(values, 0) == min(values)


def test_percentile_p50_returns_median_for_odd_count():
	# 5 values, sorted: [1, 2, 3, 4, 5] → median = 3.
	values = [3, 5, 1, 4, 2]
	assert percentile(values, 50) == 3


def test_percentile_monotonic_across_percentages():
	"""For ascending data, percentile() must be monotonic non-decreasing
	in ``pct``."""
	values = list(range(1, 101))  # 1..100
	last = -math.inf
	for p in range(0, 101, 5):
		current = percentile(values, p)
		assert current >= last, f"percentile {p} ({current}) < previous ({last})"
		last = current


# D.M-S7 fmt_ms property test (Hypothesis-driven if available).
try:
	from hypothesis import given
	from hypothesis import strategies as st
	_HAS_HYPOTHESIS = True
except ImportError:  # pragma: no cover - optional dependency
	_HAS_HYPOTHESIS = False


def _fmt_ms():
	"""The renderer's fmt_ms is a closure inside ``render()``; the
	stable, module-level formatter used by every contract path is
	report_context._ms_display."""
	from optimus.report_context import _ms_display
	return _ms_display


def test_fmt_ms_output_ends_in_ms_or_s_for_known_values():
	"""Spot-checks of the formatter contract: every output ends with
	'ms' or 's' (never both, never something else)."""
	fmt = _fmt_ms()
	for v in (0, 0.5, 1, 50, 999, 1000, 1500, 60_000, 1e6):
		out = str(fmt(v))
		# Strip wrapping markup (e.g. `<span class="time-high">…ms</span>`).
		import re
		bare = re.sub(r"<[^>]+>", "", out).strip()
		assert bare.endswith("ms") or bare.endswith("s"), (
			f"fmt_ms({v}) = {out!r} doesn't end in ms or s"
		)


if _HAS_HYPOTHESIS:
	@given(st.floats(min_value=0.0, max_value=1e7, allow_nan=False, allow_infinity=False))
	def test_fmt_ms_property_output_ends_ms_or_s(v):
		"""Property: for any non-negative finite ms value, fmt_ms output
		(stripped of HTML wrapping) ends in 'ms' or 's'."""
		fmt = _fmt_ms()
		import re
		bare = re.sub(r"<[^>]+>", "", str(fmt(v))).strip()
		assert bare.endswith("ms") or bare.endswith("s"), bare
