# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for ``optimus.api.refill_ai_suggestions``.

The endpoint chains three helpers (``_run_ai_backfill``,
``_humanize_steps_core``, ``_refill_indexes_for_doc``) and re-renders once at
the end. Covers the happy path (each step runs once, status dict surfaced),
toggle-off sections (skipped, no helper call) and gate failures (missing
provider / non-Ready / non-owning user all raise before any work).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from optimus import api


def _cfg(**overrides):
	"""Build a minimal OptimusConfig-like namespace with all three
	toggles on by default."""
	defaults = {
		"ai_suggest_findings": True,
		"ai_humanize_steps": True,
		"ai_suggest_indexes": True,
	}
	defaults.update(overrides)
	return SimpleNamespace(**defaults)


def _row(name="opt-xxx", user="user@example.com", status="Ready", title="t"):
	return {"name": name, "user": user, "status": status, "title": title}


def _fake_doc():
	"""Minimal Optimus Session doc: only ``name`` is touched (helpers are
	mocked)."""
	return SimpleNamespace(name="opt-xxx", actions=[], findings=[], table_breakdown_json="[]")


@pytest.fixture
def mock_session_environment(monkeypatch):
	"""Patch the auth + lookup boundary so the orchestration tests can
	focus on the helper chaining without standing up a real DB."""
	monkeypatch.setattr(api, "_require_profiler_user", lambda: "user@example.com")

	# frappe.db.get_value / frappe.get_doc / frappe.get_roles
	fake_frappe = MagicMock()
	fake_frappe.db.get_value.return_value = _row()
	fake_frappe.get_doc.return_value = _fake_doc()
	fake_frappe.get_roles.return_value = ["System Manager"]
	# .throw must actually raise so the gate-failure tests detect the
	# error path; mirror frappe's API of throw(msg, exc=Exception).
	def _throw(msg, exc=Exception):
		raise exc(msg)
	fake_frappe.throw.side_effect = _throw
	fake_frappe.PermissionError = PermissionError

	monkeypatch.setattr(api, "frappe", fake_frappe)
	return fake_frappe


def test_refill_runs_all_three_steps(mock_session_environment, monkeypatch):
	"""Happy path: every toggle is on, every helper runs once and the
	per-step status dict surfaces the counts."""
	from optimus import ai_fix
	from optimus.settings import get_config  # noqa: F401 we patch this below

	monkeypatch.setattr(ai_fix, "is_available", lambda: True)
	monkeypatch.setattr("optimus.settings.get_config", lambda: _cfg())

	# Mock the three core helpers + the final regen.
	backfill = MagicMock(return_value={"added": 3, "failed": 0, "skipped_time": 1, "total_pending": 4})
	humanize = MagicMock(return_value={"updated": True, "reason": None})
	indexes = MagicMock(return_value={"added": 2, "failed": 0, "skipped": 0})
	regen = MagicMock(return_value={"regenerated": True})

	monkeypatch.setattr("optimus.analyze._run_ai_backfill", backfill)
	monkeypatch.setattr(api, "_humanize_steps_core", humanize)
	monkeypatch.setattr(api, "_refill_indexes_for_doc", indexes)
	monkeypatch.setattr(api, "regenerate_reports", regen)

	out = api.refill_ai_suggestions(session_uuid="sess-1")

	assert out["ok"] is True
	assert out["fixes"]["added"] == 3 and out["fixes"]["skipped_time"] == 1
	assert out["steps"]["updated"] is True
	assert out["indexes"]["added"] == 2
	assert out["regenerated"] is True

	# Each step was called exactly once.
	assert backfill.call_count == 1
	assert humanize.call_count == 1
	assert indexes.call_count == 1
	assert regen.call_count == 1


