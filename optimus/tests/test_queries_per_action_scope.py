# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Optimus-own queries stay out of the report; Queries per action follows Tracked Apps."""

import copy

from optimus.analyzers import per_action, table_breakdown, top_queries
from optimus.analyzers.base import (
	is_frappe_path,
	is_optimus_path,
	is_profiler_own_query,
	strip_profiler_own_calls,
	walk_callsite_str,
)
from optimus.renderer.queries_per_action import build_queries_per_action


def _frame(filename, lineno=10):
	return {"filename": filename, "lineno": lineno, "function": "f"}


OWN_STACK = [
	_frame("apps/optimus/optimus/infra_capture.py", 150),
	_frame("apps/frappe/frappe/database/database.py", 200),
]
FRAMEWORK_STACK = [_frame("apps/frappe/frappe/model/document.py", 80)]
CUSTOM_STACK = [
	_frame("apps/myapp/myapp/api.py", 42),
	_frame("apps/frappe/frappe/database/database.py", 200),
]
OTHER_APP_STACK = [_frame("apps/otherapp/otherapp/x.py", 7)]


def _call(query, duration, stack):
	return {
		"query": query,
		"normalized_query": query,
		"duration": duration,
		"stack": stack,
		"normalized_copies": 1,
	}


def _recording():
	return {
		"uuid": "r1",
		"cmd": "myapp.api.go",
		"method": "POST",
		"path": "/api/method/myapp.api.go",
		"duration": 500,
		"calls": [
			_call("SHOW GLOBAL STATUS", 900, OWN_STACK),
			_call("select name from `tabItem`", 30, CUSTOM_STACK),
			_call("select name from `tabDocType`", 20, FRAMEWORK_STACK),
			_call("select name from `tabFoo`", 10, OTHER_APP_STACK),
		],
	}


def _action(rec):
	return {"recording_uuid": rec["uuid"], "action_label": "myapp.api.go"}


def _rows(rec, tracked=()):
	return build_queries_per_action([_action(rec)], {rec["uuid"]: rec}, tracked)


def test_is_optimus_path_anchors_on_path_boundary():
	assert is_optimus_path("optimus/capture.py")
	assert is_optimus_path("apps/optimus/optimus/capture.py")
	assert is_optimus_path("/home/u/bench/apps/optimus/optimus/x.py")
	assert not is_optimus_path("apps/myoptimus/myoptimus/x.py")
	assert not is_optimus_path("apps/foo_optimus/foo_optimus/x.py")
	assert not is_optimus_path("")


def test_user_app_optimus_folder_is_not_the_profiler():
	assert not is_optimus_path("foo/optimus/billing.py")
	assert not is_optimus_path("/home/u/bench/apps/foo/optimus/billing.py")
	assert is_optimus_path("env/lib/python3.14/site-packages/optimus/ai_fix.py")
	stack = [_frame("foo/optimus/billing.py"), _frame("frappe/database/database.py")]
	assert not is_profiler_own_query(stack)
	assert walk_callsite_str(stack) == "foo/optimus/billing.py:10"


def test_frappe_path_matches_on_boundary_only():
	assert is_frappe_path("frappe/database/database.py")
	assert is_frappe_path("apps/frappe/frappe/model/document.py")
	assert not is_frappe_path("apps/myfrappe/myfrappe/x.py")
	assert not is_frappe_path("x_frappe/api.py")
	stack = [_frame("myfrappe/api.py", 3), _frame("frappe/database/database.py")]
	assert walk_callsite_str(stack) == "myfrappe/api.py:3"


def test_user_frame_between_keeps_query():
	stack = [_frame("apps/myapp/myapp/a.py"), *OWN_STACK]
	assert not is_profiler_own_query(stack)


def test_strip_removes_only_own_calls_and_counts():
	rec = _recording()
	assert strip_profiler_own_calls(rec) == 1
	assert len(rec["calls"]) == 3
	assert strip_profiler_own_calls(rec) == 0  # idempotent


def test_per_action_totals_exclude_optimus_queries():
	rec = _recording()
	strip_profiler_own_calls(rec)
	action = per_action._build_action(rec)
	assert action["queries_count"] == 3
	assert action["query_time_ms"] == 60.0  # the 900ms SHOW GLOBAL STATUS is gone
	assert action["slowest_query_ms"] == 30.0


def test_other_analyzers_never_see_optimus_queries(empty_context):
	rec = _recording()
	strip_profiler_own_calls(rec)
	top = top_queries.analyze([rec], empty_context).aggregate["top_queries"]
	assert all("SHOW GLOBAL" not in q["normalized_query"] for q in top)
	tables = table_breakdown.analyze([copy.deepcopy(rec)], empty_context)
	assert "SHOW GLOBAL" not in str(tables.aggregate)


