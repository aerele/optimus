# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Render-level tests for the ``large_duration_threshold_ms`` setting.

The threshold controls how durations render: values at or above it display as
seconds (e.g. ``5.23s``); below it, they stay as milliseconds (``800ms``)."""

import json
import types
from unittest.mock import patch

from optimus import renderer
from optimus.analyzers.base import (
	_DUR_SEP,
	_reformat_durations_in_text,
	dur,
	format_durations,
)
from optimus.settings import OptimusConfig


def _action(**kw):
	base = {
		"action_label": "",
		"event_type": "HTTP Request",
		"http_method": "",
		"path": "",
		"recording_uuid": "",
		"duration_ms": 0,
		"queries_count": 0,
		"query_time_ms": 0,
		"slowest_query_ms": 0,
	}
	base.update(kw)
	return types.SimpleNamespace(**base)


def _doc(actions, findings=None):
	return types.SimpleNamespace(
		name="PS-t", session_uuid="t", title="t",
		user="a@example.com", status="Ready",
		started_at="2026-05-13T00:00:00", stopped_at="2026-05-13T00:00:06",
		notes=None, top_severity="Low", summary_html=None,
		total_duration_ms=6034, total_query_time_ms=80,
		total_queries=5, total_requests=2,
		top_queries_json="[]", table_breakdown_json="[]",
		hot_frames_json="[]", session_time_breakdown_json=None,
		total_python_ms=None, total_sql_ms=None,
		analyzer_warnings=None, v5_aggregate_json="{}",
		actions=actions, findings=findings or [], phase_2_runs=[],
	)


def _finding(title, impact_ms):
	"""A Slow Query finding whose title carries a RAW-ms duration, as the
	analyzer bakes it. Render reformats that duration; nothing is pre-formatted."""
	return types.SimpleNamespace(
		finding_type="Slow Query", severity="High",
		title=title, customer_description="A single query was slow.",
		estimated_impact_ms=impact_ms, affected_count=1, action_ref="0",
		technical_detail_json=json.dumps({
			"normalized_query": "SELECT 1",
			"callsite": "apps/myapp/foo.py:456",
		}),
	)


class TestFindingTitleThreshold:
	"""Finding titles bake raw ms at analyze time; render reformats them using
	the configured threshold, so a regenerated (not re-analyzed) report never
	shows a title unit that disagrees with the render-time impact badge."""

	def test_title_rolls_to_seconds_at_default(self):
		doc = _doc([], findings=[_finding("Slow query: 5234ms", 5234.0)])
		html = renderer.render_raw(doc, recordings=[])
		assert "Slow query: 5.23s" in html
		assert "Slow query: 5234ms" not in html

	def test_title_stays_ms_on_relaxed_threshold(self):
		doc = _doc([], findings=[_finding("Slow query: 5234ms", 5234.0)])
		with patch(
			"optimus.settings.get_config",
			return_value=OptimusConfig(large_duration_threshold_ms=99999999),
		):
			html = renderer.render_raw(doc, recordings=[])
		# Relaxed profile: the title stays in ms, matching the tables.
		assert "Slow query: 5234ms" in html
		assert "Slow query: 5.23s" not in html


class TestNotesReproducerThreshold:
	"""The Steps-to-Reproduce list bakes raw ms with a SPACE ("12418.3 ms") at
	analyze time; render must reformat those too (regression: the space form
	slipped past the first pass)."""

	def test_steps_to_reproduce_ms_converted_at_render(self):
		doc = _doc([])
		doc.notes = (
			"<ol><li>Submit Delivery Note: 12418.3 ms</li>"
			"<li>Fast step: 800 ms</li></ol>"
		)
		html = renderer.render_raw(doc, recordings=[])
		assert "Submit Delivery Note: 12.42s" in html
		assert "12418.3 ms" not in html
		assert "Fast step: 800ms" in html  # sub-second stays ms


class TestDisabledThresholdHonouredEverywhere:
	"""Regression for the review finding: the finding impact badge is built by
	report_context, which used to fall back to 1000 for a 0 threshold and roll a
	value over to seconds even when the whole report was set to stay in ms. With
	threshold=0 ("disable") honoured in every render path, the badge and title
	both stay in ms, so the two halves of the report can no longer disagree."""

	def test_finding_title_and_badge_both_stay_ms_when_disabled(self):
		doc = _doc([], findings=[_finding("Slow query: 5234ms", 5234.0)])
		with patch(
			"optimus.settings.get_config",
			return_value=OptimusConfig(large_duration_threshold_ms=0),
		):
			html = renderer.render_raw(doc, recordings=[])
		# Nothing rolls over: the raw ms form survives, no seconds anywhere.
		assert "5234ms" in html
		assert "5.23s" not in html


class TestUrlsAreNotMangled:
	"""``_reformat_durations_in_text`` rewrites <n>ms duration tokens in prose,
	but a browser-reported URL (frontend findings embed these in titles /
	descriptions / notes) can contain the same pattern. It must be left intact so
	links don't break."""

	def test_real_duration_converts_url_stays_intact(self):
		text = "LCP 1600ms on /app/report/query-2000ms-test"
		out = _reformat_durations_in_text(text, 1000.0)
		# The real duration rolls over; the URL segment is untouched.
		assert out == "LCP 1.60s on /app/report/query-2000ms-test"

	def test_href_query_param_not_rewritten(self):
		assert _reformat_durations_in_text("open ?t=1500ms now", 1000.0) == "open ?t=1500ms now"

	def test_path_segment_not_rewritten(self):
		assert _reformat_durations_in_text("GET /api/2000ms/x", 1000.0) == "GET /api/2000ms/x"

	def test_sentence_final_duration_still_converts(self):
		assert _reformat_durations_in_text("It took 5234ms.", 1000.0) == "It took 5.23s."

	def test_approx_and_label_prefixes_still_convert(self):
		# "~" (approx) and ":" (label) are real prose, not URL structure, so a
		# duration written as "~1500ms" or "latency:1500ms" must still roll over.
		assert _reformat_durations_in_text("about ~1500ms", 1000.0) == "about ~1.50s"
		assert _reformat_durations_in_text("latency:1500ms", 1000.0) == "latency:1.50s"

	def test_ms_token_inside_a_tag_attribute_is_not_rewritten(self):
		# The reformatter also runs over already-rendered HTML (notes / summary), so
		# a duration-like token inside an attribute (e.g. an inline style) must be
		# left alone or it corrupts the markup; text between tags still converts.
		html = '<span title="transition:2000ms">took 5234ms</span>'
		assert _reformat_durations_in_text(html, 1000.0) == (
			'<span title="transition:2000ms">took 5.23s</span>'
		)

	def test_ms_before_an_html_entity_still_converts(self):
		# The no-frappe fallback path HTML-escapes notes ("<" -> "&lt;"), so a
		# duration can land immediately before an escaped tag: "12418.3 ms&lt;/li&gt;".
		# The "&" there starts an entity, not a URL query separator, so the token must
		# still roll over. (This is the pure-function guard for the render-path
		# regression that only showed up in the frappe-less CI environment.)
		assert _reformat_durations_in_text("Note: 12418.3 ms&lt;/li&gt;", 1000.0) == (
			"Note: 12.42s&lt;/li&gt;"
		)

	def test_comma_grouped_duration_converts(self):
		# A thousands-grouped duration ("2,000ms", which AI-humanized notes can
		# produce) is matched whole and rolls over like "2000ms" would, instead of
		# corrupting to "2,0ms".
		assert _reformat_durations_in_text("waited 2,000ms total", 1000.0) == "waited 2.00s total"
		assert _reformat_durations_in_text("It took 1,500ms here", 500.0) == "It took 1.50s here"
		# A non-Western grouping the pattern can't consume is left intact, never corrupted.
		assert _reformat_durations_in_text("odd 1,23,456ms", 500.0) == "odd 1,23,456ms"

	def test_space_grouped_duration_converts(self):
		# Same as the comma but SPACE- / NBSP- / narrow-NBSP-grouped ("2 000ms"):
		# matched whole and rolled over, not collapsed to "2 0ms". A plain
		# " 5234ms" (the space follows a non-digit) also converts.
		for sep in (" ", "\u00a0", "\u202f"):  # space, NBSP, narrow NBSP
			text = f"waited 2{sep}000ms total"
			assert _reformat_durations_in_text(text, 1000.0) == "waited 2.00s total"
		assert _reformat_durations_in_text("done in 5234ms", 1000.0) == "done in 5.23s"
		# A stray non-thousands grouping is left intact, never corrupted.
		assert _reformat_durations_in_text("odd 1 23 456ms", 500.0) == "odd 1 23 456ms"


