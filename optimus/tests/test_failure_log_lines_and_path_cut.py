# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Operability fixes. The failure lines still go through ``safe_call.log_error_line``
and the texts point at the log line to send."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from optimus import ai_fix, ai_grounding, analyze, api, renderer, safe_call
from optimus.analyzers import base as analyzer_base
from optimus.analyzers import redundant_calls
from optimus.renderer import recipe_enrichment


@pytest.fixture
def lines(monkeypatch):
	import frappe

	out = []
	monkeypatch.setattr(
		frappe, "logger", lambda *a, **k: SimpleNamespace(error=out.append, warning=out.append, info=out.append),
		raising=False,
	)
	monkeypatch.setattr(safe_call, "_RECENT_LINES", {})
	monkeypatch.setattr(analyze, "_heartbeat_noted", set(), raising=False)
	return out


def _boom(*a, **k):
	raise RuntimeError("secret detail")


def test_the_gate_note_names_the_log_line_to_send():
	note = ai_grounding.GATE_CHECK_FAILED_NOTE
	assert (
		'If it keeps happening, send the bench log line "optimus: hot-line gate failed" to the Optimus maintainers.'
		in note
	)




@pytest.mark.parametrize("failed, summary", [
	(13, "optimus: evidence read failed for 3 more tables (only the first 10 are listed above)"),
	(11, "optimus: evidence read failed for 1 more table (only the first 10 are listed above)"),
	(1, None),
])
def test_evidence_failures_past_the_cap_get_one_summary_line(lines, monkeypatch, failed, summary):
	monkeypatch.setattr(recipe_enrichment, "_read_table_evidence", _boom)
	lookup = recipe_enrichment.make_evidence_lookup()
	for i in range(failed):
		lookup(f"tabT{i}")
	assert len(lines) == min(failed, recipe_enrichment.MAX_LOGGED_EVIDENCE_FAILURES)
	lookup.log_unlisted_failures()
	lookup.log_unlisted_failures()  # deduped
	listed = min(failed, recipe_enrichment.MAX_LOGGED_EVIDENCE_FAILURES)
	assert lines[listed:] == ([summary] if summary else [])


_SUMMARY = "optimus: evidence read failed for 1 more table (only the first 10 are listed above)"


def _missing_index_finding(i):
	return {"finding_type": "Missing Index", "technical_detail": {
		"table": f"tabT{i}", "column": "po_no", "suggested_ddl": f"ALTER TABLE `tabT{i}` ADD INDEX (po_no);",
	}}


def test_the_render_and_the_export_log_the_unlisted_failures(lines, monkeypatch):
	monkeypatch.setattr(recipe_enrichment, "_read_table_evidence", _boom)
	over_the_cap = recipe_enrichment.MAX_LOGGED_EVIDENCE_FAILURES + 1
	rows = [
		SimpleNamespace(
			finding_type="Missing Index", severity="High", title=f"Missing Index {i}", customer_description="d",
			estimated_impact_ms=300.0, affected_count=5, action_ref="", llm_fix_json=None,
			technical_detail_json=json.dumps({
				"table": f"tabT{i}", "column": "po_no",
				"callsite": {"filename": "apps/myapp/myapp/api.py", "lineno": 12, "function": "load"},
			}),
		)
		for i in range(over_the_cap)
	]
	doc = SimpleNamespace(
		name="PS-1", session_uuid="u", title="t", user="a@example.com", status="Ready",
		started_at="2026-10-08T00:00:00", stopped_at="2026-10-08T00:00:05", notes=None, top_severity="High",
		summary_html=None, total_duration_ms=1, total_query_time_ms=1, total_queries=1, total_requests=1,
		top_queries_json="[]", table_breakdown_json="[]", hot_frames_json=None, session_time_breakdown_json=None,
		total_python_ms=None, total_sql_ms=None, analyzer_warnings=None, v5_aggregate_json="{}", actions=[],
		findings=rows, phase_2_runs=[],
	)
	with patch("optimus.settings.get_ignored_apps", return_value=()):
		renderer.render_raw(doc, recordings=[])
	assert lines.count(_SUMMARY) == 1

	lines.clear()
	safe_call._RECENT_LINES.clear()
	api._export_index_advice([_missing_index_finding(i) for i in range(over_the_cap)], [])
	assert lines.count(_SUMMARY) == 1


def test_a_loop_facts_crash_leaves_one_deduped_line(lines, monkeypatch):
	monkeypatch.setattr(ai_grounding, "loop_facts_from_window", _boom)
	finding = {
		"finding_type": "N+1 Query",
		"technical_detail": {"callsite": {"lineno": 2}},
		"source_window": [{"lineno": 1, "is_target": False}, {"lineno": 2, "is_target": True}],
	}
	for _ in range(2):
		ai_fix._loop_facts_text(finding)
	assert lines == ["optimus: AI loop facts failed: RuntimeError"]


def test_an_ai_path_index_advice_crash_leaves_one_deduped_line(lines, monkeypatch):
	from optimus.renderer import index_recipes

	monkeypatch.setattr(index_recipes, "advise_finding", _boom)
	for _ in range(2):
		analyze._attach_index_advice({"finding_type": "Slow Query"}, lambda t: None, ())
	assert lines == ["optimus: AI index advice failed: RuntimeError"]


def test_the_heartbeat_note_goes_through_log_error_line(lines):
	analyze._note_heartbeat_problem("s1", "x")
	analyze._note_heartbeat_problem("s1", "y")
	assert lines == ["optimus: analyze s1: x"]
	safe_call._RECENT_LINES.clear()
	analyze._note_heartbeat_problem("s2", "x")
	assert lines == ["optimus: analyze s1: x", "optimus: analyze s2: x"]


def test_recipe_failures_go_through_log_error_line(lines):
	recipe_enrichment.log_recipe_failures(2)
	recipe_enrichment.log_recipe_failures(2)
	assert lines == ["optimus: index advice failed for 2 finding(s) or table(s) in one render"]


@pytest.mark.parametrize("path", [
	"/home/u/bench/apps/myapp/myapp/apps/x.py",
	"/home/u/apps/bench/apps/myapp/myapp/doctype/x.py",
	"/home/u/bench/apps/myapp/myapp/x.py",
])
def test_one_shared_cut_at_bench_apps_helper(path):
	cut = analyzer_base.cut_at_bench_apps(path)
	assert cut.startswith("myapp/myapp/")
	assert analyzer_base._last_app_segment(path) == "myapp"
	assert ai_grounding._short_path(path) == cut
	assert not hasattr(redundant_calls, "_cut_at_bench_apps")


def test_the_cut_helper_falls_back_to_the_last_apps_dir():
	assert analyzer_base.cut_at_bench_apps("/b/apps/pkg/mod.py") == "pkg/mod.py"
	assert analyzer_base.cut_at_bench_apps("/b/apps/a/apps/pkg/mod.py") == "pkg/mod.py"


def test_a_leading_apps_dir_counts_as_the_bench_dir():
	assert analyzer_base.cut_at_bench_apps("apps/myapp/myapp/x.py") == "myapp/myapp/x.py"
	assert analyzer_base._last_app_segment("apps/myapp/myapp/x.py") == "myapp"
	assert ai_grounding._short_path("apps/myapp/x.py") == "myapp/x.py"
	assert analyzer_base.cut_at_bench_apps("myapp/apps/x.py") == "x.py"  # a package, as before the helper
	assert analyzer_base._last_app_segment("myapp/apps/x.py") is None
	assert ai_grounding._short_path("/srv/other/x.py") == "x.py"
