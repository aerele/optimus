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
	_rolls_over_to_seconds,
	dur,
	format_duration_markers,
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
	"""A Slow Query finding whose title carries a raw-ms duration as a legacy
	(pre-marker) analyzer baked it. Render reformats that duration via the prose
	fallback so a stored title still agrees with its impact badge."""
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

	def test_nbsp_grouped_duration_converts(self):
		# NBSP- / narrow-NBSP-grouped ("2 000ms", the locale thousands separators):
		# matched whole and rolled over, not collapsed to "2 0ms".
		for sep in ("\u00a0", "\u202f"):  # NBSP, narrow NBSP
			text = f"waited 2{sep}000ms total"
			assert _reformat_durations_in_text(text, 1000.0) == "waited 2.00s total"
		assert _reformat_durations_in_text("done in 5234ms", 1000.0) == "done in 5.23s"

	def test_plain_space_is_not_a_thousands_separator(self):
		# A plain ASCII space is ambiguous ("12 500ms" is a count then a duration,
		# not 12,500ms), so it must NOT group: the count and the duration stay apart.
		assert _reformat_durations_in_text("ran 12 500ms total", 1000.0) == "ran 12 500ms total"
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
		doc = _doc([], findings=[_finding("Slow query: 5234ms", 5234.0)])
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

	def test_action_that_displays_a_full_second_is_flagged_hot(self):
		# A value in [999.5, 1000) rounds up to "1.00s" for display, so the red hot
		# flag must fire too. The template now reads the round-based duration_is_hot /
		# bar_kind from report_context; the old bug compared the raw ms in Jinja
		# (999.6 >= 1000 -> False), showing "1.00s" with only an amber accent.
		doc = _doc([_action(action_label="POST /c", http_method="POST", path="/c",
		                    recording_uuid="r2", duration_ms=999.6)])
		html = renderer.render_raw(doc, recordings=[])
		assert "1.00s" in html              # displayed value rounded up to a second
		assert 'class="hot-value"' in html  # ... so the row is flagged red, not amber


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


