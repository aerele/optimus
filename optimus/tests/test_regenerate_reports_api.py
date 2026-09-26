# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""regenerate_reports and the internal render helpers (PR-1).

``_render_session_report`` is the one re-render path. AI endpoints re-render through
``_rerender_after_ai`` (default ``ai_backfill=False``, never the whitelisted
``regenerate_reports``), and ``regenerate_reports`` keeps ``ai_backfill=True`` until the AI path
leaves regenerate. Failure logs are pinned to run outside any ``except`` block: the fake
``log_ai_failure`` records ``sys.exc_info()[0]``, which is None only outside a handler.
"""

import sys
from types import SimpleNamespace

import pytest

from optimus import ai_fix, api
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


def _env(monkeypatch, *, status="Ready", user=OWNER, perms=None, fetch_raises=False, backfill_raises=False, conf=None):
	doc = fake_session_doc(actions=[SimpleNamespace(recording_uuid="rec-1"), SimpleNamespace(recording_uuid="")])
	fake = make_fake_frappe(
		user=user, sessions={SESSION_UUID: session_row(status=status)},
		perms=owner_perms() if perms is None else perms, docs={DOCNAME: doc}, conf=conf,
	)
	install(monkeypatch, fake)
	seen = SimpleNamespace(fetched=[], backfilled=[], rendered=[], cleared=[], logged=[])

	def fetch(uuids, recordings_bundle=None):
		if fetch_raises:
			raise RuntimeError("redis gone")
		seen.fetched.append(list(uuids))
		return [{"uuid": "rec-1"}]

	def backfill(d):
		if backfill_raises:
			raise RuntimeError("llm down")
		seen.backfilled.append(d.name)

	install_module(monkeypatch, "optimus.analyze", SimpleNamespace(
		_fetch_recordings=fetch,
		_load_recordings_bundle=lambda d: None,
		_backfill_ai_suggestions=backfill,
		_render_and_attach_reports=lambda name, recs: seen.rendered.append((name, len(recs))),
	))
	install_module(monkeypatch, "optimus.pdf_export", SimpleNamespace(clear_cached_pdf=lambda u: seen.cleared.append(u)))
	monkeypatch.setattr(
		ai_fix, "log_ai_failure",
		lambda title, exc=None, **kw: seen.logged.append((title, type(exc).__name__, sys.exc_info()[0])),
	)
	monkeypatch.setattr(api, "_enqueue_analyze", _must_not_run)
	return fake, seen


def _ref():
	return api.SessionRef(
		docname=DOCNAME, session_uuid=SESSION_UUID, owner=OWNER, user=OWNER, status="Ready", title=None
	)


def test_render_session_report_is_render_only_by_default(monkeypatch):
	_, seen = _env(monkeypatch)
	out = api._render_session_report(DOCNAME)
	assert out == {"regenerated": True, "recordings_available": 1, "actions_total": 2}
	assert seen.fetched == [["rec-1"]]
	assert seen.backfilled == []
	assert seen.rendered == [(DOCNAME, 1)]
	assert seen.cleared == [SESSION_UUID]


def test_render_session_report_ai_backfill_true_backfills_first(monkeypatch):
	_, seen = _env(monkeypatch)
	api._render_session_report(DOCNAME, ai_backfill=True)
	assert seen.backfilled == [DOCNAME] and seen.rendered == [(DOCNAME, 1)]


def test_expired_recordings_render_with_an_empty_list(monkeypatch):
	_, seen = _env(monkeypatch, fetch_raises=True)
	out = api._render_session_report(DOCNAME)
	assert out["recordings_available"] == 0 and seen.rendered == [(DOCNAME, 0)]
	# Logged once, with the exception, and outside the except block (no active exception).
	assert seen.logged == [("optimus regenerate_reports fetch", "RuntimeError", None)]


def test_backfill_failure_still_renders(monkeypatch):
	_, seen = _env(monkeypatch, backfill_raises=True)
	api._render_session_report(DOCNAME, ai_backfill=True)
	assert seen.rendered == [(DOCNAME, 1)]
	assert seen.logged == [("optimus regenerate ai backfill", "RuntimeError", None)]


def test_rerender_after_ai_success_never_backfills(monkeypatch):
	_, seen = _env(monkeypatch)
	assert api._rerender_after_ai(_ref()) is True
	assert seen.backfilled == [] and seen.rendered == [(DOCNAME, 1)]


def test_rerender_after_ai_reports_failure_instead_of_raising(monkeypatch):
	fake, seen = _env(monkeypatch)

	def boom(docname, *, ai_backfill=False):
		raise RuntimeError("disk full")

	monkeypatch.setattr(api, "_render_session_report", boom)
	assert api._rerender_after_ai(_ref()) is False
	assert seen.logged == [("optimus AI re-render", "RuntimeError", None)]
	assert len(fake.spies.rollback) == 1


def test_regenerate_reports_keeps_ai_backfill_until_pr2(monkeypatch):
	fake, seen = _env(monkeypatch)
	out = api.regenerate_reports(session_uuid=SESSION_UUID)
	assert out == {
		"regenerated": True, "session_uuid": SESSION_UUID, "docname": DOCNAME,
		"recordings_available": 1, "actions_total": 2,
	}
	assert seen.backfilled == [DOCNAME]
	assert fake.spies.enqueue == []


def test_regenerate_reports_allows_failed_sessions(monkeypatch):
	_, seen = _env(monkeypatch, status="Failed")
	api.regenerate_reports(session_uuid=SESSION_UUID)
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


def test_phase2_worker_rerender_passes_gate_for_owner(monkeypatch):
	"""line_profile/analyzer._regenerate_parent_reports (frozen) calls the whitelisted
	regenerate_reports as the enqueuing user; a plain Optimus User owner must now pass."""
	_, seen = _env(monkeypatch)
	from optimus.line_profile import analyzer as lp_analyzer

	lp_analyzer._regenerate_parent_reports(SESSION_UUID)
	assert seen.rendered == [(DOCNAME, 1)]


class _JobTimeout(Exception):
	pass


@pytest.mark.parametrize("stage", ["fetch", "backfill", "pdf", "render", "rollback"])
def test_report_helpers_propagate_job_timeouts_without_the_failed_frames(monkeypatch, stage):
	fake, seen = _env(monkeypatch)
	monkeypatch.setattr(ai_fix, "_job_timeout_types", lambda: (_JobTimeout,))
	original = _JobTimeout("job expired")

	def interrupted(*args, **kwargs):
		raise original

	from optimus import analyze, pdf_export

	if stage == "fetch":
		monkeypatch.setattr(analyze, "_fetch_recordings", interrupted)
	elif stage == "backfill":
		monkeypatch.setattr(analyze, "_backfill_ai_suggestions", interrupted)
	elif stage == "pdf":
		monkeypatch.setattr(pdf_export, "clear_cached_pdf", interrupted)
	elif stage == "render":
		monkeypatch.setattr(api, "_render_session_report", interrupted)
	else:
		def broken_render(*args, **kwargs):
			raise RuntimeError("disk full")
		monkeypatch.setattr(api, "_render_session_report", broken_render)
		fake.db.rollback = interrupted

	with pytest.raises(_JobTimeout) as caught:
		if stage in {"render", "rollback"}:
			api._rerender_after_ai(_ref())
		else:
			api._render_session_report(DOCNAME, ai_backfill=True)
	assert caught.value is not original
	assert caught.value.args == original.args
	assert caught.value.__context__ is None and caught.value.__cause__ is None
	frames = []
	tb = caught.value.__traceback__
	while tb:
		frames.append(tb.tb_frame.f_code.co_name)
		tb = tb.tb_next
	assert "interrupted" not in frames
	assert seen.logged == [] and seen.rendered == []
