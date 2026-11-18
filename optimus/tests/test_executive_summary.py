# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Render-level tests for the first screen of the report: the KPI "Issues found"
cell and the clean-session "Nothing to fix" hero."""

import json
import types


class TestEndToEndRender:
	def test_card_renders_into_html(self):
		from optimus import renderer

		doc = types.SimpleNamespace()
		doc.title = "Test"
		doc.session_uuid = "t"
		doc.user = "a"
		doc.status = "Ready"
		doc.started_at = "2026-04-14"
		doc.stopped_at = "2026-04-14"
		doc.notes = None
		doc.top_severity = "High"
		doc.total_duration_ms = 6000
		doc.total_query_time_ms = 0
		doc.total_queries = 100
		doc.total_requests = 10
		doc.summary_html = None
		doc.top_queries_json = "[]"
		doc.table_breakdown_json = "[]"
		doc.hot_frames_json = "[]"
		doc.session_time_breakdown_json = "{}"
		doc.total_python_ms = 0
		doc.total_sql_ms = 0
		doc.analyzer_warnings = None
		doc.v5_aggregate_json = json.dumps({
			"infra_summary": {"rss_delta": 90_000_000}
		})
		doc.actions = []

		finding_row = types.SimpleNamespace()
		finding_row.finding_type = "N+1 Query"
		finding_row.severity = "High"
		finding_row.title = "Same query ran 50× at myapp/foo.py:10"
		finding_row.customer_description = "desc"
		finding_row.estimated_impact_ms = 500.0
		finding_row.affected_count = 50
		finding_row.action_ref = "0"
		finding_row.technical_detail_json = json.dumps({
			"callsite": {"filename": "apps/myapp/foo.py", "lineno": 10}
		})
		doc.findings = [finding_row]

		html = renderer.render(doc, recordings=[])

		# v0.7.x redesign Phase B: the "At a glance" exec-summary card
		# was replaced by the TL;DR hero (single composed headline); the
		# assertions below hold via the TL;DR composition + the KPI strip.
		assert 'class="tldr"' in html, "TL;DR hero must render"
		# Duration crosses the 1000ms threshold rendered as seconds
		# with .time-high (timing rule applies everywhere).
		assert '<span class="time-high">6.00s</span>' in html
		assert "operation" in html
		# Top finding title surfaced TL;DR fallback branch wraps it.
		assert "Same query ran 50× at myapp/foo.py:10" in html
		# v0.7.x: KPI strip (replaces stat cards) plainly-labelled cells,
		# and the "Issues found" cell's big number is the TOTAL finding
		# count (not the High count), with a severity breakdown that sums
		# to it.
		assert "Issues found" in html and "Database queries" in html
		import re as _re
		m = _re.search(
			r'Issues found</div>\s*<div class="kpi-value[^"]*">(.*?)</div>\s*<div class="kpi-sub">(.*?)</div>',
			html, _re.S,
		)
		assert m, "could not locate the 'Issues found' KPI cell"
		assert _re.sub(r"<[^>]+>", "", m.group(1)).strip() == "1"  # one finding total
		assert "1 high" in _re.sub(r"<[^>]+>", "", m.group(2))

	def test_clean_session_no_card(self):
		from optimus import renderer

		doc = types.SimpleNamespace()
		doc.title = "Clean"
		doc.session_uuid = "t"
		doc.user = "a"
		doc.status = "Ready"
		doc.started_at = "2026-04-14"
		doc.stopped_at = "2026-04-14"
		doc.notes = None
		doc.top_severity = "Low"
		doc.total_duration_ms = 500
		doc.total_query_time_ms = 0
		doc.total_queries = 5
		doc.total_requests = 1
		doc.summary_html = None
		doc.top_queries_json = "[]"
		doc.table_breakdown_json = "[]"
		doc.hot_frames_json = "[]"
		doc.session_time_breakdown_json = "{}"
		doc.total_python_ms = 0
		doc.total_sql_ms = 0
		doc.analyzer_warnings = None
		doc.v5_aggregate_json = "{}"
		doc.actions = []
		doc.findings = []

		html = renderer.render(doc, recordings=[])

		# v0.7.x redesign Phase B: the exec-summary card was replaced
		# by the TL;DR hero. The hero ALWAYS renders clean sessions
		# get a "Nothing to fix" branch instead of being hidden.
		assert 'class="exec-summary' not in html, (
			"Old exec-summary card markup must not be present"
		)
		assert 'class="tldr"' in html, "TL;DR hero must render even when clean"
		assert "Nothing to fix" in html, (
			"Clean-session TL;DR must use the 'Nothing to fix' headline"
		)