def test_refill_skips_sections_whose_toggle_is_off(mock_session_environment, monkeypatch):
	"""Per-section toggles gate each step: a toggle-off section reports
	``skipped`` instead of erroring and the helper isn't called."""
	from optimus import ai_fix

	monkeypatch.setattr(ai_fix, "is_available", lambda: True)
	# Only fixes toggle is on; humanize + indexes toggles are off.
	monkeypatch.setattr(
		"optimus.settings.get_config",
		lambda: _cfg(ai_humanize_steps=False, ai_suggest_indexes=False),
	)

	backfill = MagicMock(return_value={"added": 1, "failed": 0, "skipped_time": 0, "total_pending": 1})
	humanize = MagicMock()
	indexes = MagicMock()
	regen = MagicMock(return_value={"regenerated": True})

	monkeypatch.setattr("optimus.analyze._run_ai_backfill", backfill)
	monkeypatch.setattr(api, "_humanize_steps_core", humanize)
	monkeypatch.setattr(api, "_refill_indexes_for_doc", indexes)
	monkeypatch.setattr(api, "regenerate_reports", regen)

	out = api.refill_ai_suggestions(session_uuid="sess-1")

	assert backfill.call_count == 1
	assert humanize.call_count == 0
	assert indexes.call_count == 0
	assert out["steps"]["reason"] == "toggle_off"
	assert out["indexes"]["skipped_reason"] == "toggle_off"
	# Final re-render still runs (the fixes step did write something).
	assert regen.call_count == 1


def test_refill_fails_fast_when_provider_missing(mock_session_environment, monkeypatch):
	"""``ai_fix.is_available() == False`` → endpoint raises before any
	helper runs."""
	from optimus import ai_fix

	monkeypatch.setattr(ai_fix, "is_available", lambda: False)
	monkeypatch.setattr("optimus.settings.get_config", lambda: _cfg())

	backfill = MagicMock()
	humanize = MagicMock()
	indexes = MagicMock()
	regen = MagicMock()
	monkeypatch.setattr("optimus.analyze._run_ai_backfill", backfill)
	monkeypatch.setattr(api, "_humanize_steps_core", humanize)
	monkeypatch.setattr(api, "_refill_indexes_for_doc", indexes)
	monkeypatch.setattr(api, "regenerate_reports", regen)

	with pytest.raises(Exception) as excinfo:
		api.refill_ai_suggestions(session_uuid="sess-1")
	assert "aren't configured" in str(excinfo.value)

	assert backfill.call_count == 0
	assert humanize.call_count == 0
	assert indexes.call_count == 0
	assert regen.call_count == 0


def test_refill_requires_ready_status(mock_session_environment, monkeypatch):
	"""Non-Ready session → raises before any work."""
	from optimus import ai_fix

	mock_session_environment.db.get_value.return_value = _row(status="Analyzing")
	monkeypatch.setattr(ai_fix, "is_available", lambda: True)
	monkeypatch.setattr("optimus.settings.get_config", lambda: _cfg())

	with pytest.raises(Exception) as excinfo:
		api.refill_ai_suggestions(session_uuid="sess-1")
	assert "Ready sessions" in str(excinfo.value)


def test_refill_permission_gate(mock_session_environment, monkeypatch):
	"""Non-owning + non-System-Manager + non-Administrator user → raises."""
	from optimus import ai_fix

	# Doc is owned by a different user; current user has no elevated roles.
	mock_session_environment.db.get_value.return_value = _row(user="someone-else@example.com")
	mock_session_environment.get_roles.return_value = []
	monkeypatch.setattr(api, "_require_profiler_user", lambda: "user@example.com")
	monkeypatch.setattr(ai_fix, "is_available", lambda: True)
	monkeypatch.setattr("optimus.settings.get_config", lambda: _cfg())

	with pytest.raises(PermissionError):
		api.refill_ai_suggestions(session_uuid="sess-1")


# ------------------------------------------------ shared budget + circuit break