class TestDurationMarkers:
	"""The structured path: analyzers tag durations with dur(); render formats the
	marker exactly, so prose around it is never misread."""

	def test_marker_formats_at_render(self):
		t = f"Slow query: {dur(5234)}"
		assert format_durations(t, 1000.0) == "Slow query: 5.23s"
		assert format_durations(t, 0) == "Slow query: 5234ms"  # disabled

	def test_marker_is_exact_prose_and_urls_untouched(self):
		# A marker converts; a bare "2000ms" in a URL (no marker) does not.
		t = f"{dur(1600)} on /app/report/query-2000ms-test"
		assert format_durations(t, 1000.0) == "1.60s on /app/report/query-2000ms-test"

	def test_marker_title_formats_through_render(self):
		# End-to-end: a dur() marker in a finding title is formatted by render_raw.
		doc = _doc([], findings=[_finding(f"Slow query: {dur(5234)}", 5234.0)])
		html = renderer.render_raw(doc, recordings=[])
		assert "Slow query: 5.23s" in html
		assert "5234ms" not in html

	def test_marker_degrades_to_plain_ms(self):
		# An unformatted marker still reads as plain "5234ms" (separator invisible).
		assert dur(5234).replace(_DUR_SEP, "") == "5234ms"

	def test_marker_preserves_decimals(self):
		assert format_durations(f"line {dur(12.5, 1)}", 1000.0) == "line 12.5ms"

	def test_marker_strips_trailing_fractional_zeros(self):
		# A whole-ms value tagged with decimals reads "800ms", never "800.0ms",
		# so the reproducer notes match the pre-marker rendering exactly.
		assert format_durations(f"step {dur(800.0, 1)}", 1000.0) == "step 800ms"
		assert format_durations(f"step {dur(842.3, 1)}", 1000.0) == "step 842.3ms"
		# A whole-ms value that rolls over is unaffected (seconds are always .2f).
		assert format_durations(f"step {dur(5000000.0, 1)}", 1000.0) == "step 5000.00s"

	def test_marker_negative_input_is_clamped(self):
		# A duration is never negative; a stray negative collapses to "0ms" rather
		# than baking a "-500ms" marker the sign-free marker regex can't own.
		assert format_durations(f"x {dur(-500)}", 1000.0) == "x 0ms"
		assert format_durations(f"x {dur(-5000)}", 1000.0) == "x 0ms"

	def test_format_durations_also_handles_free_prose(self):
		# The prose fallback still rolls a raw "5234ms" in AI-written free text.
		assert format_durations("took 5234ms", 1000.0) == "took 5.23s"

	def test_multiple_markers_in_one_string(self):
		t = f"{dur(1600)} of {dur(1095)} wall time"
		assert format_durations(t, 1000.0) == "1.60s of 1.09s wall time"

	def test_marker_never_carries_a_separator_or_sci_notation(self):
		# The analyzer controls the number, so a marker can't contain a comma,
		# space or "e+" (the edge cases that plagued the prose approach).
		for ms in (2000, 2000.5, 5_000_000, 1_234_567):
			m = dur(ms)
			assert "," not in m and " " not in m and "e+" not in m

	def test_marker_non_finite_input_is_safe(self):
		# dur(inf/nan) must still yield a formattable marker, not "infms".
		for bad in (float("inf"), float("nan"), float("-inf")):
			assert format_durations(f"x {dur(bad)}", 1000.0) == "x 0ms"