class TestCodeReviewFixes:
	"""Regressions for the low-severity findings from the max-effort review."""

	def test_note_duration_before_em_dash_rolls_over(self):
		# #2: the em-dash sweep must run AFTER format_durations. A raw "5234ms—slow"
		# used to become "5234ms-slow" (hyphen), which the prose reformatter's URL
		# guard then skipped, leaving raw ms. It must roll over to seconds now.
		doc = _doc([])
		doc.notes = "<p>Submit Delivery Note took 5234ms—the slowest step.</p>"
		html = renderer.render_raw(doc, recordings=[])
		assert "5.23s" in html
		assert "5234ms" not in html

	def test_finding_title_raw_duration_before_em_dash_rolls_over(self):
		# A stored finding title can hold a raw "<n>ms" (legacy, or a marker-less
		# analyzer) right before an em dash. format_durations must roll it over
		# BEFORE the em-dash sweep, else the sweep leaves a hyphen the prose URL
		# guard would skip, stranding the value in ms while the badge shows seconds.
		doc = _doc([], findings=[_finding("Slow query 5234ms—the worst one", 5234.0)])
		doc.findings[0].customer_description = "It took 5234ms—over budget."
		html = renderer.render_raw(doc, recordings=[])
		assert "5.23s" in html
		assert "5234ms" not in html
		assert "—" not in html

	def test_legacy_raw_ms_title_agrees_with_badge(self):
		# Regression for the review finding: a session analyzed before dur() markers
		# stores a marker-less "1374ms" title. On re-render (no re-analyze) the title
		# must roll over to match the impact badge, not stay in ms beside a seconds
		# badge. The prose fallback in format_durations is what keeps them in step.
		doc = _doc([], findings=[_finding("Slow query: 1374ms", 1374.0)])
		html = renderer.render_raw(doc, recordings=[])
		assert "Slow query: 1.37s" in html
		assert "Slow query: 1374ms" not in html

	def test_summary_tagged_duration_rolls_over(self):
		# The render-time summary is rebuilt fresh (always tagged); render formats the
		# markers (marker-only, so a threshold-literal like ">200ms" is left alone).
		doc = _doc([])
		with patch(
			"optimus.analyze._build_summary_html",
			return_value=f"The slowest step took {dur(5234)}—a big gap.",
		):
			html = renderer.render_raw(doc, recordings=[])
		assert "5.23s" in html
		assert "5234ms" not in html

	def test_stored_summary_fallback_is_formatted(self):
		# #4: when the render-time summary is empty, the template falls back to the
		# stored session.summary_html, which also carries dur() markers. It must be
		# formatted (rolled over, marker stripped), not printed raw.
		doc = _doc([])
		doc.summary_html = f"The slowest step was {dur(5234)}."
		with patch("optimus.analyze._build_summary_html", return_value=""):
			html = renderer.render_raw(doc, recordings=[])
		assert "The slowest step was 5.23s" in html
		assert _DUR_SEP not in html

	def test_stored_summary_fallback_preserves_threshold_caption(self):
		# The stored-summary fallback formats markers only (scan=False), like the
		# render-time path, so the FIXED "(>200ms)" caption is not rewritten to
		# "(>0.20s)" under a custom threshold <= 200 (where the prose scanner would
		# otherwise match the "200ms" after the "&gt;" entity).
		doc = _doc([])
		doc.summary_html = (
			"Found <strong>slow queries</strong> (&gt;200ms) - slowest " + dur(5234) + "."
		)
		with patch("optimus.analyze._build_summary_html", return_value=""):
			with patch(
				"optimus.settings.get_config",
				return_value=OptimusConfig(large_duration_threshold_ms=100),
			):
				html = renderer.render_raw(doc, recordings=[])
		assert "(&gt;200ms)" in html      # fixed caption preserved
		assert "&gt;0.20s" not in html    # NOT rewritten by the prose scanner
		assert "slowest 5.23s" in html    # the real dur() marker still formats
		assert _DUR_SEP not in html

	def test_title_truncation_never_severs_a_duration(self):
		# A long title whose dur() marker STRADDLES the truncation boundary must not
		# be cut mid token, leaving bare digits or a unit-less "5234m". The token is
		# dropped whole instead (badge + description still carry it). The marker is
		# positioned so the keep boundary falls inside it, exercising the straddle
		# branch: if that branch is deleted the cut lands at "...5234" and the
		# stem.endswith assertion below fails.
		from optimus.analyze import (
			_FINDING_TITLE_ELLIPSIS,
			_FINDING_TITLE_MAX_CHARS,
			_truncate_finding_titles,
		)

		keep = _FINDING_TITLE_MAX_CHARS - len(_FINDING_TITLE_ELLIPSIS)
		# Place the marker so it starts a few chars before `keep` and ends after it.
		marker = dur(5234)  # "5234ms" + separator
		prefix = "x" * (keep - 4)  # marker starts at keep-4, spans across keep
		f = {"title": prefix + marker + (" tail" * 8)}  # total well over the max
		# Sanity: the boundary really lands inside the marker (else the test is moot).
		assert len(prefix) < keep < len(prefix) + len("5234ms")
		_truncate_finding_titles([f])
		t = f["title"]
		assert len(t) <= _FINDING_TITLE_MAX_CHARS
		assert t.endswith("...")
		# No half-number / unit-less remnant left dangling before the ellipsis.
		stem = t[: -len(_FINDING_TITLE_ELLIPSIS)].rstrip()
		assert not stem.endswith(("5", "52", "523", "5234", "5234m"))
		assert _DUR_SEP not in t

	def test_report_context_fallback_honours_non_default_threshold(self):
		# #6: build_report_context must build a threshold-aware default formatter,
		# so a caller that omits ctx["fmt_ms"] on a non-default profile does NOT
		# silently fall back to the hardcoded 1000ms rollover.
		from optimus import report_context as rc

		class _Doc:
			def __getattr__(self, _n):
				return 0

		ctx = {
			"render_config": {"large_duration_threshold_ms": 500},
			"top_queries": [{"query_duration_ms": 800, "callsite": "a/b.py:1"}],
		}
		out = rc.build_report_context(_Doc(), ctx)
		# Inspect the BUILT output, which is formatted by the default _fmt_ms the
		# context builds when ctx omits "fmt_ms". 800ms at a 500ms threshold rolls
		# over; if that fallback regressed to a hardcoded 1000 it would read "800ms".
		assert out["slow_queries"][0]["total_time_display"] == "0.80s"
		assert out["large_duration_threshold_ms"] == 500

	def test_rolls_over_helper_matches_humanize(self):
		# #11: the highlight decision comes from the shared helper, not string-
		# sniffing the formatted output. It must agree with humanize_duration_ms.
		from optimus.analyzers.base import humanize_duration_ms

		for v in (0, 300, 499.6, 500, 800, 5234, 10_000_000):
			for thr in (0, 500, 1000):
				text = humanize_duration_ms(v, thr)
				is_seconds = text.endswith("s") and not text.endswith("ms")
				assert _rolls_over_to_seconds(v, thr) == is_seconds