def _wire_refill(monkeypatch, *, backfill=None, humanize=None, indexes=None, cfg=None):
	"""Common wiring for the budget / abort tests: AI available, helpers mocked."""
	from optimus import ai_fix

	monkeypatch.setattr(ai_fix, "is_available", lambda: True)
	monkeypatch.setattr("optimus.settings.get_config", lambda: cfg or _cfg())
	backfill = backfill or MagicMock(
		return_value={"added": 1, "failed": 0, "skipped_time": 0, "total_pending": 1}
	)
	humanize = humanize or MagicMock(return_value={"updated": True, "reason": None})
	indexes = indexes or MagicMock(return_value={"added": 1, "failed": 0, "skipped": 0})
	regen = MagicMock(return_value={"regenerated": True})
	monkeypatch.setattr("optimus.analyze._run_ai_backfill", backfill)
	monkeypatch.setattr(api, "_humanize_steps_core", humanize)
	monkeypatch.setattr(api, "_refill_indexes_for_doc", indexes)
	monkeypatch.setattr(api, "regenerate_reports", regen)
	return backfill, humanize, indexes, regen


def test_refill_aborted_fixes_skip_later_phases_and_surface_first_error(
	mock_session_environment, monkeypatch
):
	backfill = MagicMock(return_value={
		"added": 0, "failed": 1, "skipped_time": 0, "total_pending": 3,
		"first_error": "Codex is not signed in.", "aborted": True,
	})
	_, humanize, indexes, regen = _wire_refill(monkeypatch, backfill=backfill)

	out = api.refill_ai_suggestions(session_uuid="sess-1")

	assert humanize.call_count == 0 and indexes.call_count == 0
	assert out["steps"]["reason"] == "aborted"
	assert out["indexes"]["skipped_reason"] == "aborted"
	assert out["first_error"] == "Codex is not signed in."
	assert out["fixes"]["first_error"] == "Codex is not signed in."
	assert out["aborted"] is True
	# The final re-render still runs and never triggers a second backfill.
	regen.assert_called_once_with("sess-1", ai_backfill=False)


def test_refill_passes_remaining_budget_to_findings_and_indexes(
	mock_session_environment, monkeypatch
):
	backfill, _, indexes, _ = _wire_refill(monkeypatch)

	api.refill_ai_suggestions(session_uuid="sess-1")

	from optimus import analyze

	assert backfill.call_args.kwargs["time_budget"] <= analyze.AI_BACKFILL_TIME_BUDGET_SECONDS
	assert 0 < indexes.call_args.kwargs["time_budget"] <= api.REFILL_TOTAL_BUDGET_SECONDS


def test_refill_skips_steps_and_indexes_when_deadline_passed(
	mock_session_environment, monkeypatch
):
	monkeypatch.setattr(api, "REFILL_TOTAL_BUDGET_SECONDS", 0)
	backfill, humanize, indexes, _ = _wire_refill(monkeypatch)

	out = api.refill_ai_suggestions(session_uuid="sess-1")

	assert backfill.call_args.kwargs["time_budget"] == 0
	assert humanize.call_count == 0 and indexes.call_count == 0
	assert out["steps"]["reason"] == "deadline"
	assert out["indexes"]["skipped_reason"] == "deadline"


def test_refill_collects_first_error_from_steps_and_indexes(
	mock_session_environment, monkeypatch
):
	humanize = MagicMock(return_value={"updated": False, "reason": "flaky", "error": "flaky", "retryable": True})
	indexes = MagicMock(return_value={"added": 0, "failed": 1, "skipped": 0, "first_error": "idx err"})
	_wire_refill(monkeypatch, humanize=humanize, indexes=indexes)

	out = api.refill_ai_suggestions(session_uuid="sess-1")

	assert out["first_error"] == "flaky"
	assert out["indexes"]["first_error"] == "idx err"
	assert out["aborted"] is False


def test_refill_non_retryable_steps_error_skips_indexes(mock_session_environment, monkeypatch):
	humanize = MagicMock(return_value={
		"updated": False, "reason": "signed out", "error": "signed out", "retryable": False,
	})
	_, _, indexes, _ = _wire_refill(monkeypatch, humanize=humanize)

	out = api.refill_ai_suggestions(session_uuid="sess-1")

	assert indexes.call_count == 0
	assert out["indexes"]["skipped_reason"] == "aborted"
	assert out["first_error"] == "signed out"


