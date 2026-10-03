"""The legacy refresh route now admits background work, preserving its session gates."""
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from optimus import api
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


@pytest.fixture
def env(monkeypatch):
	def make(*, status="Ready", user=OWNER, perms=None, cfg=None):
		fake = make_fake_frappe(user=user, sessions={SESSION_UUID: session_row(status=status)},
			perms=owner_perms() if perms is None else perms, docs={DOCNAME: fake_session_doc()})
		install(monkeypatch, fake)
		jobs = import_module("optimus.ai_jobs")
		monkeypatch.setattr(jobs, "frappe", fake)
		h = SimpleNamespace(start=MagicMock(return_value={"status": "queued", "refresh": {"run_id": "fake-run", "state": "queued"}}),
			active=MagicMock(return_value=None))
		monkeypatch.setattr(jobs, "active_refresh", h.active, raising=False)
		monkeypatch.setattr(jobs, "start_refresh", h.start)
		monkeypatch.setattr("optimus.settings.get_config", lambda: cfg or SimpleNamespace(
			ai_suggest_findings=True, ai_humanize_steps=True, ai_refresh_max_findings=20))
		return fake, h
	return make


def test_refill_queues_for_plain_owner_without_inline_spend_or_counter_write(env):
	fake, h = env()
	out = api.refill_ai_suggestions(SESSION_UUID)
	assert out["status"] == "queued" and out["ok"] is True
	assert out["session_uuid"] == SESSION_UUID
	assert h.start.call_args.kwargs == dict(docname=DOCNAME, session_uuid=SESSION_UUID,
		requested_by=OWNER, regenerate_all=False, include_fixes=True, include_steps=True,
		cap=20, resume_from=None, retry_uncertain=False)
	assert not fake.spies.set_value and not fake.spies.get_doc


def test_refill_honors_section_flags_and_explicit_unlimited_cap(env):
	_, h = env(cfg=SimpleNamespace(ai_suggest_findings=False, ai_humanize_steps=True, ai_refresh_max_findings=0))
	api.refill_ai_suggestions(SESSION_UUID, regenerate_all="1")
	assert h.start.call_args.kwargs["include_fixes"] is False
	assert h.start.call_args.kwargs["regenerate_all"] is True
	assert h.start.call_args.kwargs["cap"] == 0


def test_refill_requires_ready_status_before_admission(env):
	_, h = env(status="Analyzing")
	with pytest.raises(FakeValidationError):
		api.refill_ai_suggestions(SESSION_UUID)
	h.start.assert_not_called()


def test_refill_denies_nonowner_without_write(env):
	fake, h = env(user="other@example.com", perms={})
	with pytest.raises(FakePermissionError):
		api.refill_ai_suggestions(SESSION_UUID)
	h.start.assert_not_called()
	assert not fake.cache.calls


def test_refill_allows_write_sharee_and_attributes_actual_requester(env):
	user = "sharee@example.com"
	_, h = env(user=user, perms={("read", DOCNAME, user): True, ("write", DOCNAME, user): True})
	assert api.refill_ai_suggestions(SESSION_UUID)["ok"]
	assert h.start.call_args.kwargs["requested_by"] == user


@pytest.mark.parametrize("value", ["yes please", "2", [], {}, None, 2, 0.5])
def test_malformed_flags_do_not_admit_or_consume_rate_limit(env, value):
	fake, h = env()
	with pytest.raises(FakeValidationError):
		api.refill_ai_suggestions(SESSION_UUID, regenerate_all=value)
	h.start.assert_not_called()
	assert not fake.cache.calls


@pytest.mark.parametrize("value,expected", [(False, False), (True, True), (0, False), (1, True),
	("0", False), ("1", True), ("false", False), ("true", True), ("FALSE", False)])
def test_refresh_flags_keep_false_strings_false(env, value, expected):
	_, h = env()
	api.refill_ai_suggestions(SESSION_UUID, regenerate_all=value, retry_uncertain=value)
	assert h.start.call_args.kwargs["regenerate_all"] is expected
	assert h.start.call_args.kwargs["retry_uncertain"] is expected


def test_existing_run_attaches_without_redis_or_configuration(env, monkeypatch):
	fake, h = env()
	h.active.return_value = {"run_id": "fake-run", "state": "running"}
	monkeypatch.setattr("optimus.settings.get_config", lambda: pytest.fail("must attach without Settings"))
	out = api.refill_ai_suggestions(SESSION_UUID)
	assert out["status"] == "already_running"
	assert not fake.cache.calls
	h.start.assert_not_called()


