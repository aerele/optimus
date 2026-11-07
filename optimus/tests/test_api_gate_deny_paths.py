# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Deny-path spies for every gated endpoint (PR-1).

For each endpoint in GATED_ENDPOINTS and each denied caller shape, the call must raise
PermissionError and leave no trace: no LLM, analyze, PDF or line profiling module touched (tripwired),
no enqueue, no set_value or sql, no get_doc, no commit and no rate-limit count.
"""

import types

import pytest

from optimus import api
from optimus.tests.gate_fakes import (
	DOCNAME,
	GATED_ENDPOINTS,
	OWNER,
	OWNER_SCOPED_ENDPOINTS,
	SESSION_UUID,
	FakePermissionError,
	FakeValidationError,
	Tripwire,
	install,
	install_tripwires,
	make_fake_frappe,
	session_row,
)

CALLS = {
	"cancel_ai_refresh": {"session_uuid": SESSION_UUID, "run_id": "fake-run"},
	"refill_ai_suggestions": {"session_uuid": SESSION_UUID},
	"regenerate_reports": {"session_uuid": SESSION_UUID},
	"retry_analyze": {"session_uuid": SESSION_UUID},
	"start_line_profile_pass": {"session_uuid": SESSION_UUID, "picks": '[{"dotted_path": "a.b", "source": "freeform"}]'},
	"stop_line_profile_pass": {"run_uuid": "run-1"},
	"retry_phase2_analyze": {"run_uuid": "run-1"},
}


def _boom(doctype, ptype, doc, user):
	raise RuntimeError("permission engine hiccup")


DENIED = {
	"stranger": {"user": "stranger@example.com", "perms": {}},
	"read_sharee": {"user": "sharee@example.com", "perms": {("read", DOCNAME, "sharee@example.com"): True}},
	"owner_read_revoked": {"user": OWNER, "perms": {}},
	"permission_engine_raises": {"user": OWNER, "has_permission": _boom},
}


def test_every_gated_endpoint_has_a_deny_case():
	assert set(CALLS) == set(GATED_ENDPOINTS)


@pytest.mark.parametrize("caller", sorted(DENIED))
@pytest.mark.parametrize("endpoint", sorted(GATED_ENDPOINTS))
def test_denied_caller_leaves_no_trace(monkeypatch, endpoint, caller):
	fake = make_fake_frappe(
		sessions={SESSION_UUID: session_row()},
		runs={"run-1": {"name": "RUN-1", "parent": DOCNAME, "status": "Recording"}},
		**DENIED[caller],
	)
	install(monkeypatch, fake)
	commits = []
	monkeypatch.setattr(api, "safe_commit", lambda: commits.append(True))
	session_wire = Tripwire("optimus.session")
	monkeypatch.setattr(api, "session", session_wire)
	wires = install_tripwires(
		monkeypatch,
		"optimus.ai_fix",
		"optimus.ai_jobs",
		"optimus.analyze",
		"optimus.pdf_export",
		"optimus.line_profile.capture",
		"optimus.line_profile.analyzer",
		"optimus.line_profile.jobs",
		"optimus.line_profile.picker",
	)

	with pytest.raises(FakePermissionError):
		getattr(api, endpoint)(**CALLS[endpoint])

	assert {name: wire.touched for name, wire in wires.items() if wire.touched} == {}
	assert session_wire.touched == []
	assert fake.spies.set_value == [] and fake.spies.sql == [] and fake.spies.enqueue == []
	assert fake.spies.get_doc == [] and fake.spies.publish_realtime == []
	assert commits == []
	assert fake.cache.calls == []  # the rate limit is counted only after permission passes


# Stop is owner-scoped, not gated: it must refuse these callers and leave no trace either.
STOP_DENIED = {
	"guest": {"user": "Guest", "roles": (), "error": FakeValidationError},
	"no_profiler_role": {"user": "plain@example.com", "roles": ("Desk User",), "error": FakePermissionError},
}


def test_stop_is_the_only_owner_scoped_endpoint():
	assert OWNER_SCOPED_ENDPOINTS == {"stop"}


@pytest.mark.parametrize("caller", sorted(STOP_DENIED))
@pytest.mark.parametrize("uuid", [None, SESSION_UUID])
def test_stop_refuses_callers_without_the_profiler_role_and_leaves_no_trace(monkeypatch, caller, uuid):
	caller_args = {k: v for k, v in STOP_DENIED[caller].items() if k != "error"}
	fake = make_fake_frappe(sessions={SESSION_UUID: session_row(status="Recording")}, **caller_args)
	install(monkeypatch, fake)
	commits = []
	monkeypatch.setattr(api, "safe_commit", lambda: commits.append(True))
	wire = Tripwire("optimus.session")
	monkeypatch.setattr(api, "session", wire)

	with pytest.raises(STOP_DENIED[caller]["error"]):
		api.stop(session_uuid=uuid)

	assert wire.touched == [] and commits == []
	assert fake.spies.set_value == [] and fake.spies.enqueue == [] and fake.cache.calls == []


@pytest.mark.parametrize("uuid", [None, SESSION_UUID])
def test_stop_never_touches_another_users_session(monkeypatch, uuid):
	fake = make_fake_frappe(
		user="intruder@example.com", sessions={SESSION_UUID: session_row(status="Recording")}
	)
	install(monkeypatch, fake)
	monkeypatch.setattr(api, "safe_commit", lambda: None)
	monkeypatch.setattr(api, "session", types.SimpleNamespace(get_active_session_for=lambda user: None))
	monkeypatch.setattr(api.ratelimit, "enforce_user_rate_limit", lambda *a, **kw: None)

	assert api.stop(session_uuid=uuid) == {"stopped": False, "reason": "no active session"}
	assert fake.spies.set_value == [] and fake.spies.enqueue == []
