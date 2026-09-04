# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for ``optimus.api.refill_ai_suggestions``: the single-button
entry point that replaces the five legacy AI buttons.

The endpoint chains three core helpers (``_run_ai_backfill``,
``_humanize_steps_core``, ``_refill_indexes_for_doc``) and re-renders
the report once at the end. The tests verify:

- happy path: each step is called once, the per-step status dict is
  surfaced, the final re-render runs
- toggle-off path: a section whose feature toggle is off is silently
  skipped (no helper call, ``skipped`` key set)
- gate failures: missing provider / non-Ready / non-owning user all
  raise before any work runs
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
	"""Minimal Optimus Session doc only `name` is touched by the
	endpoint's orchestration; the helpers are mocked, so the doc body
	doesn't matter."""
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
	"""Happy path: every toggle is on, every helper runs once, and the
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
	``skipped`` instead of erroring, and the helper isn't called."""
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
	assert "AI isn't configured" in str(excinfo.value)

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
