"""Selection, caps, toggles and Steps persistence through the queued worker.

The removed inline loops are replaced by post-Ready admission (isolation tests),
transactional worker execution (worker/journal tests), and these real selectors.
"""

import json
from types import SimpleNamespace

import pytest

from optimus import ai_fix, ai_prompts
from optimus.tests.test_ai_refresh_journal import journal as _journal_fixture  # noqa: F401
from optimus.tests.test_ai_refresh_work import Doc
from optimus.tests.test_ai_refresh_work import work as _work_fixture  # noqa: F401


def install_rows(work, monkeypatch, rows):
	work.journal.db.rows["Optimus Finding"] = {r.name: r for r in rows}
	monkeypatch.setattr(work.jobs.frappe, "get_doc", lambda *a: Doc(**work.parent, findings=[Doc(r) for r in rows]))


@pytest.mark.parametrize("scope,regenerate,expected", [
	("fixes_missing", False, "missing"), ("all", False, "missing"), ("all", True, "missing"),
])
def test_missing_answers_are_first_even_during_explicit_replacement(work, monkeypatch, scope, regenerate, expected):
	rows = [Doc(work.row, name="current", llm_fix_json=json.dumps({"suggestion":"saved", "prompt_version":ai_prompts.PROMPT_VERSION})),
		Doc(work.row, name="missing"), Doc(work.row, name="outdated", llm_fix_json='{"suggestion":"old","prompt_version":0}')]
	install_rows(work, monkeypatch, rows)
	work.journal.run.update(scope=scope, regenerate_all=regenerate)
	assert work.jobs._next_item(work.journal.run, {}).target == expected


@pytest.mark.parametrize("scope,regenerate,stored,selected", [
	("fixes_missing", False, {"suggestion":"old", "prompt_version":0}, False),
	("all", False, {"suggestion":"old", "prompt_version":0}, True),
	("all", False, {"suggestion":"saved", "prompt_version":ai_prompts.PROMPT_VERSION}, False),
	("all", True, {"suggestion":"saved", "prompt_version":ai_prompts.PROMPT_VERSION}, True),
])
def test_worker_obeys_automatic_vs_manual_refresh_selection(work, scope, regenerate, stored, selected):
	work.row["llm_fix_json"] = json.dumps(stored)
	work.journal.run.update(scope=scope, regenerate_all=regenerate)
	assert bool(work.jobs._next_item(work.journal.run, {})) is selected


@pytest.mark.parametrize("cap,expected", [(0, True), (1, False), (2, True)])
def test_cap_counts_already_attempted_items_across_slices(work, cap, expected):
	work.journal.run["cap"] = cap
	work.journal.db.rows[work.journal.mod.ATTEMPT] = {"old-attempt": {
		"name":"old-attempt", "run_id":"fake-run", "session_name":"fake-session-doc",
		"kind":"fix", "target_name":"another-finding", "state":"failed",
	}}
	assert bool(work.jobs._next_item(work.journal.run, {})) is expected


@pytest.mark.parametrize("section", ["findings", "humanize"])
def test_disabled_section_makes_no_prepared_provider_call(work, monkeypatch, section):
	work.journal.run.update(include_fixes=section == "findings", include_steps=section == "humanize")
	monkeypatch.setattr(ai_fix, "is_available", lambda **kw: False)
	assert work.jobs._next_item(work.journal.run, {}) is None
	assert not work.sends


def test_worker_prioritizes_severity_then_impact(work, monkeypatch):
	rows = [Doc(work.row, name="low", severity="Low", estimated_impact_ms=999),
		Doc(work.row, name="high-small", estimated_impact_ms=1),
		Doc(work.row, name="high-large", estimated_impact_ms=100)]
	install_rows(work, monkeypatch, rows)
	assert work.jobs._next_item(work.journal.run, {}).target == "high-large"


def test_empty_findings_do_not_prepare_an_ai_request(work, monkeypatch):
	install_rows(work, monkeypatch, [])
	assert work.jobs._next_item(work.journal.run, {}) is None
	assert not work.sends


def steps_item(work, monkeypatch):
	from optimus import analyze
	work.journal.run.update(include_steps=1, include_fixes=0)
	monkeypatch.setattr(analyze, "load_recordings_light", lambda *a, **kw: [{"uuid":"fake-rec"}])
	monkeypatch.setattr(analyze, "_actions_for_humanizer", lambda *a: [{"method":"POST", "path":"/api/method/fake"}])
	monkeypatch.setattr(work.jobs, "_touch_session", lambda name, **values: work.journal.db.set_value("Optimus Session", name, values))
	return work.jobs._next_item(work.journal.run, {})


def test_steps_answer_and_token_field_are_written_only_inside_settlement_transaction(work, monkeypatch):
	def humanize(actions, *, usage_out, **kw):
		assert actions and kw["session_uuid"] == "fake-session"
		usage_out.begin()
		usage_out.update(total_tokens=42)
		usage_out.observe(True)
		return "1. Open the Sales Invoice form"
	monkeypatch.setattr(ai_fix, "humanize_steps", humanize)
	item = steps_item(work, monkeypatch)
	result = item.send(10)
	assert work.parent["notes"] == "" and result["tokens"]["total_tokens"] == 42
	with work.journal.db.transaction():
		assert item.persist(result)
	assert "Open the Sales Invoice form" in work.parent["notes"]
	assert work.parent["ai_steps_tokens"] == 42


def test_steps_failure_preserves_previous_notes_without_a_partial_write(work, monkeypatch):
	def fail(*a, **kw):
		raise ai_fix.AiFixError("fake provider unavailable", kind="transport")
	monkeypatch.setattr(ai_fix, "humanize_steps", fail)
	work.parent["notes"] = "Previous useful steps"
	item = steps_item(work, monkeypatch)
	with pytest.raises(ai_fix.AiFixError):
		item.send(10)
	assert work.parent["notes"] == "Previous useful steps"
	assert "ai_steps_tokens" not in work.parent
