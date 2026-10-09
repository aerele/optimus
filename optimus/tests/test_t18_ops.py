# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Operability, deployment and docs fixes. The failure lines still
go through ``safe_call.log_error_line``, the texts point at the log line to send, and the
docs and the file map say what the code does."""

import os
from types import SimpleNamespace

import pytest

from optimus import ai_fix, ai_grounding, analyze, safe_call
from optimus.analyzers import base as analyzer_base
from optimus.analyzers import redundant_calls
from optimus.renderer import recipe_enrichment

_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _read(*parts):
	with open(os.path.join(_ROOT, *parts), encoding="utf-8") as f:
		return f.read()


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


def test_the_refresh_toast_says_nothing_to_refresh_only_when_nothing_failed():
	js = _read("optimus", "optimus", "doctype", "optimus_session", "optimus_session.js")
	start = js.index("const failed = fx.failed || 0;")
	assert start < js.index("Nothing to refresh.")
	assert ": failed" in js[start:js.index("Nothing to refresh.")]


def test_evidence_failures_past_the_cap_get_one_summary_line(lines, monkeypatch):
	monkeypatch.setattr(recipe_enrichment, "_read_table_evidence", _boom)
	lookup = recipe_enrichment.make_evidence_lookup()
	for i in range(13):
		lookup(f"tabT{i}")
	assert len(lines) == recipe_enrichment.MAX_LOGGED_EVIDENCE_FAILURES
	lookup.log_unlisted_failures()
	lookup.log_unlisted_failures()  # deduped
	assert lines[recipe_enrichment.MAX_LOGGED_EVIDENCE_FAILURES:] == [
		"optimus: evidence read failed for 3 more tables (only the first 10 are listed above)",
	]


def test_no_summary_line_when_every_failure_was_listed(lines, monkeypatch):
	monkeypatch.setattr(recipe_enrichment, "_read_table_evidence", _boom)
	lookup = recipe_enrichment.make_evidence_lookup()
	lookup("tabA")
	lookup.log_unlisted_failures()
	assert len(lines) == 1


def test_the_render_and_the_export_log_the_unlisted_failures():
	assert "log_unlisted_failures()" in _read("optimus", "renderer", "_internal.py")
	assert "log_unlisted_failures()" in _read("optimus", "api.py")


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
	assert "frappe.logger" not in _source_of(analyze._note_heartbeat_problem)


def test_recipe_failures_go_through_log_error_line(lines):
	recipe_enrichment.log_recipe_failures(2)
	recipe_enrichment.log_recipe_failures(2)
	assert lines == ["optimus: index advice failed for 2 finding(s) or table(s) in one render"]
	assert "frappe.logger" not in _source_of(recipe_enrichment.log_recipe_failures)


def _source_of(fn):
	import inspect

	return inspect.getsource(fn)


def test_a_run_that_never_held_the_flag_does_not_say_it_no_longer_holds_it():
	src = _read("optimus", "analyze.py")
	assert "no longer holds" not in src
	assert "this run does not hold it" in src


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


def test_uninstall_text_names_the_right_bench_commands():
	for text in (_read("docs", "AI-FIXING.md"), _read("CHANGELOG.md")):
		flat = " ".join(text.split())
		assert "`bench --site <site> uninstall-app` (and later `bench remove-app`) removes neither" in flat
		assert "`bench remove-app` removes neither" not in flat
	assert "before_uninstall" in _read("docs", "AI-FIXING.md")


def test_the_file_map_lists_the_split_modules():
	text = _read("docs", "AI-FIXING.md")
	section = text[text.index("## 9. Where the code lives"):text.index("## 10.")]
	for needle in ("where_scan.py", "ensure_indexes_template.py", "index_evidence.py"):
		assert needle in section, needle


def test_the_runbook_names_the_lock_wait_cause_and_the_console_line():
	text = _read("docs", "AI-FIXING.md")
	section = text[text.index("### 6.5 Troubleshooting"):text.index("## 7. Threat model")]
	assert "300 s" in section and "quiet window" in section
	assert "could not be written either" in section


def test_the_docs_say_the_postgres_restore_is_committed():
	flat = " ".join(_read("docs", "AI-FIXING.md").split())
	assert "commits right after it puts the old lock setting back" in flat


def test_a_leading_apps_dir_counts_as_the_bench_dir():
	assert analyzer_base.cut_at_bench_apps("apps/myapp/myapp/x.py") == "myapp/myapp/x.py"
	assert analyzer_base._last_app_segment("apps/myapp/myapp/x.py") == "myapp"
	assert ai_grounding._short_path("apps/myapp/x.py") == "myapp/x.py"
	assert analyzer_base.cut_at_bench_apps("myapp/apps/x.py") == "x.py"  # a package, as before the helper
	assert analyzer_base._last_app_segment("myapp/apps/x.py") is None
	assert ai_grounding._short_path("/srv/other/x.py") == "x.py"
