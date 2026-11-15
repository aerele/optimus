# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""URL redaction hardening: linear time on hostile input, nested and encoded URL values, credential header
names, fail-closed capture and one source for the Settings extras. Pure functions, no Frappe."""

import time
from unittest import mock

import pytest

from optimus import redaction

REDACTED = "<REDACTED>"
N = 60_000


def _seconds(fn, *args):
	start = time.perf_counter()
	fn(*args)
	return time.perf_counter() - start


HOSTILE = (
	lambda n: "a" * n + "@",
	lambda n: "a" * n + "://x/@",
	lambda n: "//" + "a b " * (n // 4) + "@",
)


class TestLinearTime:
	"""A guest controls form values and the Referer, so no input may stall a worker."""

	@pytest.mark.parametrize("make", HOSTILE)
	def test_redact_url_has_a_generous_cap(self, make):
		# The quadratic form takes seconds at this size; the linear one takes milliseconds.
		assert _seconds(redaction.redact_url, make(N)) < 1.5

	@pytest.mark.parametrize("make", HOSTILE)
	def test_user_info_scan_time_grows_linearly(self, make):
		# 8x the input: about 8x the time when linear, about 64x when quadratic.
		small = max(_seconds(redaction._mask_userinfo, make(N // 8), REDACTED), 0.001)
		large = _seconds(redaction._mask_userinfo, make(N), REDACTED)
		assert large < small * 24 + 0.05

	@pytest.mark.parametrize("make", HOSTILE)
	def test_redact_sensitive_form_value_has_a_generous_cap(self, make):
		assert _seconds(redaction.redact_sensitive, {"cmd": make(N * 4)}) < 1.5

	def test_no_regex_sees_a_url_over_the_limit(self):
		class Probe:
			def sub(self, repl, text, count=0):
				assert len(text) <= redaction.URL_MAX_CHARS
				return text

		with mock.patch.object(redaction, "_USERINFO", Probe()):
			for text in ("a" * 20_000 + "@", "a" * 20_000 + "?token=x", "//u:p@" + "a" * 20_000 + "?a=1"):
				redaction.redact_url(text)

	def test_long_url_keeps_user_info_masked(self):
		out = redaction.redact_url("https://user:pw@host/" + "a" * 20_000 + "?token=x")
		assert "user:pw" not in out and out.startswith("https://" + REDACTED + "@host/")
		assert "token=x" not in out

	def test_user_info_stops_at_whitespace(self):
		assert redaction.redact_url("see //not a user@host") == "see //not a user@host"
		assert redaction.redact_url("https://u:p@host/x") == f"https://{REDACTED}@host/x"
		assert redaction.redact_url("//u:p@host") == f"//{REDACTED}@host"
