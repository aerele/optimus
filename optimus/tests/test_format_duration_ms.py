# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Pure-Python unit tests for ``_format_duration_ms``: the threshold-aware
duration formatter behind the ``fmt_ms`` Jinja-callable. Below the threshold it
renders ms (caller-controlled decimals); at or above, seconds with 2 decimals.

The formatter returns ``markupsafe.Markup`` so the seconds branch can wrap the
output in a ``<span class="time-high">``. ``Markup`` subclasses ``str`` so string
equality still works, but tests must match the wrapped form when ≥1000ms."""

from optimus.renderer import _format_duration_ms


def _seconds(text: str) -> str:
	"""Wrap a seconds-formatted value in the highlight span."""
	return f'<span class="time-high">{text}</span>'


class TestBelowThreshold:
	def test_zero(self):
		assert _format_duration_ms(0) == "0ms"

	def test_just_under_threshold(self):
		assert _format_duration_ms(999.4) == "999ms"  # rounds down, stays ms
		# 999.9 rounds up to a full second at display precision, so it rolls over
		# to seconds rather than showing the four-digit "1000ms" the rule avoids.
		assert _format_duration_ms(999.9) == _seconds("1.00s")

	def test_decimals_one(self):
		assert _format_duration_ms(12.5, decimals=1) == "12.5ms"


class TestAboveThreshold:
	def test_exact_threshold_converts(self):
		assert _format_duration_ms(1000) == _seconds("1.00s")

	def test_custom_threshold_above_default(self):
		# threshold raised to 5000 → 4234ms still renders as ms.
		assert _format_duration_ms(4234, threshold_ms=5000) == "4234ms"
		assert _format_duration_ms(5234, threshold_ms=5000) == _seconds("5.23s")


class TestDisabled:
	def test_threshold_zero_keeps_ms(self):
		assert _format_duration_ms(5234, threshold_ms=0) == "5234ms"


class TestDefensive:
	def test_none_input(self):
		assert _format_duration_ms(None) == "0ms"

	def test_negative_value_above_threshold_absolute(self):
		# abs(-5234) >= 1000 → converts.
		assert _format_duration_ms(-5234) == _seconds("-5.23s")