@pytest.mark.parametrize("reason", ["no_worker", "queue_unavailable", "site_cap", "disabled"])
def test_admission_refusal_is_explicit_without_inline_fallback(env, reason):
	fake, h = env()
	h.start.return_value = {"status": "refused", "reason": reason}
	out = api.refill_ai_suggestions(SESSION_UUID)
	assert out["ok"] is False and out["reason"] == reason and out["message"]
	assert not fake.spies.set_value


def test_cancel_cannot_target_another_sessions_run(env, monkeypatch):
	fake, _ = env()
	jobs = import_module("optimus.ai_jobs")
	cancel = MagicMock()
	monkeypatch.setattr(jobs, "cancel_refresh", cancel)
	# The fake DB finds no run bound to this parent, even if a run id is supplied.
	with pytest.raises(FakeValidationError):
		api.cancel_ai_refresh(SESSION_UUID, "foreign-run")
	cancel.assert_not_called()
	assert not fake.cache.calls


def test_status_checks_read_permission_before_touching_journal(env, monkeypatch):
	fake, _ = env(user="stranger@example.com", perms={})
	jobs = import_module("optimus.ai_jobs")
	monkeypatch.setattr(jobs, "refresh_state", lambda *a: pytest.fail("unauthorized journal read"))
	with pytest.raises(FakePermissionError):
		api.ai_refresh_status(SESSION_UUID)


@pytest.mark.parametrize("owner", [True, False])
def test_read_sharee_can_poll_but_only_actor_sees_refresh_plan(env, monkeypatch, owner):
	user = OWNER if owner else "reader@example.com"
	fake, _ = env(user=user, perms={("read", DOCNAME, user): True})
	permission = fake.has_permission
	monkeypatch.setattr(fake, "has_permission", lambda dt, ptype, doc, user=None: permission(dt, ptype, doc, user=user or fake.session.user))
	monkeypatch.setattr(fake, "get_doc", lambda *a: fake_session_doc(owner=OWNER, status="Ready"))
	jobs = import_module("optimus.ai_jobs")
	monkeypatch.setattr(jobs, "refresh_state", lambda *a: {"state": "complete", "seq": 9})
	plan = MagicMock(return_value={"pending": 3})
	monkeypatch.setattr(jobs, "refresh_plan", plan)
	out = api.ai_refresh_status(SESSION_UUID)
	assert out["status"] == "ok" and out["refresh"]["seq"] == 9
	assert out["can_act"] is owner
	assert bool(out["plan"]) is owner
	assert plan.call_count == int(owner)
	assert not fake.cache.calls


def test_cancel_passes_exact_requested_run_even_when_a_newer_run_is_active(env, monkeypatch):
	fake, _ = env(status="Failed")
	jobs = import_module("optimus.ai_jobs")
	read = fake.db.get_value
	def get_value(table, filters, field="name", **kw):
		if table == "Optimus AI Refresh Run":
			assert filters == {"name": "old-run", "session_name": DOCNAME}
			return "old-run"
		return read(table, filters, field, **kw)
	monkeypatch.setattr(fake, "db", SimpleNamespace(get_value=get_value))
	cancel = MagicMock(return_value={"state": "cancelled", "run_id": "old-run"})
	monkeypatch.setattr(jobs, "cancel_refresh", cancel)
	assert api.cancel_ai_refresh(SESSION_UUID, "old-run")["refresh"]["state"] == "cancelled"
	cancel.assert_called_once_with("old-run", requested_by=OWNER)
	assert not fake.cache.calls


def test_progress_failure_is_unknown_and_does_not_claim_completion(env, monkeypatch):
	fake, _ = env()
	monkeypatch.setattr(fake, "has_permission", lambda *a, **kw: True)
	jobs = import_module("optimus.ai_jobs")
	def fail(*a):
		raise ConnectionError("fake SQL outage")
	monkeypatch.setattr(jobs, "refresh_state", fail)
	logs = []
	monkeypatch.setattr("optimus.analyze._log_ai_step_failure", lambda *a, **kw: logs.append(type(a[1]).__name__))
	assert api.ai_refresh_status(SESSION_UUID) == {
		"status": "unknown", "refresh": None, "can_act": False, "plan": None,
	}
	assert logs == ["ConnectionError"]


def test_render_only_action_refuses_active_refresh_before_consuming_rate_limit(env):
	fake, helpers = env()
	helpers.active.return_value = {"run_id": "fake-run", "state": "running"}
	with pytest.raises(FakeValidationError, match="AI refresh"):
		api.regenerate_reports(SESSION_UUID)
	assert not fake.cache.calls