# ----------------------------------------------------------- per-session lock


def test_second_concurrent_refill_is_refused(mock_session_environment, monkeypatch):
	backfill, humanize, indexes, regen = _wire_refill(monkeypatch)
	# SETNX returns None when the key already exists (a run is in progress).
	mock_session_environment.cache.set.return_value = None

	with pytest.raises(Exception, match="already running"):
		api.refill_ai_suggestions(session_uuid="sess-1")

	assert backfill.call_count == 0 and regen.call_count == 0
	# The refused call did not own the lock, so it must not delete it.
	mock_session_environment.cache.delete_value.assert_not_called()


def test_backfill_ai_fixes_also_refuses_concurrent_run(mock_session_environment, monkeypatch):
	from optimus import ai_fix

	monkeypatch.setattr(ai_fix, "is_available", lambda: True)
	monkeypatch.setattr("optimus.settings.get_config", lambda: _cfg())
	mock_session_environment.utils.cint = int
	backfill = MagicMock()
	monkeypatch.setattr("optimus.analyze._run_ai_backfill", backfill)
	mock_session_environment.cache.set.return_value = None

	with pytest.raises(Exception, match="already running"):
		api.backfill_ai_fixes(session_uuid="sess-1")

	assert backfill.call_count == 0


def test_lock_is_taken_with_ttl_and_released_after_the_run(mock_session_environment, monkeypatch):
	_wire_refill(monkeypatch)
	cache = mock_session_environment.cache
	cache.set.return_value = True

	api.refill_ai_suggestions(session_uuid="sess-1")

	kwargs = cache.set.call_args.kwargs
	assert kwargs["nx"] is True and kwargs["ex"] == api.AI_REFRESH_LOCK_TTL_SECONDS
	cache.delete_value.assert_called_once_with("optimus:ai_refill_lock:sess-1")


def test_lock_is_released_when_the_run_raises(mock_session_environment, monkeypatch):
	backfill = MagicMock(side_effect=RuntimeError("boom"))
	_wire_refill(monkeypatch, backfill=backfill)
	cache = mock_session_environment.cache
	cache.set.return_value = True

	with pytest.raises(RuntimeError):
		api.refill_ai_suggestions(session_uuid="sess-1")

	cache.delete_value.assert_called_once()


def test_lock_fails_open_when_cache_is_unreachable(mock_session_environment, monkeypatch):
	_, _, _, regen = _wire_refill(monkeypatch)
	mock_session_environment.cache.set.side_effect = ConnectionError("redis down")

	out = api.refill_ai_suggestions(session_uuid="sess-1")

	assert out["ok"] is True and regen.call_count == 1
	mock_session_environment.cache.delete_value.assert_not_called()


# ------------------------------------------------- regenerate_reports ai_backfill


@pytest.fixture
def regen_env(mock_session_environment, monkeypatch):
	mock_session_environment.utils.cint = int
	mock_session_environment.db.get_value.return_value = {
		"name": "opt-xxx", "user": "user@example.com", "status": "Ready",
	}
	monkeypatch.setattr(api, "_require_session_permission", lambda *a, **k: None)
	monkeypatch.setattr("optimus.analyze._fetch_recordings", lambda *a, **k: [])
	monkeypatch.setattr("optimus.analyze._load_recordings_bundle", lambda *a, **k: None)
	monkeypatch.setattr("optimus.analyze._render_and_attach_reports", MagicMock())
	backfill = MagicMock(return_value=False)
	monkeypatch.setattr("optimus.analyze._backfill_ai_suggestions", backfill)
	return backfill


def test_regenerate_reports_default_still_backfills(regen_env):
	out = api.regenerate_reports("sess-1")
	assert out["regenerated"] is True
	assert regen_env.call_count == 1


def test_regenerate_reports_ai_backfill_false_skips_backfill(regen_env):
	out = api.regenerate_reports("sess-1", ai_backfill=False)
	assert out["regenerated"] is True
	assert regen_env.call_count == 0
