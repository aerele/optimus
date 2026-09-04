# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""B.M-S4 / D.M-S5 regression tests for production-readiness disclosures.

The audit graded Optimus partially trustworthy on metric integrity because
several caveats the data deserves were absent from the rendered report
(wall-clock sampler discount, frame-count truncation, slow-query cap, etc).
This file guards against those caveats silently disappearing again.
"""

import json
import re
from types import SimpleNamespace

from optimus import renderer


def _fake_doc(**fields):
	defaults = {
		"name": "PS-disclosure-test",
		"session_uuid": "test-uuid",
		"title": "disclosure test",
		"user": "tester@example.com",
		"status": "Ready",
		"started_at": "2026-05-19T00:00:00",
		"stopped_at": "2026-05-19T00:00:05",
		"total_duration_ms": 5000,
		"total_requests": 1,
		"total_queries": 0,
		"total_query_time_ms": 0,
		"analyze_duration_ms": 100,
		"top_severity": "None",
		"summary_html": "<p>summary</p>",
		"top_queries_json": "[]",
		"table_breakdown_json": "[]",
		"analyzer_warnings": None,
		"actions": [],
		"findings": [],
		"hot_frames_json": None,
		"session_time_breakdown_json": None,
		"total_python_ms": None,
		"total_sql_ms": None,
	}
	defaults.update(fields)
	return SimpleNamespace(**defaults)


def test_sampler_disclosure_phrase_in_rendered_html():
	"""B.M-S4 the 'How to read this report' bullet that calls out the
	wall-clock sampler must always be present so a reader knows
	sub-interval functions can be under-counted."""
	doc = _fake_doc()
	html = renderer.render_raw(doc, recordings=[])
	assert "wall-clock sampler" in html
	# Interval is dynamic (Optimus Settings), but the phrase
	# "sampler at" + "ms intervals" should always be present.
	assert re.search(r"sampler at .+?ms interval", html), html[:5000]


def test_self_time_wall_clock_disclosure_present():
	"""Self-time-is-wall-clock callout in 'How to read this report'."""
	doc = _fake_doc()
	html = renderer.render_raw(doc, recordings=[])
	# Match either the glossary bullet or the Slow-Hot-Path disclaimer copy.
	assert "wall-clock self-time" in html or "self-time is wall-clock" in html


def test_frame_truncation_banner_renders_when_truncated():
	"""D.M-S5 when ANY action's call_tree_json is _truncated with
	captured/kept counts, the Hot Frames banner must surface those
	numbers (else readers can't tell their picture is partial)."""
	truncated_tree = json.dumps({
		"_truncated": True,
		"_captured_frames": 850,
		"_kept_frames": 300,
		"function": "<root>",
		"filename": "",
		"lineno": 0,
		"self_ms": 0,
		"cumulative_ms": 1000,
		"kind": "python",
		"children": [
			{
				"function": "my_app.work.do",
				"filename": "apps/my_app/work.py",
				"lineno": 10,
				"self_ms": 250,
				"cumulative_ms": 800,
				"kind": "python",
				"children": [],
			},
		],
	})
	action_attrs = {
		"event_type": "Request",
		"action_label": "POST /api/method/do",
		"http_method": "POST",
		"path": "/api/method/do",
		"duration_ms": 1000,
		"queries_count": 0,
		"query_time_ms": 0,
		"slowest_query_ms": 0,
		"call_tree_json": truncated_tree,
		"recording_uuid": "r-1",
	}
	doc = _fake_doc(
		actions=[SimpleNamespace(**action_attrs)],
		hot_frames_json=json.dumps([
			{
				"function": "my_app/work.py::do",
				"total_ms": 800,
				"occurrences": 1,
				"distinct_actions": 1,
				"action_refs": [0],
			}
		]),
		total_python_ms=800,
		total_sql_ms=0,
		total_duration_ms=1000,
	)
	html = renderer.render_raw(doc, recordings=[])
	assert "captured" in html and "850" in html, "expected captured frame count in banner"
	assert "300" in html and "top 300 by self-time" in html


def test_slow_query_cap_banner_when_more_than_max_findings():
	"""B.DI4 when more than MAX_FINDINGS slow queries clear the
	threshold, the Top Queries section must surface a 'N more
	suppressed' note."""
	# 7 user-app queries above the 200ms default slow threshold;
	# MAX_FINDINGS = 5 so 2 should be flagged as suppressed.
	queries = []
	for i in range(7):
		queries.append({
			"query_duration_ms": 300 + i,  # all > 200ms default threshold
			"normalized_query": "select * from x where id = ?",
			"callsite": "apps/my_app/views.py:42 in my_app.views.do",
			"action_idx": 0,
			"recording_uuid": "r-1",
		})
	doc = _fake_doc(top_queries_json=json.dumps(queries))
	html = renderer.render_raw(doc, recordings=[])
	# Banner copy lives in the Top Queries section.
	assert "additional quer" in html, "suppressed-count banner missing"
	assert "2" in html  # exact suppressed count (7 - 5)
