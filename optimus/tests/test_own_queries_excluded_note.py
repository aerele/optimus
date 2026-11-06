# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The report says, subtly, how many of Optimus's own queries it left out, so a wrongly hidden query shows."""

import html as htmllib
import re
import types

from optimus import renderer
from optimus.analyzers.base import OWN_EXCLUDED_KEY, strip_own_calls, strip_profiler_own_calls

USER = {"filename": "myapp/myapp/api.py", "lineno": 3, "function": "attach"}
SQL = {"filename": "frappe/frappe/database/database.py", "lineno": 200, "function": "sql"}
PROTECT = {"filename": "optimus/optimus/managed.py", "lineno": 234, "function": "protect_file"}
UNREGISTERED = {"filename": "optimus/optimus/new_feature.py", "lineno": 30, "function": "forward"}
LISTS = {"tracked_apps": (), "installed_apps": frozenset({"frappe", "optimus", "myapp"})}


def _session_doc():
	doc = types.SimpleNamespace()
	for name, value in {
		"title": "Test session", "session_uuid": "test-uuid", "user": "alice@example.com", "status": "Ready",
		"started_at": "2026-04-14 10:00:00", "stopped_at": "2026-04-14 10:02:00", "notes": None,
		"top_severity": "Medium", "total_duration_ms": 2000, "total_query_time_ms": 500, "total_queries": 20,
		"total_requests": 2, "summary_html": None, "top_queries_json": "[]", "table_breakdown_json": "[]",
		"hot_frames_json": "[]", "session_time_breakdown_json": "{}", "total_python_ms": 100,
		"total_sql_ms": 500, "analyzer_warnings": None, "v5_aggregate_json": "{}", "actions": [], "findings": [],
	}.items():
		setattr(doc, name, value)
	return doc


def _recording(uuid, own=0, user=0, stamped=0):
	calls = [{"query": "select own", "stack": [USER, PROTECT, SQL]} for _ in range(own)]
	calls += [{"query": "select user", "stack": [USER, SQL]} for _ in range(user)]
	rec = {"uuid": uuid, "calls": calls}
	if stamped:
		rec[OWN_EXCLUDED_KEY] = stamped
	return rec


def _note(html):
	match = re.search(r'<p class="own-excluded-note[^>]*>(.*?)</p>', html, re.S)
	return " ".join(htmllib.unescape(match.group(1)).split()) if match else None


class TestStripStampsTheCount:
	def test_the_strip_records_how_many_it_dropped_on_the_recording(self):
		rec = _recording("r", own=3, user=1)
		strip_own_calls(rec)
		assert rec[OWN_EXCLUDED_KEY] == 3 and len(rec["calls"]) == 1

	def test_a_second_strip_adds_nothing(self):
		rec = _recording("r", own=3)
		strip_profiler_own_calls(rec)
		strip_profiler_own_calls(rec)
		assert rec[OWN_EXCLUDED_KEY] == 3

	def test_a_recording_that_dropped_nothing_is_left_unmarked(self):
		rec = _recording("r", user=2)
		strip_own_calls(rec)
		assert OWN_EXCLUDED_KEY not in rec

	def test_a_kept_suspicious_call_is_not_counted_as_excluded(self):
		rec = {"uuid": "r", "calls": [{"query": "q", "stack": [USER, UNREGISTERED, SQL]}]}
		strip_own_calls(rec, **LISTS)
		assert OWN_EXCLUDED_KEY not in rec and len(rec["calls"]) == 1


class TestReportNote:
	def test_a_count_stamped_before_render_shows_in_the_note(self):
		html = renderer.render(_session_doc(), recordings=[_recording("r1", stamped=5), _recording("r2", stamped=2)])
		assert _note(html) == "7 queries from Optimus's own code were excluded from this report"

	def test_own_calls_the_renderer_strips_itself_are_counted_too(self):
		html = renderer.render(_session_doc(), recordings=[_recording("r1", own=4, user=2)])
		assert _note(html) == "4 queries from Optimus's own code were excluded from this report"

	def test_stamped_and_rendered_counts_add_up(self):
		html = renderer.render(_session_doc(), recordings=[_recording("r1", own=2, stamped=3)])
		assert _note(html).startswith("5 queries")

	def test_one_query_reads_in_the_singular(self):
		html = renderer.render(_session_doc(), recordings=[_recording("r1", stamped=1)])
		assert _note(html) == "1 query from Optimus's own code was excluded from this report"

	def test_no_note_when_nothing_was_excluded(self):
		html = renderer.render(_session_doc(), recordings=[_recording("r1", user=3)])
		assert "own-excluded-note" not in html and "excluded from this report" not in html

	def test_the_note_is_informational_not_a_warning(self):
		html = renderer.render(_session_doc(), recordings=[_recording("r1", stamped=9)])
		note_tag = re.search(r'<p class="own-excluded-note[^>]*>', html).group(0)
		assert "muted" in note_tag and "warn" not in note_tag and "truncation" not in note_tag

	def test_the_note_text_goes_through_translation(self, monkeypatch):
		import frappe

		monkeypatch.setattr(frappe, "_", lambda msg, *a, **k: "T:" + msg, raising=False)
		html = renderer.render(_session_doc(), recordings=[_recording("r1", stamped=5)])
		assert "T:{0} queries from Optimus's own code were excluded from this report" not in html
		assert "T:" in _note(html)
