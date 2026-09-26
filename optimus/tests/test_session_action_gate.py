# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The session gates in optimus.api: _session_action_gate, _ai_session_gate, _phase2_run_gate.

``frappe`` is replaced wholesale (gate_fakes.make_fake_frappe) with a permission engine keyed on
(ptype, docname, user), so these tests prove the gate asks about the right document for the right
user, fails closed, checks status only after permission and counts the rate limit last.
"""

import pytest

from optimus import ai_fix, api
from optimus.tests.gate_fakes import (
	DOCNAME,
	OWNER,
	SESSION_UUID,
	FakeDoesNotExistError,
	FakePermissionError,
	FakeRateLimitExceededError,
	FakeValidationError,
	install,
	make_fake_frappe,
	owner_perms,
	session_row,
)

SHAREE = "sharee@example.com"
MANAGER = "manager@example.com"
STRANGER = "stranger@example.com"


def _install(
	monkeypatch, *, user=OWNER, roles=("Optimus User",), perms=None, row=None, runs=None,
	has_permission=None, conf=None,
):
	fake = make_fake_frappe(
		user=user, roles=roles, sessions={SESSION_UUID: row or session_row()}, runs=runs,
		perms=perms if perms is not None else {}, has_permission=has_permission, conf=conf,
	)
	install(monkeypatch, fake)
	return fake


# --- _session_action_gate: the D1 matrix through the real wrapper -----------------------------

MATRIX = [
	("owner", OWNER, ("Optimus User",), {("read", DOCNAME, OWNER): True}, True),
	("read_sharee", SHAREE, ("Optimus User",), {("read", DOCNAME, SHAREE): True}, False),
	(
		"write_sharee", SHAREE, ("Optimus User",),
		{("read", DOCNAME, SHAREE): True, ("write", DOCNAME, SHAREE): True}, True,
	),
	(
		"system_manager", MANAGER, ("System Manager",),
		{("read", DOCNAME, MANAGER): True, ("write", DOCNAME, MANAGER): True}, True,
	),
	("stranger", STRANGER, ("Optimus User",), {}, False),
	("owner_read_revoked", OWNER, ("Optimus User",), {}, False),
]


@pytest.mark.parametrize("label,user,roles,perms,allowed", MATRIX, ids=[r[0] for r in MATRIX])
def test_gate_matrix(monkeypatch, label, user, roles, perms, allowed):
	fake = _install(monkeypatch, user=user, roles=roles, perms=perms)
	if allowed:
		ref = api._session_action_gate(SESSION_UUID, action="regenerate_reports")
		assert ref == api.SessionRef(
			docname=DOCNAME, session_uuid=SESSION_UUID, owner=OWNER, user=OWNER,
			status="Ready", title="Checkout flow",
		)
	else:
		with pytest.raises(FakePermissionError):
			api._session_action_gate(SESSION_UUID, action="regenerate_reports")
		assert fake.spies.throws[-1]["title"] == "Optimus"
	# Frappe is asked about THIS document for THIS caller, for both read and write.
	assert ("Optimus Session", "read", DOCNAME, user) in fake.spies.has_permission
	assert ("Optimus Session", "write", DOCNAME, user) in fake.spies.has_permission


def test_gate_fails_closed_when_permission_engine_raises(monkeypatch):
	def boom(doctype, ptype, doc, user):
		raise RuntimeError("permission engine hiccup")

	_install(monkeypatch, has_permission=boom)
	with pytest.raises(FakePermissionError):
		api._session_action_gate(SESSION_UUID, action="regenerate_reports")


def test_gate_keys_on_owner_not_the_user_field(monkeypatch):
	_install(monkeypatch, perms=owner_perms(), row=session_row(owner="admin@example.com", user=OWNER))
	with pytest.raises(FakePermissionError):
		api._session_action_gate(SESSION_UUID, action="regenerate_reports")


def test_gate_owner_match_ignores_email_case(monkeypatch):
	_install(monkeypatch, perms=owner_perms(), row=session_row(owner="Owner@Example.com"))
	assert api._session_action_gate(SESSION_UUID, action="regenerate_reports").owner == "Owner@Example.com"


def test_gate_requires_the_profiler_role_before_any_lookup(monkeypatch):
	fake = _install(monkeypatch, roles=(), perms=owner_perms())
	with pytest.raises(FakePermissionError):
		api._session_action_gate(SESSION_UUID, action="regenerate_reports")
	assert fake.spies.has_permission == []


def test_gate_blank_uuid(monkeypatch):
	_install(monkeypatch, perms=owner_perms())
	with pytest.raises(FakeValidationError):
		api._session_action_gate("", action="regenerate_reports")


def test_gate_unknown_uuid_is_escaped(monkeypatch):
	fake = _install(monkeypatch, perms=owner_perms())
	with pytest.raises(FakeDoesNotExistError) as exc:
		api._session_action_gate("<b>x</b>", action="regenerate_reports")
	assert "&lt;b&gt;" in str(exc.value) and "<b>" not in str(exc.value)
	assert fake.spies.throws[-1]["title"] == "Optimus"


def test_gate_checks_permission_before_status(monkeypatch):
	_install(monkeypatch, user=STRANGER, row=session_row(status="Analyzing"))
	with pytest.raises(FakePermissionError):
		api._session_action_gate(SESSION_UUID, action="regenerate_reports")


def test_gate_status_mismatch(monkeypatch):
	fake = _install(monkeypatch, perms=owner_perms(), row=session_row(status="Analyzing"))
	with pytest.raises(FakeValidationError) as exc:
		api._session_action_gate(SESSION_UUID, action="regenerate_reports", statuses=("Ready", "Failed"))
	assert str(exc.value) == "This action needs a session in status Ready, Failed. This one is Analyzing."
	assert fake.spies.throws[-1]["title"] == "Optimus"


def test_gate_status_hint_replaces_the_default_message(monkeypatch):
	_install(monkeypatch, perms=owner_perms(), row=session_row(status="Analyzing"))
	with pytest.raises(FakeValidationError) as exc:
		api._session_action_gate(
			SESSION_UUID, action="regenerate_reports", statuses=("Ready",), status_hint="Busy: '{0}'."
		)
	assert str(exc.value) == "Busy: 'Analyzing'."


def test_gate_status_hint_is_unused_when_the_status_matches(monkeypatch):
	_install(monkeypatch, perms=owner_perms())
	ref = api._session_action_gate(SESSION_UUID, action="x", status_hint="never shown {0}")
	assert ref.status == "Ready"


def test_gate_empty_statuses_accept_any_status(monkeypatch):
	_install(monkeypatch, perms=owner_perms(), row=session_row(status="Stopping"))
	assert api._session_action_gate(SESSION_UUID, action="x", statuses=()).status == "Stopping"


def test_read_gate_escapes_an_unknown_uuid(monkeypatch):
	fake = _install(monkeypatch, perms=owner_perms())
	with pytest.raises(FakeDoesNotExistError) as exc:
		api._require_session_permission("<img src=x>")
	assert "&lt;img" in str(exc.value) and "<img" not in str(exc.value)
	assert fake.spies.throws[-1]["title"] == "Optimus"


def test_gate_never_writes_or_counts(monkeypatch):
	fake = _install(monkeypatch, perms=owner_perms())
	api._session_action_gate(SESSION_UUID, action="regenerate_reports")
	assert fake.spies.set_value == [] and fake.spies.sql == [] and fake.spies.enqueue == []
	assert fake.cache.calls == []


# --- _ai_session_gate -------------------------------------------------------------------------


def _ai(monkeypatch, *, configured=True, section_on=True, **kw):
	fake = _install(monkeypatch, **kw)
	asked = []

	def is_available(section=None):
		asked.append(section)
		return configured and (section is None or section_on)

	monkeypatch.setattr(ai_fix, "is_available", is_available)
	return fake, asked


def test_ai_gate_allows_owner_and_counts_one_call(monkeypatch):
	fake, asked = _ai(monkeypatch, perms=owner_perms())
	ref = api._ai_session_gate(SESSION_UUID, section="findings", action="refill_ai_suggestions")
	assert ref.docname == DOCNAME
	assert asked == [None, "findings"]
	incr = [c for c in fake.cache.calls if c[0] == "incr"]
	assert incr == [("incr", "site|optimus:ratelimit:refill_ai_suggestions:3600:owner@example.com")]


def test_ai_gate_not_configured_is_refused_and_not_counted(monkeypatch):
	fake, _ = _ai(monkeypatch, configured=False, perms=owner_perms())
	with pytest.raises(FakeValidationError) as exc:
		api._ai_session_gate(SESSION_UUID, section=None, action="refill_ai_suggestions")
	assert "aren't configured" in str(exc.value)
	assert fake.cache.calls == []


@pytest.mark.parametrize(
	"section,needle",
	[("findings", "Fix suggestions on findings"), ("humanize", "Steps to Reproduce")],
)
def test_ai_gate_section_toggle_off(monkeypatch, section, needle):
	fake, _ = _ai(monkeypatch, section_on=False, perms=owner_perms())
	with pytest.raises(FakeValidationError) as exc:
		api._ai_session_gate(SESSION_UUID, section=section, action="refill_ai_suggestions")
	assert needle in str(exc.value)
	assert fake.cache.calls == []


def test_ai_gate_without_section_skips_the_toggle(monkeypatch):
	_, asked = _ai(monkeypatch, section_on=False, perms=owner_perms())
	api._ai_session_gate(SESSION_UUID, section=None, action="refill_ai_suggestions")
	assert asked == [None]


def test_ai_gate_denied_caller_is_not_counted_and_ai_not_consulted(monkeypatch):
	fake, asked = _ai(monkeypatch, user=STRANGER)
	with pytest.raises(FakePermissionError):
		api._ai_session_gate(SESSION_UUID, section="findings", action="refill_ai_suggestions")
	assert asked == [] and fake.cache.calls == []


def test_ai_gate_needs_a_ready_session(monkeypatch):
	_, asked = _ai(monkeypatch, perms=owner_perms(), row=session_row(status="Failed"))
	with pytest.raises(FakeValidationError):
		api._ai_session_gate(SESSION_UUID, section=None, action="refill_ai_suggestions")
	assert asked == []


def test_ai_gate_rate_limit(monkeypatch):
	_ai(monkeypatch, perms=owner_perms(), conf={"optimus_rate_limits": {"refill_ai_suggestions": [2, 3600]}})
	api._ai_session_gate(SESSION_UUID, section=None, action="refill_ai_suggestions")
	api._ai_session_gate(SESSION_UUID, section=None, action="refill_ai_suggestions")
	with pytest.raises(FakeRateLimitExceededError):
		api._ai_session_gate(SESSION_UUID, section=None, action="refill_ai_suggestions")


def test_limit_tables_are_the_reviewed_defaults():
	# Exact pins: only the endpoints that exist are limited, and PR-3's status poll and cancel
	# are deliberately absent (not rate-limited).
	assert api._AI_LIMITS == {
		"refill_ai_suggestions": {"limit": 6, "seconds": 3600},
		"test_ai_connection": {"limit": 10, "seconds": 60},
	}
	assert api._ACTION_LIMITS == {
		"regenerate_reports": {"limit": 30, "seconds": 60},
		"retry_analyze": {"limit": 5, "seconds": 60},
		"download_pdf": {"limit": 20, "seconds": 60},
		"export_session": {"limit": 20, "seconds": 60},
	}


# --- _phase2_run_gate -------------------------------------------------------------------------

RUNS = {
	"run-1": {"name": "RUN-1", "parent": DOCNAME, "status": "Recording"},
	"orphan": {"name": "RUN-9", "parent": "GONE", "status": "Recording"},
}


def test_run_gate_resolves_the_run_to_its_session(monkeypatch):
	_install(monkeypatch, perms=owner_perms(), runs=RUNS)
	ref, run = api._phase2_run_gate("run-1", action="stop_line_profile_pass", run_statuses=("Recording",))
	assert ref.session_uuid == SESSION_UUID and run["name"] == "RUN-1"


@pytest.mark.parametrize("run_uuid", ["nope", "orphan"])
def test_run_gate_unknown_or_orphan_run(monkeypatch, run_uuid):
	_install(monkeypatch, perms=owner_perms(), runs=RUNS)
	with pytest.raises(FakeDoesNotExistError):
		api._phase2_run_gate(run_uuid, action="retry_phase2_analyze")


def test_run_gate_checks_permission_before_run_status(monkeypatch):
	_install(monkeypatch, user=STRANGER, runs={"run-1": dict(RUNS["run-1"], status="Ready")})
	with pytest.raises(FakePermissionError):
		api._phase2_run_gate("run-1", action="stop_line_profile_pass", run_statuses=("Recording",))


def test_run_gate_run_status_mismatch(monkeypatch):
	_install(monkeypatch, perms=owner_perms(), runs={"run-1": dict(RUNS["run-1"], status="Ready")})
	with pytest.raises(FakeValidationError):
		api._phase2_run_gate("run-1", action="stop_line_profile_pass", run_statuses=("Recording",))


def test_run_gate_ignores_the_parent_status(monkeypatch):
	_install(monkeypatch, perms=owner_perms(), runs=RUNS, row=session_row(status="Failed"))
	ref, _ = api._phase2_run_gate("run-1", action="retry_phase2_analyze")
	assert ref.status == "Failed"
