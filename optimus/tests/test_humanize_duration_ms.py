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
		assert humanize_duration_ms(999.4) == "999ms"  # rounds down, stays ms
		# 999.9 rounds up to a full second at display precision, so it rolls over
		# to seconds rather than showing the four-digit "1000ms" the rule avoids.
		assert humanize_duration_ms(999.9) == "1.00s"

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

	def test_threshold_is_the_second_positional_arg(self):
		# Signature matches _format_duration_ms: (ms, threshold_ms, decimals).
		# Guards the footgun where a swapped order bound decimals to a huge
		# threshold value and emitted a giant decimal string.
		assert humanize_duration_ms(1500, 5000) == "1500ms"   # 1500 < 5000
		assert humanize_duration_ms(1500, 1000) == "1.50s"    # 1500 >= 1000
		assert humanize_duration_ms(12.5, 1000, 1) == "12.5ms"  # decimals is third

	def test_relaxed_profile_keeps_ms(self):
		# The shipped "Relaxed" profile sets an effectively-infinite threshold.
		assert humanize_duration_ms(5000, threshold_ms=99999999) == "5000ms"


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


class TestRoundingBoundaryAgreement:
	"""The seconds value is computed from the whole-millisecond number, so a
	duration formatted straight from the raw float (a finding's impact badge)
	and the same duration re-parsed from already-rounded "1235ms" finding text
	(the title) can't disagree by 0.01s at a rounding boundary."""

	def test_raw_float_and_rounded_ms_agree(self):
		# 1234.99 (raw impact) rounds to 1235ms (baked title); both must render
		# the same string, not "1.23s" vs "1.24s".
		assert humanize_duration_ms(1234.99) == humanize_duration_ms(1235.0)

	def test_fractional_ms_matches_its_rounded_int(self):
		for raw in (1500.4, 1500.6, 2749.5, 999.6):
			assert humanize_duration_ms(raw) == humanize_duration_ms(round(raw))


class TestNegativeZero:
	"""A value that ROUNDS to zero at the display precision must never keep a
	negative sign: a sub-precision cross-run improvement reads "0ms", not
	"-0.00ms". A genuine negative still keeps its sign."""

	def test_negative_rounding_to_zero_drops_sign(self):
		assert humanize_duration_ms(-0.001, decimals=2) == "0.00ms"
		assert humanize_duration_ms(-0.3) == "0ms"           # decimals=0 rounds to 0
		assert humanize_duration_ms(-0.4, decimals=0) == "0ms"

	def test_genuine_negative_keeps_sign(self):
		assert humanize_duration_ms(-0.3, decimals=2) == "-0.30ms"
		assert humanize_duration_ms(-5.0, decimals=2) == "-5.00ms"

	def test_seconds_branch_drops_negative_zero(self):
		# A negative value that rolls to seconds but rounds to zero there reads
		# "0.00s", not "-0.00s" (reachable only at an unusually low threshold).
		assert humanize_duration_ms(-1, threshold_ms=1) == "0.00s"


class TestNonFiniteInput:
	"""inf / nan / an overflowing value format as zero (the "non-numeric -> zero"
	contract), never crash: round(inf) raises OverflowError and round(nan) raises
	ValueError, and OverflowError is not a ValueError, so a caller guarding
	except (TypeError, ValueError) would otherwise 500 the whole render."""

	def test_inf_nan_and_overflow_format_as_zero(self):
		assert humanize_duration_ms("inf") == "0ms"
		assert humanize_duration_ms("nan") == "0ms"
		assert humanize_duration_ms("-inf") == "0ms"
		assert humanize_duration_ms(float("inf")) == "0ms"
		# A token so long it overflows float() to inf (the reformatter's \\d+ is
		# unbounded) must also format as zero, not raise.
		assert humanize_duration_ms("9" * 309) == "0ms"
		# A huge int overflows float() with OverflowError (not ValueError), which
		# the except must catch too.
		assert humanize_duration_ms(10 ** 400) == "0ms"
