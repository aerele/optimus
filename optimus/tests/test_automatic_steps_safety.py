"""Automatic consent, captured-note provenance and retry boundaries."""

from types import SimpleNamespace

import pytest

from optimus import ai_guardrails, analyze
from optimus.auto_steps import notes_hash
from optimus.tests.fixtures.ai_refresh_journal import journal as _journal_fixture
from optimus.tests.fixtures.ai_refresh_work import work as _work_fixture


def auto(work, monkeypatch):
	work.journal.run.update(scope="fixes_missing", include_fixes=0, include_steps=1)
	work.parent["notes"] = "captured steps"
	work.parent["auto_notes_sha256"] = notes_hash(work.parent["notes"])
	monkeypatch.setattr(analyze, "actions_for_humanizer", lambda recordings: [{"label": "Synthetic action"}])
	return work.journal.run


def test_only_fresh_generated_notes_can_be_rewritten_automatically(work, monkeypatch):
	run = auto(work, monkeypatch)
	assert work.jobs._next_item(run, {}).kind == "steps"
	work.parent["notes"] += " my edits"
	assert work.jobs._next_item(run, {}) is None
	assert run["steps_state"] == "kept"


def test_master_consent_is_rechecked_before_the_next_item(work, monkeypatch):
	run = auto(work, monkeypatch)
	assert work.jobs._next_item(run, {}).kind == "steps"
	monkeypatch.setattr(work.jobs, "_consent_enabled", lambda field: field != "ai_auto_suggest")
	with pytest.raises(work.jobs.StopRefresh, match="disabled"):
		work.jobs._next_item(run, {})


def test_section_consent_is_rechecked_under_the_send_lock(work, monkeypatch):
	run = auto(work, monkeypatch)
	item = work.jobs._next_item(run, {})
	monkeypatch.setattr(work.jobs, "_consent_enabled", lambda field: field != "ai_humanize_steps")
	with work.journal.db.transaction():
		assert item.valid() is False


def test_automatic_followup_does_not_repeat_an_unknown_target_with_new_input(work, monkeypatch):
	run = auto(work, monkeypatch)
	item = work.jobs._next_item(run, {})
	work.journal.db.rows[work.journal.mod.ATTEMPT] = {
		"older": {
			"name": "older",
			"run_id": "older-run",
			"session_name": work.parent["name"],
			"kind": "steps",
			"target_name": item.target,
			"input_hash": item.input_hash,
			"state": "uncertain",
		}
	}
	work.parent["title"] = "new title changes fingerprint"
	assert work.jobs._next_item(run, {}) is None
	assert run["steps_state"] == "uncertain"


def test_legacy_failed_timeout_is_blocked_by_the_same_predicate_as_the_ui(work, monkeypatch):
	run = auto(work, monkeypatch)
	item = work.jobs._next_item(run, {})
	work.journal.db.rows[work.journal.mod.ATTEMPT] = {
		"older": {
			"name": "older",
			"run_id": "older-run",
			"session_name": work.parent["name"],
			"kind": "steps",
			"target_name": item.target,
			"input_hash": item.input_hash,
			"state": "failed",
			"error_kind": "timeout",
			"send_certainty": None,
		}
	}
	assert len(work.jobs.uncertain_attempts(work.parent["name"])) == 1
	assert work.jobs._next_item(run, {}) is None
	run["retry_uncertain"] = 1
	assert work.jobs._next_item(run, {}).kind == "steps"


def test_a_note_edit_during_the_call_fences_the_answer(work, monkeypatch):
	run = auto(work, monkeypatch)
	item = work.jobs._next_item(run, {})
	work.parent["notes"] = "User changes during HTTP"
	work.parent["modified"] = "new timestamp"
	with work.journal.db.transaction():
		assert not item.persist({"notes": "AI response", "tokens": {"total_tokens": 9}})
	assert work.parent["notes"] == "User changes during HTTP"