def test_section_exclusion_mode_hides_only_optimus():
	out = _rows(_recording())
	queries = [r["duration"] for r in out["rows"]]
	assert queries == [30, 20, 10]  # optimus 900ms row is absent
	assert out["hidden_untracked"] == 0


def test_section_inclusion_mode_lists_only_tracked_apps():
	out = _rows(_recording(), tracked=("myapp",))
	assert [r["duration"] for r in out["rows"]] == [30]
	assert out["rows"][0]["callsite"] == "apps/myapp/myapp/api.py:42"
	assert out["hidden_untracked"] == 2


def test_section_never_shows_optimus_even_if_it_is_tracked():
	out = _rows(_recording(), tracked=("optimus", "myapp"))
	assert all("optimus" not in r["callsite"] for r in out["rows"])
	assert 900 not in [r["duration"] for r in out["rows"]]


def test_section_callsite_is_user_frame_not_innermost_framework_frame():
	out = _rows(_recording())
	callsites = {r["duration"]: r["callsite"] for r in out["rows"]}
	assert callsites[30] == "apps/myapp/myapp/api.py:42"


def test_section_handles_missing_recording_and_empty_calls():
	assert _rows({"uuid": "x", "calls": []})["rows"] == []
	out = build_queries_per_action([{"recording_uuid": "gone"}], {}, ())
	assert out["rows"] == [] and out["total"] == 0


def test_section_caps_rows_and_reports_total():
	rec = {
		"uuid": "r2",
		"calls": [_call(f"q{i}", i + 1, CUSTOM_STACK) for i in range(30)],
	}
	acts = [{"recording_uuid": "r2", "action_label": f"a{i}"} for i in range(3)]
	out = build_queries_per_action(acts, {"r2": rec}, ())
	assert len(out["rows"]) == 40
	assert out["total"] == 90  # every visible query, not the per-action top 20


def _render(recordings, tracked, monkeypatch):
	import types

	from optimus import renderer, settings

	monkeypatch.setattr(settings, "get_tracked_apps", lambda: tuple(tracked))
	action = types.SimpleNamespace(
		action_label="savedocs:Save", event_type="HTTP Request", http_method="POST",
		path="/api/method/x", recording_uuid="r1", duration_ms=500.0, queries_count=3,
		query_time_ms=60.0, slowest_query_ms=30.0,
	)
	doc = types.SimpleNamespace(
		name="PS", title="T", session_uuid="t", user="a", status="Ready",
		started_at="2026-04-17", stopped_at="2026-04-17", notes=None, top_severity="Low",
		total_duration_ms=1000, total_query_time_ms=60, total_queries=3, total_requests=1,
		summary_html=None, top_queries_json="[]", table_breakdown_json="[]",
		hot_frames_json="[]", session_time_breakdown_json="{}", total_python_ms=0,
		total_sql_ms=0, analyzer_warnings=None, v5_aggregate_json="{}",
		actions=[action], findings=[], phase_2_runs=[],
	)
	return renderer.render(doc, recordings=recordings)


def test_rendered_report_has_no_optimus_query_exclusion_mode(monkeypatch):
	html = _render([_recording()], (), monkeypatch)
	assert "infra_capture.py" not in html
	assert "SHOW GLOBAL STATUS" not in html
	assert "myapp/api.py:42" in html


def test_rendered_report_inclusion_mode_hides_untracked_apps(monkeypatch):
	html = _render([_recording()], ("myapp",), monkeypatch)
	assert "infra_capture.py" not in html
	assert "SHOW GLOBAL STATUS" not in html
	assert "otherapp/x.py:7" not in html
	assert "myapp/api.py:42" in html


def test_rendered_report_hides_section_when_no_queries_and_nothing_hidden(monkeypatch):
	rec = _recording()
	rec["calls"] = [rec["calls"][0]]
	html = _render([rec], (), monkeypatch)
	assert "Queries per action" not in html


def test_rendered_report_says_no_tracked_queries_when_all_hidden(monkeypatch):
	html = _render([_recording()], ("nothere",), monkeypatch)
	assert "No queries from tracked apps." in html
	assert "not shown" in html


def test_callsite_treats_myoptimus_app_as_user_code():
	from optimus.analyzers.base import walk_callsite_str

	stack = [_frame("apps/myoptimus/myoptimus/a.py", 5), *OWN_STACK]
	assert walk_callsite_str(stack) == "apps/myoptimus/myoptimus/a.py:5"
