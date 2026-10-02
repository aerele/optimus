# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for ``optimus.api.refill_ai_suggestions``.

The endpoint chains three helpers (``_run_ai_backfill``, ``_humanize_steps_core``,
``_refill_indexes_for_doc``) and re-renders once at the end through ``_render_session_report``
(never the whitelisted ``regenerate_reports``). Covers the happy path for a plain Optimus User
owner, toggle-off sections and gate failures (provider missing, non-Ready, non-owner): each
raises before any helper runs.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from optimus import ai_fix, api
from optimus.tests.gate_fakes import (
	DOCNAME,
	OWNER,
	SESSION_UUID,
	FakePermissionError,
	FakeValidationError,
	fake_session_doc,
	install,
	make_fake_frappe,
	owner_perms,
	session_row,
)


def _cfg(**overrides):
	defaults = {"ai_suggest_findings": True, "ai_humanize_steps": True, "ai_suggest_indexes": True}
	defaults.update(overrides)
	return SimpleNamespace(**defaults)


def _must_not_be_called(*args, **kwargs):
	raise AssertionError("refill must re-render via _render_session_report, not regenerate_reports")


@pytest.fixture
def env(monkeypatch):
	def _make(*, status="Ready", user=OWNER, perms=None, available=True, cfg=None):
		fake = make_fake_frappe(
			user=user, sessions={SESSION_UUID: session_row(status=status)},
			perms=owner_perms() if perms is None else perms, docs={DOCNAME: fake_session_doc()},
		)
		install(monkeypatch, fake)
		monkeypatch.setattr(ai_fix, "is_available", lambda section=None: available)
		monkeypatch.setattr("optimus.settings.get_config", lambda: cfg or _cfg())
		helpers = SimpleNamespace(
			backfill=MagicMock(return_value={"added": 3, "failed": 0, "skipped_time": 1, "total_pending": 4}),
			humanize=MagicMock(return_value={"updated": True, "reason": None}),
			indexes=MagicMock(return_value={"added": 2, "failed": 0, "skipped": 0}),
			render=MagicMock(return_value={"regenerated": True, "recordings_available": 0, "actions_total": 0}),
		)
		monkeypatch.setattr("optimus.analyze._run_ai_backfill", helpers.backfill)
		monkeypatch.setattr(api, "_humanize_steps_core", helpers.humanize)
		monkeypatch.setattr(api, "_refill_indexes_for_doc", helpers.indexes)
		monkeypatch.setattr(api, "_render_session_report", helpers.render)
		monkeypatch.setattr(api, "regenerate_reports", _must_not_be_called)
		return fake, helpers

	return _make


def test_refill_runs_all_three_steps_for_a_plain_owner(env):
	fake, h = env()
	out = api.refill_ai_suggestions(session_uuid=SESSION_UUID)
	assert out["ok"] is True and out["session_uuid"] == SESSION_UUID
	assert out["fixes"]["added"] == 3 and out["fixes"]["skipped_time"] == 1
	assert out["steps"]["updated"] is True and out["indexes"]["added"] == 2
	assert out["regenerated"] is True
	assert h.backfill.call_args.kwargs == {"cap": 0, "regenerate_all": True}
	assert h.humanize.call_args.kwargs == {"title": "Checkout flow"}
	assert h.indexes.call_count == 1
	h.render.assert_called_once_with(DOCNAME)
	assert fake.spies.set_value[0][0][:3] == ("Optimus Session", DOCNAME, "ai_refresh_count")


def test_refill_skips_sections_whose_toggle_is_off(env):
	_, h = env(cfg=_cfg(ai_humanize_steps=False, ai_suggest_indexes=False))
	out = api.refill_ai_suggestions(session_uuid=SESSION_UUID)
	assert h.backfill.call_count == 1
	assert h.humanize.call_count == 0 and h.indexes.call_count == 0
	assert out["steps"]["reason"] == "toggle_off"
	assert out["indexes"]["skipped_reason"] == "toggle_off"
	assert h.render.call_count == 1


def test_refill_fails_fast_when_provider_missing(env):
	fake, h = env(available=False)
	with pytest.raises(FakeValidationError) as exc:
		api.refill_ai_suggestions(session_uuid=SESSION_UUID)
	assert "aren't configured" in str(exc.value)
	assert h.backfill.call_count == h.humanize.call_count == h.indexes.call_count == h.render.call_count == 0
	assert fake.spies.set_value == []


def test_refill_requires_ready_status(env):
	_, h = env(status="Analyzing")
	with pytest.raises(FakeValidationError) as exc:
		api.refill_ai_suggestions(session_uuid=SESSION_UUID)
	assert "Ready" in str(exc.value)
	assert h.backfill.call_count == 0


def test_refill_denies_a_non_owner_without_write(env):
	_, h = env(user="someone-else@example.com", perms={})
	with pytest.raises(FakePermissionError):
		api.refill_ai_suggestions(session_uuid=SESSION_UUID)
	assert h.backfill.call_count == 0


def test_refill_allows_a_write_sharee(env):
	sharee = "sharee@example.com"
	_, h = env(user=sharee, perms={("read", DOCNAME, sharee): True, ("write", DOCNAME, sharee): True})
	assert api.refill_ai_suggestions(session_uuid=SESSION_UUID)["ok"] is True
