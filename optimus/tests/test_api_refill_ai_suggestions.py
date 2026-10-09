# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for ``optimus.api.refill_ai_suggestions``.

The endpoint chains two helpers (``_run_ai_backfill``, ``_humanize_steps_core``) and re-renders once at the end through ``_render_session_report``
(never the whitelisted ``regenerate_reports``). Covers the happy path for a plain Optimus User
owner, toggle-off sections and gate failures (provider missing, non-Ready, non-owner): each
raises before any helper runs.
"""

from types import SimpleNamespace
from unittest.mock import DEFAULT, MagicMock

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
		order = []
		helpers = SimpleNamespace(
			backfill=MagicMock(
				side_effect=lambda *a, **k: order.append("backfill") or DEFAULT,
				return_value={"added": 3, "failed": 0, "skipped_time": 1, "total_pending": 4},
			),
			humanize=MagicMock(return_value={"updated": True, "reason": None}),
			render=MagicMock(return_value={"regenerated": True, "recordings_available": 0, "actions_total": 0}),
			order=order,
		)
		monkeypatch.setattr("optimus.analyze._bump_ai_refresh_count", lambda docname: order.append(("bump", docname)))
		monkeypatch.setattr(api, "safe_commit", lambda: order.append("commit"))
		monkeypatch.setattr("optimus.analyze._run_ai_backfill", helpers.backfill)
		monkeypatch.setattr(api, "_humanize_steps_core", helpers.humanize)
		monkeypatch.setattr(api, "_render_session_report", helpers.render)
		monkeypatch.setattr(api, "regenerate_reports", _must_not_be_called)
		return fake, helpers

	return _make


def test_refill_runs_both_ai_steps_for_a_plain_owner(env):
	fake, h = env()
	out = api.refill_ai_suggestions(session_uuid=SESSION_UUID)
	assert out["ok"] is True and out["session_uuid"] == SESSION_UUID
	assert out["fixes"]["added"] == 3 and out["fixes"]["skipped_time"] == 1
	assert out["steps"]["updated"] is True
	assert "indexes" not in out
	assert out["regenerated"] is True
	assert h.backfill.call_args.kwargs == {"cap": 0, "regenerate_all": True}
	assert set(h.humanize.call_args.kwargs) == {"title", "memo"} and h.humanize.call_args.kwargs["title"] == "Checkout flow"
	h.render.assert_called_once_with(DOCNAME, memo=h.humanize.call_args.kwargs["memo"])
	# one atomic bump, committed on its own before any provider call (no read-modify-write)
	assert h.order[:3] == [("bump", DOCNAME), "commit", "backfill"]
	assert not [args for args, _kw in fake.spies.set_value if "ai_refresh_count" in args]


def test_refill_skips_sections_whose_toggle_is_off(env):
	_, h = env(cfg=_cfg(ai_humanize_steps=False, ai_suggest_indexes=False))
	out = api.refill_ai_suggestions(session_uuid=SESSION_UUID)
	assert h.backfill.call_count == 1
	assert h.humanize.call_count == 0
	assert out["steps"]["reason"] == "toggle_off"
	assert h.render.call_count == 1


def test_refill_fails_fast_when_provider_missing(env):
	fake, h = env(available=False)
	with pytest.raises(FakeValidationError) as exc:
		api.refill_ai_suggestions(session_uuid=SESSION_UUID)
	assert "aren't configured" in str(exc.value)
	assert h.backfill.call_count == h.humanize.call_count == h.render.call_count == 0
	assert fake.spies.set_value == [] and h.order == []


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


def test_refill_reports_gated_and_excluded_counts(env):
	_, h = env()
	h.backfill.return_value = {
		"added": 1, "failed": 0, "skipped_time": 0, "total_pending": 1,
		"gated": 2, "excluded": 1, "skipped_ineligible": 3,
	}
	out = api.refill_ai_suggestions(session_uuid=SESSION_UUID)
	assert (out["fixes"]["gated"], out["fixes"]["excluded"], out["fixes"]["skipped_ineligible"]) == (2, 1, 3)
	assert "indexes" not in out


def test_refill_shares_one_recordings_memo_between_the_steps_and_the_rerender(env):
	_, h = env()
	api.refill_ai_suggestions(session_uuid=SESSION_UUID)
	memo = h.humanize.call_args.kwargs["memo"]
	assert isinstance(memo, dict) and h.render.call_args.kwargs["memo"] is memo


def test_each_refill_starts_a_fresh_memo(env):
	_, h = env()
	api.refill_ai_suggestions(session_uuid=SESSION_UUID)
	first = h.humanize.call_args.kwargs["memo"]
	api.refill_ai_suggestions(session_uuid=SESSION_UUID)
	assert h.humanize.call_args.kwargs["memo"] is not first


# --- one refresh per session at a time -----------------------------------------------------

_FLAG = "site|optimus:ai_refresh:" + SESSION_UUID


def test_an_overlapping_refresh_of_the_same_session_is_refused_without_billing(env):
	"""Two Refreshes of one session ran side by side once the bump stopped holding the session
	row: both billed every finding and overwrote each other's answers. A second click while the
	first runs is answered at once, before the refresh is counted or the provider is called."""
	fake, h = env()
	inner = {}

	def backfill(*a, **k):
		h.order.append("backfill")
		inner["out"] = api.refill_ai_suggestions(session_uuid=SESSION_UUID)
		return {"added": 1, "failed": 0, "skipped_time": 0}

	h.backfill.side_effect = backfill
	out = api.refill_ai_suggestions(session_uuid=SESSION_UUID)
	assert out["ok"] is True
	second = inner["out"]
	assert second["ok"] is False and second["busy"] is True and second["session_uuid"] == SESSION_UUID
	assert "already running for this session" in second["message"]
	# the second click counted nothing and called nothing: one bump, one backfill, one Steps call
	assert [step for step in h.order if step != "commit"] == [("bump", DOCNAME), "backfill"]
	assert h.humanize.call_count == 1 and h.render.call_count == 1
	assert _FLAG not in fake.cache.store  # released by the refresh that held it


def test_the_flag_is_taken_atomically_with_a_ttl_and_released_after_the_refresh(env):
	fake, h = env()
	api.refill_ai_suggestions(session_uuid=SESSION_UUID)
	[take] = [c for c in fake.cache.calls if c[0] == "set"]
	_, key, token, nx, ttl = take
	assert key == _FLAG and nx is True and isinstance(token, str) and len(token) >= 16
	# the longest a refresh can run: its 60 s fix budget, a fix call and a Steps call at the
	# Request timeout, and a minute for the write and the re-render
	assert ttl == 60 + 2 * ai_fix._resolve_timeout_seconds() + 60
	assert ("delete", _FLAG) in fake.cache.calls and _FLAG not in fake.cache.store


def test_the_flag_is_released_when_the_refresh_fails(env):
	fake, h = env()
	h.backfill.side_effect = RuntimeError("boom")
	with pytest.raises(RuntimeError):
		api.refill_ai_suggestions(session_uuid=SESSION_UUID)
	assert _FLAG not in fake.cache.store
	h.backfill.side_effect = None
	assert api.refill_ai_suggestions(session_uuid=SESSION_UUID)["ok"] is True  # the next click runs


def test_a_refresh_never_releases_another_refreshs_flag(env):
	"""The flag expired while this refresh ran and another refresh took it: the release reads the
	holder back from Redis and leaves another token alone."""
	fake, h = env()

	def backfill(*a, **k):
		fake.cache.store[_FLAG] = b"another-refresh"
		return {"added": 0, "failed": 0, "skipped_time": 0}

	h.backfill.side_effect = backfill
	api.refill_ai_suggestions(session_uuid=SESSION_UUID)
	assert fake.cache.store[_FLAG] == b"another-refresh"


def test_a_cache_failure_never_blocks_the_refresh(env, monkeypatch):
	fake, h = env()

	def broken(*a, **k):
		raise ConnectionError("redis down")

	monkeypatch.setattr(fake.cache, "set", broken)
	out = api.refill_ai_suggestions(session_uuid=SESSION_UUID)
	assert out["ok"] is True and h.backfill.call_count == 1
	assert not [c for c in fake.cache.calls if c[0] in ("get", "delete")]  # nothing taken, nothing to release


def test_a_refused_refresh_counts_nothing(env):
	fake, h = env()
	fake.cache.store[_FLAG] = b"the-first-refresh"
	out = api.refill_ai_suggestions(session_uuid=SESSION_UUID)
	assert out["busy"] is True
	assert h.order == [] and h.backfill.call_count == h.humanize.call_count == h.render.call_count == 0
	assert fake.cache.store[_FLAG] == b"the-first-refresh"


def test_the_flag_key_is_registered_and_documented():
	from optimus import redis_keys

	assert redis_keys.ai_refresh_inflight(SESSION_UUID) == "optimus:ai_refresh:" + SESSION_UUID
	assert "optimus:ai_refresh:<session_uuid>" in redis_keys.KEY_PATTERNS


def test_the_form_shows_a_busy_refresh_as_a_notice_and_stops():
	from pathlib import Path

	js = (Path(api.__file__).parent / "optimus" / "doctype" / "optimus_session" / "optimus_session.js").read_text()
	body = js[js.index("function _refill_ai_call("):]
	busy = body.index("if (m.busy) {")
	assert body.index("return;", busy) < body.index("const fx = m.fixes", busy)
	assert '__("A refresh is already running for this session.' in body[busy:busy + 400]


def test_refill_with_fix_suggestions_off_skips_the_backfill(env):
	fake, h = env(cfg=_cfg(ai_suggest_findings=False))
	out = api.refill_ai_suggestions(session_uuid=SESSION_UUID)
	assert h.backfill.call_count == 0 and out["fixes"]["skipped"] == "toggle_off"
	assert h.humanize.call_count == 1 and _FLAG not in fake.cache.store
