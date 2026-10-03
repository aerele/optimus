"""Optional enrichment cannot roll back or fail an already saved profile."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from optimus import ai_fix, ai_jobs, analyze
from optimus.analyzers.base import AnalyzerResult

pytestmark = pytest.mark.rq


@pytest.fixture
def pipeline(monkeypatch):
	state = {"status": "Stopping", "findings": [], "report": None}
	durable = deepcopy(state)
	events, provider = [], []

	def commit():
		durable.clear()
		durable.update(deepcopy(state))
		events.append("commit")

	def rollback():
		state.clear()
		state.update(deepcopy(durable))

	def set_value(table, name, field, value):
		assert table == "Optimus Session"
		state[field] = value

	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(
		db=SimpleNamespace(get_value=lambda *a, **kw: "fake-doc", set_value=set_value, rollback=rollback),
		local=SimpleNamespace(), conf={"optimus_analyze_gc_collect": False},
		cache=SimpleNamespace(get_value=lambda *a: None), log_error=lambda **kw: events.append("log"),
	))
	monkeypatch.setattr(analyze, "safe_commit", commit)
	monkeypatch.setattr(analyze, "is_scheduler_disabled", lambda: True)
	monkeypatch.setattr(analyze, "_bg_wait_for_pending_jobs", lambda *a: 0)
	monkeypatch.setattr(analyze, "_acquire_singleflight", lambda *a: True)
	monkeypatch.setattr(analyze, "_fetch_recordings", lambda *a: [{"uuid": "fake-rec", "calls": []}])
	monkeypatch.setattr(analyze, "_enrich_recordings", lambda *a: [])
	monkeypatch.setattr(analyze.session, "get_recordings", lambda *a: ["fake-rec"])
	monkeypatch.setattr(analyze.session, "get_session_meta", lambda *a: {})
	monkeypatch.setattr(analyze, "_get_analyzers", lambda: [lambda *a: AnalyzerResult(
		findings=[{"title": "fake finding", "severity": "High"}],
	)])
	for name in (
		"_touch_singleflight", "_release_singleflight", "_publish_progress", "_publish_session_event",
		"_enrich_findings_with_source_snippets", "_persist_recordings_file", "_cleanup_redis", "_auto_arm_phase2",
	):
		monkeypatch.setattr(analyze, name, lambda *a, **kw: None)
	monkeypatch.setattr(ai_fix, "suggest_fix", lambda *a, **kw: provider.append(True))

	def persist(name, context, *a):
		state["findings"] = deepcopy(context.findings)
		commit()

	def render(*a):
		state["report"] = "fake-private-report"
		commit()

	monkeypatch.setattr(analyze, "_persist", persist)
	monkeypatch.setattr(analyze, "_render_and_attach_reports", render)
	return SimpleNamespace(state=state, durable=durable, events=events, provider=provider)


def test_profile_is_ready_and_durable_before_any_optional_ai_admission(pipeline, monkeypatch):
	queued = []
	def queue(name):
		assert pipeline.durable["status"] == "Ready"
		assert pipeline.durable["findings"] and pipeline.durable["report"]
		queued.append(name)
	monkeypatch.setattr(analyze, "_queue_analyze_time_ai", queue, raising=False)
	analyze.run("fake-session")
	assert queued == ["fake-doc"] and not pipeline.provider


def test_auto_arm_cleanup_failure_cannot_reclassify_a_saved_profile(pipeline, monkeypatch):
	def broken_cleanup(*args):
		raise RuntimeError("fake rollback failure after optional auto-arm")
	monkeypatch.setattr(analyze, "_auto_arm_phase2", broken_cleanup)
	with pytest.raises(RuntimeError, match="fake rollback failure"):
		analyze.run("fake-session")
	assert pipeline.durable["status"] == pipeline.state["status"] == "Ready"
	assert pipeline.durable["findings"] and pipeline.durable["report"]


@pytest.mark.rq
def test_timeout_in_optional_admission_cannot_mark_profile_failed(pipeline, monkeypatch):
	JobTimeoutException = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	def timeout(name):
		raise JobTimeoutException("fake optional admission timeout")
	monkeypatch.setattr(analyze, "_queue_analyze_time_ai", timeout, raising=False)
	with pytest.raises(JobTimeoutException):
		analyze.run("fake-session")
	assert pipeline.durable["status"] == "Ready" and pipeline.state["status"] == "Ready"
	assert pipeline.durable["findings"] and pipeline.durable["report"]
	assert not pipeline.provider


@pytest.mark.parametrize("fixes,steps", [(True, True), (False, True), (True, False), (False, False)])
def test_auto_admission_uses_the_session_owner_and_independent_section_toggles(monkeypatch, fixes, steps):
	cfg = SimpleNamespace(ai_enabled=True, ai_auto_suggest=fixes, ai_suggest_findings=True,
		ai_humanize_steps=steps, ai_auto_suggest_max=5)
	monkeypatch.setattr("optimus.settings.get_config", lambda: cfg)
	row = {"name": "fake-doc", "session_uuid": "fake-session", "owner": "fake-owner", "status": "Ready"}
	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(db=SimpleNamespace(get_value=lambda *a, **k: row)))
	calls = []
	monkeypatch.setattr(ai_jobs, "start_refresh", lambda **kw: calls.append(kw) or {"status": "queued"})
	analyze._queue_analyze_time_ai("fake-doc")
	assert len(calls) == int(fixes or steps)
	if calls:
		assert calls[0] == dict(docname="fake-doc", session_uuid="fake-session", requested_by="fake-owner",
			scope="fixes_missing", cap=5, include_fixes=fixes, include_steps=steps)


def test_auto_admission_failure_is_logged_without_undoing_the_profile(monkeypatch):
	def invalid_config():
		raise ValueError("fake invalid AI configuration")
	monkeypatch.setattr("optimus.settings.get_config", invalid_config)
	logs = []
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **kw: logs.append(type(a[1]).__name__))
	monkeypatch.setattr(analyze, "safe_commit", lambda: None)
	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(db=SimpleNamespace(rollback=lambda: None)))
	analyze._queue_analyze_time_ai("fake-doc")
	assert logs == ["ValueError"]


def test_persistence_writes_deterministic_steps_without_a_provider_call(monkeypatch):
	doc = MagicMock(notes="", title="fake title")
	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(get_doc=lambda *a: doc))
	monkeypatch.setattr(analyze, "safe_commit", lambda: None)
	monkeypatch.setattr(analyze.session, "get_jobs", lambda *a: [])
	monkeypatch.setattr(analyze, "_build_auto_notes_html", lambda *a: "deterministic steps")
	def forbidden(*a, **kw):
		raise AssertionError("core persistence must not contact the provider")
	monkeypatch.setattr(ai_fix, "humanize_steps", forbidden)
	analyze._persist("fake-doc", analyze.AnalyzeContext("fake-session", "fake-doc"), [])
	assert doc.notes == "deterministic steps"
	doc.save.assert_called_once_with(ignore_permissions=True)


@pytest.mark.parametrize("reason", ["no_worker", "queue_unavailable", "phase2", "disabled"])
def test_auto_admission_refusal_leaves_an_operator_notice(monkeypatch, reason):
	cfg = SimpleNamespace(ai_enabled=True, ai_auto_suggest=True, ai_suggest_findings=True,
		ai_humanize_steps=False, ai_auto_suggest_max=5)
	monkeypatch.setattr("optimus.settings.get_config", lambda: cfg)
	row = {"name": "fake-doc", "session_uuid": "fake-session", "owner": "fake-owner", "status": "Ready"}
	notices = []
	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(db=SimpleNamespace(get_value=lambda *a, **k: row)))
	monkeypatch.setattr(ai_jobs, "start_refresh", lambda **kw: {"status": "refused", "reason": reason})
	monkeypatch.setattr(ai_jobs, "record_admission_notice", lambda *a, **kw: notices.append((a, kw)), raising=False)
	analyze._queue_analyze_time_ai("fake-doc")
	assert notices == [(("fake-doc",), {"reason": reason})]