class TestReformatterRobustness:
	"""Guards for the render-time reformatter against pathological / large input."""

	def test_dense_bare_lt_does_not_hang(self):
		# _TAG_SPLIT_RE was O(n^2) on dense bare "<" (no ">"), which silently hung
		# the render worker (a slow loop the broad try/except can't catch). The
		# linear split handles 60k "<" in milliseconds; assert it finishes well
		# under a generous budget and still converts the trailing real token.
		import time
		evil = "<" * 60000 + " 5234ms"
		start = time.perf_counter()
		out = _reformat_durations_in_text(evil, 1000.0)
		assert time.perf_counter() - start < 2.0
		assert out.endswith(" 5.23s")

	def test_large_plain_duration_rolls_over(self):
		# A >=1e6 ms step is baked as plain digits (not "5e+06"), so it must roll
		# over to seconds rather than being mangled.
		assert _reformat_durations_in_text("Step: 5000000 ms", 1000.0) == "Step: 5000.00s"

	def test_reproducer_note_bakes_large_duration_without_sci_notation(self):
		# analyze.py tags reproducer-step durations with dur() (plain digits, so a
		# >=1e6 ms step never bakes as "5e+06") and render formats the marker.
		from optimus.analyze import _build_auto_notes_list_html
		html = _build_auto_notes_list_html([{"cmd": "x", "duration": 5000000}])
		assert "e+" not in html
		assert "5000000ms" in html  # marker carries plain digits, trailing .0 stripped
		assert "5000.00s" in format_durations(html, 1000.0)


