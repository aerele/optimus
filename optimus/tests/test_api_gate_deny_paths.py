# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Deny-path spies for every gated endpoint (PR-1).

For each endpoint in GATED_ENDPOINTS and each denied caller shape, the call must raise
PermissionError and leave no trace: no LLM, analyze, PDF or Phase 2 module touched (tripwired),
no enqueue, no set_value or sql, no get_doc, no commit and no rate-limit count.
"""

import pytest

from optimus import api
from optimus.tests.gate_fakes import (
	DOCNAME,
	GATED_ENDPOINTS,
	OWNER,
	SESSION_UUID,
	FakePermissionError,
	Tripwire,
	install,
	install_tripwires,
	make_fake_frappe,
	session_row,
)

CALLS = {
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
		"optimus.analyze",
		"optimus.pdf_export",
		"optimus.line_profile.capture",
		"optimus.line_profile.analyzer",
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