class TestReviewRound4Fixes:
	"""Regressions for the 4th review pass."""

	def test_count_then_duration_still_converts(self):
		# #3: a real duration after a count ("top 3 2400ms") must convert; the old
		# digit+space look-behind (a leftover of the removed plain-space grouping)
		# wrongly skipped it.
		assert _reformat_durations_in_text("top 3 2400ms queries", 1000.0) == "top 3 2.40s queries"
		assert _reformat_durations_in_text("after 2 retries 5234ms", 1000.0) == "after 2 retries 5.23s"

	def test_title_and_badge_agree_at_rounding_boundary(self):
		# #4: dur() rounds to 0.01ms so a title decides the ms/seconds rollover from
		# the same value as the badge (estimated_impact_ms = round(x, 2)). 999.495ms
		# rolled title 999ms vs badge 1.00s before; both roll to 1.00s now.
		from optimus.report_context import _ms_display

		imp = 999.495
		title = format_duration_markers(f"x {dur(imp)}", 1000.0)
		badge = _ms_display(round(imp, 2), threshold_ms=1000.0)
		assert title == "x 1.00s"
		assert badge == "1.00s"

	def test_resolve_threshold_ms_tolerates_bad_value(self):
		# #7: a non-numeric config value falls back to the default instead of raising
		# at the top of build_report_context.
		from optimus.analyzers.base import DEFAULT_DISPLAY_THRESHOLD_MS
		from optimus.report_context import _resolve_threshold_ms

		assert _resolve_threshold_ms({"large_duration_threshold_ms": "abc"}) == DEFAULT_DISPLAY_THRESHOLD_MS
		assert _resolve_threshold_ms({"large_duration_threshold_ms": "500"}) == 500.0
		assert _resolve_threshold_ms({"large_duration_threshold_ms": 0}) == 0.0


class TestFinalizeProse:
	"""_finalize_prose is the single home of the format-before-em-dash order."""

	def test_scan_true_rolls_raw_ms_before_em_dash_sweep(self):
		from optimus.renderer._internal import _finalize_prose

		assert _finalize_prose("took 5234ms—slow", 1000.0) == "took 5.23s-slow"

	def test_scan_false_is_marker_only(self):
		from optimus.renderer._internal import _finalize_prose

		# A threshold literal (no dur() marker) is left alone under scan=False.
		assert _finalize_prose("queries &gt;200ms slow", 1000.0, scan=False) == "queries &gt;200ms slow"
		assert _finalize_prose(f"took {dur(5234)}", 1000.0, scan=False) == "took 5.23s"
