# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Phase 2 endpoints behind the session gate (PR-1): a run_uuid is resolved to its parent
session and gated there; the run instruments the caller's own requests."""

import datetime
import sys
import types
from types import SimpleNamespace

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
	install_module,
	make_fake_frappe,
	owner_perms,
	session_row,
)

PICKS = '[{"dotted_path": "app.mod.fn", "source": "freeform"}]'
MANAGER = "manager@example.com"


def _env(monkeypatch, *, user=OWNER, roles=("Optimus User",), perms=None, run_status="Recording", session_status="Ready"):
	doc = fake_session_doc(phase_2_runs=[SimpleNamespace(run_uuid="run-1", status=run_status, ended_at=None)])
	fake = make_fake_frappe(
		user=user, roles=roles, sessions={SESSION_UUID: session_row(status=session_status)},
		runs={"run-1": {"name": "RUN-1", "parent": DOCNAME, "status": run_status}},
		perms=owner_perms() if perms is None else perms, docs={DOCNAME: doc},
	)
	install(monkeypatch, fake)
	seen = SimpleNamespace(saved=[], capture=[], analyzed=[])
	monkeypatch.setattr(api, "_save_parent_bypassing_perms", lambda parent: seen.saved.append(parent.name))
	install_module(monkeypatch, "optimus.line_profile.capture", SimpleNamespace(
		is_active=lambda u: None,
		start_line_profile_pass=lambda **kw: seen.capture.append(("start", kw))
		or [{"dotted_path": "app.mod.fn", "source": "freeform", "eligible": True}],
		stop_line_profile_pass=lambda run_uuid, u: seen.capture.append(("stop", run_uuid, u)),
		CaptureError=RuntimeError,
	))
	install_module(monkeypatch, "optimus.line_profile.picker", SimpleNamespace(expand_hot_chain=lambda *a, **k: []))
	install_module(monkeypatch, "optimus.line_profile.analyzer", SimpleNamespace(run_analyze=lambda s, r: seen.analyzed.append((s, r))))
	scheduler = types.ModuleType("frappe.utils.scheduler")
	scheduler.is_scheduler_disabled = lambda: False
	monkeypatch.setitem(sys.modules, "frappe.utils.scheduler", scheduler)
	monkeypatch.setattr(api, "session", SimpleNamespace(get_active_session_for=lambda u: None))
	# api.py binds frappe.utils.now_datetime at import; the real one reads the site timezone.
	monkeypatch.setattr(api, "now_datetime", lambda: datetime.datetime(2026, 9, 24, 12, 0, 0))
	return fake, doc, seen


def test_start_accepts_json_picks_and_string_flag(monkeypatch):
	_, doc, seen = _env(monkeypatch)
	out = api.start_line_profile_pass(session_uuid=SESSION_UUID, picks=PICKS, auto_expand="0")
	assert out["session_uuid"] == SESSION_UUID and out["docname"] == DOCNAME
	assert out["auto_expanded"] is False
	((kind, kw),) = seen.capture
	assert kind == "start" and kw["user"] == OWNER
	assert kw["picks"] == [{"dotted_path": "app.mod.fn", "source": "freeform"}]
	assert seen.saved == [DOCNAME] and len(doc.phase_2_runs) == 2


def test_start_runs_as_the_caller_not_the_owner(monkeypatch):
	perms = {("read", DOCNAME, MANAGER): True, ("write", DOCNAME, MANAGER): True}
	_, _, seen = _env(monkeypatch, user=MANAGER, roles=("System Manager",), perms=perms)
	api.start_line_profile_pass(session_uuid=SESSION_UUID, picks=PICKS, auto_expand=False)
	((_, kw),) = seen.capture
	assert kw["user"] == MANAGER


def test_start_needs_a_ready_session(monkeypatch):
	_, _, seen = _env(monkeypatch, session_status="Failed")
	with pytest.raises(FakeValidationError):
		api.start_line_profile_pass(session_uuid=SESSION_UUID, picks=PICKS)
	assert seen.capture == []


def test_start_rejects_a_bad_picks_payload_with_a_title(monkeypatch):
	fake, _, seen = _env(monkeypatch)
	with pytest.raises(FakeValidationError):
		api.start_line_profile_pass(session_uuid=SESSION_UUID, picks="not json")
	assert fake.spies.throws[-1]["title"] == "Optimus" and seen.capture == []


def test_stop_by_the_owner(monkeypatch):
	fake, doc, seen = _env(monkeypatch)
	out = api.stop_line_profile_pass(run_uuid="run-1")
	assert out == {"run_uuid": "run-1", "session_uuid": SESSION_UUID, "status": "Analyzing"}
	assert seen.capture == [("stop", "run-1", OWNER)]
	assert doc.phase_2_runs[0].status == "Analyzing" and seen.saved == [DOCNAME]
	assert len(fake.spies.enqueue) == 1


def test_stop_rejects_run_not_recording_without_writes(monkeypatch):
	fake, _, seen = _env(monkeypatch, run_status="Analyzing")
	with pytest.raises(FakeValidationError):
		api.stop_line_profile_pass(run_uuid="run-1")
	assert seen.capture == [] and seen.saved == [] and fake.spies.enqueue == []


def test_stop_denies_a_stranger_before_the_run_status(monkeypatch):
	_, _, seen = _env(monkeypatch, user="stranger@example.com", perms={}, run_status="Ready")
	with pytest.raises(FakePermissionError):
		api.stop_line_profile_pass(run_uuid="run-1")
	assert seen.capture == []


def test_retry_phase2_analyze_by_the_owner(monkeypatch):
	_, _, seen = _env(monkeypatch, run_status="Failed")
	assert api.retry_phase2_analyze(run_uuid="run-1") == {"run_uuid": "run-1", "session_uuid": SESSION_UUID, "status": "Ready"}
	assert seen.analyzed == [(SESSION_UUID, "run-1")] and seen.saved == [DOCNAME]


def test_batch_isolates_a_foreign_run(monkeypatch):
	other = "other@example.com"
	doc = fake_session_doc(phase_2_runs=[SimpleNamespace(run_uuid="run-1", status="Failed", ended_at=None)])
	fake = make_fake_frappe(
		sessions={SESSION_UUID: session_row(), "uuid-2": session_row(name="SESS-2", owner=other)},
		runs={
			"run-1": {"name": "RUN-1", "parent": DOCNAME, "status": "Failed"},
			"run-2": {"name": "RUN-2", "parent": "SESS-2", "status": "Failed"},
		},
		perms=owner_perms(), docs={DOCNAME: doc},
	)
	install(monkeypatch, fake)
	saved, analyzed = [], []
	monkeypatch.setattr(api, "_save_parent_bypassing_perms", lambda parent: saved.append(parent.name))
	install_module(monkeypatch, "optimus.line_profile.analyzer", SimpleNamespace(run_analyze=lambda s, r: analyzed.append((s, r))))
	out = api.retry_phase2_analyzes_batch(run_uuids='["run-1", "run-2"]')
	assert analyzed == [(SESSION_UUID, "run-1")]
	assert out["tallies"] == {"Ready": 1, "Failed": 1, "Analyzing": 0, "Skipped": 0}
	assert out["results"][0] == {"run_uuid": "run-1", "session_uuid": SESSION_UUID, "status": "Ready"}
	assert out["results"][1]["run_uuid"] == "run-2" and out["results"][1]["status"] == "Failed"
	assert saved == [DOCNAME]
