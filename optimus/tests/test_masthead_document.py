# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Masthead top-right shows the session title. (The touched-document doctype/name
display was reverted these tests guard that it doesn't creep back.)"""

import types

from optimus import renderer


def _doc(actions):
	return types.SimpleNamespace(
		name="PS-TEST", title="Sales Order · 2026-08-27 21:55", session_uuid="t",
		user="u@x", status="Ready", started_at="2026-08-27 21:55:00",
		stopped_at="2026-08-27 21:55:30", notes=None, top_severity="Low",
		total_duration_ms=1, total_query_time_ms=0, total_queries=0, total_requests=1,
		summary_html=None, top_queries_json="[]", table_breakdown_json="[]",
		hot_frames_json="[]", session_time_breakdown_json="{}", total_python_ms=0,
		total_sql_ms=0, analyzer_warnings=None, v5_aggregate_json="{}",
		actions=actions, findings=[], phase_2_runs=[],
	)


def _action():
	return types.SimpleNamespace(
		action_label="Save", event_type="HTTP Request", http_method="POST",
		path="/api/method/frappe.client.save", recording_uuid="u1",
		duration_ms=1, queries_count=0, query_time_ms=0, slowest_query_ms=0,
	)


def test_masthead_shows_session_title_even_with_a_document():
	"""Even when the session touched a document, the top-right shows the session
	title the doctype/name display was reverted."""
	rec = {"uuid": "u1", "form_dict": {"doctype": "Sales Order", "name": "SAL-ORD-2026-00042"}, "calls": []}
	html = renderer.render(_doc([_action()]), recordings=[rec])
	assert "Sales Order · 2026-08-27 21:55" in html  # composed session title
	assert 'class="masthead-docname"' not in html
	assert "<h1>Sales Order</h1>" not in html  # doctype is NOT the masthead heading
	# (the document name may still appear in the per-action breadcrumb only the
	#  masthead top-right display was reverted, not the per-action document line.)


def test_masthead_without_a_document_uses_session_title():
	"""No touched document → the composed session title is used, no doctype line."""
	html = renderer.render(_doc([]), recordings=[])
	assert "Sales Order · 2026-08-27 21:55" in html
	assert 'class="masthead-docname"' not in html