class TestRowDangerNotTiedToDisplay:
	"""Per-row hot/red styling fires at a FIXED slowness threshold (1000ms), not
	the display threshold, so Strict (500) doesn't paint every 500ms row red while
	the Total-time KPI stays calm."""

	def test_sub_second_action_not_hot_on_strict(self):
		doc = _doc([_action(action_label="POST /a", http_method="POST", path="/a",
		                    recording_uuid="r0", duration_ms=600)])
		with patch("optimus.settings.get_config",
		           return_value=OptimusConfig(large_duration_threshold_ms=500)):
			html = renderer.render_raw(doc, recordings=[])
		# Displays in seconds (600 >= the 500 display threshold) ...
		assert "0.60s" in html
		# ... but the row is NOT flagged hot/red: 600 < the fixed 1000ms danger
		# threshold. (Check the APPLIED class, not the always-present CSS rule.)
		assert 'class="hot-value"' not in html

	def test_slow_action_is_hot(self):
		doc = _doc([_action(action_label="POST /b", http_method="POST", path="/b",
		                    recording_uuid="r1", duration_ms=1500)])
		html = renderer.render_raw(doc, recordings=[])
		assert 'class="hot-value"' in html  # 1500 >= 1000 fixed threshold


class TestDefaultThreshold:
	def test_slow_row_renders_in_seconds(self):
		doc = _doc([
			_action(action_label="POST /api/method/myapp.slow",
			        http_method="POST", path="/api/method/myapp.slow",
			        recording_uuid="r0", duration_ms=5234),
			_action(action_label="POST /api/method/myapp.fast",
			        http_method="POST", path="/api/method/myapp.fast",
			        recording_uuid="r1", duration_ms=800),
		])
		html = renderer.render_raw(doc, recordings=[])

		# Slow action: 5234ms → "5.23s" appears, the raw ms form does NOT.
		assert ">5.23s<" in html
		assert ">5234ms<" not in html
		# Fast action stays in ms.
		assert ">800ms<" in html
		# Footer stamps the default threshold (1000).
		assert "large_duration_threshold_ms=1000" in html


class TestDisabledThreshold:
	def test_threshold_zero_keeps_everything_in_ms(self):
		doc = _doc([
			_action(action_label="POST /api/method/myapp.slow",
			        http_method="POST", path="/api/method/myapp.slow",
			        recording_uuid="r0", duration_ms=5234),
		])
		with patch(
			"optimus.settings.get_config",
			return_value=OptimusConfig(large_duration_threshold_ms=0),
		):
			html = renderer.render_raw(doc, recordings=[])

		assert ">5234ms<" in html
		assert ">5.23s<" not in html
		assert "large_duration_threshold_ms=0" in html


class TestHighThreshold:
	def test_threshold_above_all_values_keeps_everything_in_ms(self):
		doc = _doc([
			_action(action_label="POST /api/method/myapp.slow",
			        http_method="POST", path="/api/method/myapp.slow",
			        recording_uuid="r0", duration_ms=5234),
		])
		with patch(
			"optimus.settings.get_config",
			return_value=OptimusConfig(large_duration_threshold_ms=10000),
		):
			html = renderer.render_raw(doc, recordings=[])

		assert ">5234ms<" in html
		assert ">5.23s<" not in html
		assert "large_duration_threshold_ms=10000" in html


class TestLowThreshold:
	def test_threshold_500ms_converts_everything_above(self):
		doc = _doc([
			_action(action_label="POST /api/method/myapp.slow",
			        http_method="POST", path="/api/method/myapp.slow",
			        recording_uuid="r0", duration_ms=5234),
			_action(action_label="POST /api/method/myapp.medium",
			        http_method="POST", path="/api/method/myapp.medium",
			        recording_uuid="r1", duration_ms=800),
			_action(action_label="POST /api/method/myapp.fast",
			        http_method="POST", path="/api/method/myapp.fast",
			        recording_uuid="r2", duration_ms=200),
		])
		with patch(
			"optimus.settings.get_config",
			return_value=OptimusConfig(large_duration_threshold_ms=500),
		):
			html = renderer.render_raw(doc, recordings=[])

		# Both 5234ms (5.23s) and 800ms (0.80s) cross the 500ms threshold.
		assert ">5.23s<" in html
		assert ">0.80s<" in html
		# 200ms stays as ms.
		assert ">200ms<" in html
