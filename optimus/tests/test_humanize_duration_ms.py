# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Pure-Python unit tests for ``humanize_duration_ms``: the shared plain-text
duration formatter analyzers and analyze.py drop into finding titles and
descriptions. One second is 1000ms, so a duration that reaches a full second
reads as seconds ("1.50s") instead of a four-digit millisecond count
("1500ms"). Below the threshold it renders as ms (with caller-controlled
decimals); at or above, as seconds with 2 decimals.

Unlike ``_format_duration_ms`` (which powers the report's HTML cells and wraps
seconds in a ``<span class="time-high">``), this one returns plain text with
no markup and no space before the unit, so it slots into a sentence cleanly."""

from optimus.analyzers.base import humanize_duration_ms


class TestBelowThreshold:
	def test_zero(self):
		assert humanize_duration_ms(0) == "0ms"

	def test_integer_ms_default_zero_decimals(self):
		assert humanize_duration_ms(800) == "800ms"

	def test_just_under_threshold(self):
		assert humanize_duration_ms(999.4) == "999ms"  # %.0f rounds
		# Rounds up for display but the threshold check uses the raw value
		# (999.9 < 1000), so it stays in the ms branch.
		assert humanize_duration_ms(999.9) == "1000ms"

	def test_decimals_one(self):
		assert humanize_duration_ms(12.5, decimals=1) == "12.5ms"

	def test_decimals_two_preserves_sub_ms(self):
		assert humanize_duration_ms(0.52, decimals=2) == "0.52ms"

	def test_decimals_two_pads_to_two(self):
		assert humanize_duration_ms(5, decimals=2) == "5.00ms"


class TestAboveThreshold:
	def test_exact_threshold_converts(self):
		# 1000ms == 1 second, so the boundary itself rolls over to seconds.
		assert humanize_duration_ms(1000) == "1.00s"

	def test_typical_slow_query(self):
		assert humanize_duration_ms(1234) == "1.23s"

	def test_typical_slow_action(self):
		assert humanize_duration_ms(5234) == "5.23s"

	def test_decimals_arg_ignored_in_seconds(self):
		# seconds always show 2 decimals regardless of the decimals arg.
		assert humanize_duration_ms(5234, decimals=0) == "5.23s"
		assert humanize_duration_ms(5234, decimals=1) == "5.23s"
		assert humanize_duration_ms(5234, decimals=2) == "5.23s"

	def test_large_value(self):
		# Python's banker's rounding: 62.345 → 62.34 (not 62.35).
		assert humanize_duration_ms(62345) == "62.34s"


class TestCustomThreshold:
	def test_threshold_above_default_keeps_ms(self):
		assert humanize_duration_ms(4234, threshold_ms=5000) == "4234ms"
		assert humanize_duration_ms(5234, threshold_ms=5000) == "5.23s"

	def test_threshold_zero_disables_conversion(self):
		assert humanize_duration_ms(5234, threshold_ms=0) == "5234ms"


class TestDefensive:
	def test_none_input(self):
		assert humanize_duration_ms(None) == "0ms"

	def test_string_input(self):
		assert humanize_duration_ms("not-a-number") == "0ms"

	def test_negative_below_threshold_absolute(self):
		# abs() means a -500ms value (below threshold) stays as ms.
		assert humanize_duration_ms(-500) == "-500ms"

	def test_negative_above_threshold_absolute(self):
		assert humanize_duration_ms(-5234) == "-5.23s"
