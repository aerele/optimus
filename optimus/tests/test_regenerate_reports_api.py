# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""regenerate_reports and the internal render helpers (PR-1).

API rendering delegates to the transactional report helper. Report regeneration
never calls the model. Failure logs are pinned outside any ``except`` block: the fake
``log_ai_failure`` records ``sys.exc_info()[0]``, which is None only outside a handler.
"""

import sys
from types import SimpleNamespace

import pytest

from optimus import ai_fix, api
from optimus import analyze as _analyze
from optimus.tests.gate_fakes import (
	DOCNAME,
	OWNER,
	SESSION_UUID,
	FakePermissionError,
	FakeRateLimitExceededError,
	FakeValidationError,
	fake_session_doc,
	install,
	install_module,
	make_fake_frappe,
	owner_perms,
	session_row,
)


def _must_not_run(*args, **kwargs):
	raise AssertionError("regenerate_reports must not re-run analyze")


def _env(monkeypatch, *, status="Ready", user=OWNER, perms=None, conf=None):
	fake = make_fake_frappe(user=user, sessions={SESSION_UUID: session_row(status=status)},
		perms=owner_perms() if perms is None else perms, conf=conf)
	install(monkeypatch, fake)
	from optimus import ai_jobs, report_refresh
	seen = SimpleNamespace(rendered=[], logged=[])
	monkeypatch.setattr(ai_jobs, "active_refresh", lambda *a: None)
	def render(docname):
		seen.rendered.append((docname, 1))
		return {"regenerated": True, "recordings_available": 1, "actions_total": 2}
	monkeypatch.setattr(report_refresh, "render_report", render)
	monkeypatch.setattr(ai_fix, "suggest_fix", _must_not_run)
	monkeypatch.setattr(ai_fix, "humanize_steps", _must_not_run)
	def log_failure(title, exc=None, **kw):
		seen.logged.append((title, type(exc).__name__, sys.exc_info()[0]))
		guard = ai_fix._InterruptGuard()
		guard.note(exc)
		if guard.pending():
			raise guard.interrupt()
	monkeypatch.setattr(ai_fix, "log_ai_failure", log_failure)
	monkeypatch.setattr(api, "_enqueue_analyze", _must_not_run)
	return fake, seen


def test_render_session_report_only_delegates_to_transactional_report_helper(monkeypatch):
	_, seen = _env(monkeypatch)
	assert api._render_session_report(DOCNAME) == {"regenerated": True, "recordings_available": 1, "actions_total": 2}
	assert seen.rendered == [(DOCNAME, 1)]


def test_render_helper_rejects_the_removed_ai_side_effect_option(monkeypatch):
	_, seen = _env(monkeypatch)
	with pytest.raises(TypeError):
		api._render_session_report(DOCNAME, ai_backfill=True)
	assert not seen.rendered


def test_report_failure_is_logged_outside_except_and_returns_a_fixed_user_message(monkeypatch):
	_, seen = _env(monkeypatch)
	from optimus import report_refresh
	def fail(*a):
		raise OSError("fake rendering failure")
	monkeypatch.setattr(report_refresh, "render_report", fail)
	with pytest.raises(FakeValidationError, match="previous report was kept"):
		api._render_session_report(DOCNAME)
	assert seen.logged == [("optimus regenerate report", "OSError", None)]


def test_regenerate_reports_never_spends_tokens(monkeypatch):
	fake, seen = _env(monkeypatch)
	assert api.regenerate_reports(SESSION_UUID) == {
		"regenerated": True, "session_uuid": SESSION_UUID, "docname": DOCNAME,
		"recordings_available": 1, "actions_total": 2,
	}
	assert seen.rendered == [(DOCNAME, 1)] and not fake.spies.enqueue


def test_regenerate_reports_allows_failed_sessions(monkeypatch):
	_, seen = _env(monkeypatch, status="Failed")
	api.regenerate_reports(SESSION_UUID)
	assert seen.rendered == [(DOCNAME, 1)]


@pytest.mark.parametrize("status", ["Recording", "Stopping", "Capturing Background Jobs", "Analyzing"])
def test_regenerate_rejects_in_flight_sessions(monkeypatch, status):
	fake, seen = _env(monkeypatch, status=status)
	with pytest.raises(FakeValidationError):
		api.regenerate_reports(session_uuid=SESSION_UUID)
	assert seen.rendered == [] and fake.cache.calls == []
	assert fake.spies.throws[-1]["title"] == "Optimus"


def test_regenerate_refusal_keeps_develops_wording(monkeypatch):
	"""The frozen real-bench test tests_integration/test_regenerate_reports_idempotent.py
	(test_regenerate_refuses_non_terminal_status) asserts "terminal" (or "ready") and
	"retry_analyze" in this message; the gate must not replace it with its generic text."""
	_env(monkeypatch, status="Analyzing")
	with pytest.raises(FakeValidationError) as exc:
		api.regenerate_reports(session_uuid=SESSION_UUID)
	assert str(exc.value) == (
		"regenerate_reports requires the session to be in a terminal state (Ready or Failed); "
		"this one is 'Analyzing'. Wait for analyze to finish, or use retry_analyze to restart a "
		"stuck pipeline."
	)


def test_regenerate_reports_read_sharee_is_denied(monkeypatch):
	sharee = "sharee@example.com"
	_, seen = _env(monkeypatch, user=sharee, perms={("read", DOCNAME, sharee): True})
	with pytest.raises(FakePermissionError):
		api.regenerate_reports(session_uuid=SESSION_UUID)
	assert seen.rendered == []


def test_regenerate_reports_rate_limited_per_user(monkeypatch):
	_env(monkeypatch, conf={"optimus_rate_limits": {"regenerate_reports": [1, 60]}})
	api.regenerate_reports(session_uuid=SESSION_UUID)
	with pytest.raises(FakeRateLimitExceededError):
		api.regenerate_reports(session_uuid=SESSION_UUID)


def test_phase2_worker_rerender_does_not_call_http_endpoint(monkeypatch):
	"""Trusted worker re-renders already-authorized work without a second rate limit."""
	_, seen = _env(monkeypatch)
	from optimus.line_profile import jobs

	monkeypatch.setattr(api, "regenerate_reports", _must_not_run)
	jobs._render({"parent": DOCNAME})
	assert seen.rendered == [(DOCNAME, 1)]



class _JobTimeout(Exception):
	pass


def test_report_api_propagates_job_timeouts_without_the_failed_frames(monkeypatch):
	_, seen = _env(monkeypatch)
	monkeypatch.setattr(ai_fix, "_job_timeout_types", lambda: (_JobTimeout,))
	original = _JobTimeout("fake job expired")
	def interrupted(*a):
		raise original
	from optimus import report_refresh
	monkeypatch.setattr(report_refresh, "render_report", interrupted)
	with pytest.raises(_JobTimeout) as caught:
		api._render_session_report(DOCNAME)
	assert caught.value is not original
	assert caught.value.__context__ is None and caught.value.__cause__ is None
	tb = caught.value.__traceback__
	while tb:
		assert tb.tb_frame.f_code is not interrupted.__code__
		tb = tb.tb_next
	assert seen.logged == [("optimus regenerate report", "_JobTimeout", None)]
