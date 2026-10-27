# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""retry_analyze behind the session gate, per-user limits on the two read endpoints, and
POST-only on the non-session mutating endpoints (PR-1)."""

import ast
import os
from types import SimpleNamespace

import pytest

from optimus import api
from optimus.tests.gate_fakes import (
	DOCNAME,
	OWNER,
	SESSION_UUID,
	FakePermissionError,
	FakeRateLimitExceededError,
	FakeValidationError,
	install,
	install_module,
	make_fake_frappe,
	owner_perms,
	session_row,
)

_API_PATH = os.path.join(os.path.dirname(__file__), "..", "api.py")


# --- retry_analyze ---------------------------------------------------------------------------


def _retry_env(monkeypatch, *, status="Failed", conf=None):
	fake = make_fake_frappe(sessions={SESSION_UUID: session_row(status=status)}, perms=owner_perms(), conf=conf)
	install(monkeypatch, fake)
	seen = SimpleNamespace(commits=[], enqueued=[], cleared=[])
	monkeypatch.setattr(api, "safe_commit", lambda: seen.commits.append(True))
	monkeypatch.setattr(
		api, "_enqueue_analyze", lambda uuid, docname=None: seen.enqueued.append((uuid, docname)) or False
	)
	install_module(monkeypatch, "optimus.pdf_export", SimpleNamespace(clear_cached_pdf=lambda u: seen.cleared.append(u)))
	return fake, seen


def test_owner_retries_a_failed_session(monkeypatch):
	fake, seen = _retry_env(monkeypatch)
	out = api.retry_analyze(session_uuid=SESSION_UUID)
	assert out == {"retried": True, "session_uuid": SESSION_UUID, "docname": DOCNAME, "ran_inline": False, "status": None}
	((args, _kwargs),) = fake.spies.set_value
	assert args == ("Optimus Session", DOCNAME, {"status": "Stopping", "analyzer_warnings": None})
	assert seen.commits == [True]
	assert seen.enqueued == [(SESSION_UUID, DOCNAME)]
	assert seen.cleared == [SESSION_UUID]


@pytest.mark.parametrize("status", ["Ready", "Analyzing", "Stopping"])
def test_retry_analyze_rejects_non_failed_without_writes(monkeypatch, status):
	fake, seen = _retry_env(monkeypatch, status=status)
	with pytest.raises(FakeValidationError):
		api.retry_analyze(session_uuid=SESSION_UUID)
	assert fake.spies.set_value == [] and seen.enqueued == [] and fake.cache.calls == []
	assert fake.spies.throws[-1]["title"] == "Optimus"


def test_retry_analyze_rate_limited_per_user(monkeypatch):
	_retry_env(monkeypatch, conf={"optimus_rate_limits": {"retry_analyze": [1, 60]}})
	api.retry_analyze(session_uuid=SESSION_UUID)
	with pytest.raises(FakeRateLimitExceededError):
		api.retry_analyze(session_uuid=SESSION_UUID)


# --- download_pdf / export_session: per-user limit after the read check ----------------------


def _read_env(monkeypatch, *, deny=False):
	fake = make_fake_frappe(sessions={SESSION_UUID: session_row()})
	install(monkeypatch, fake)
	order = []

	def require(uuid, ptype="read"):
		order.append(("perm", ptype))
		if deny:
			raise FakePermissionError("no")
		return DOCNAME

	monkeypatch.setattr(api, "_require_session_permission", require)
	monkeypatch.setattr(
		api.ratelimit, "enforce_user_rate_limit",
		lambda action, *, limit, seconds: order.append((action, limit, seconds)),
	)
	return fake, order


def test_download_pdf_counts_the_limit_after_permission(monkeypatch):
	_, order = _read_env(monkeypatch)
	install_module(monkeypatch, "optimus.pdf_export", SimpleNamespace(get_or_generate_pdf=lambda u: "/private/files/x.pdf"))
	assert api.download_pdf(session_uuid=SESSION_UUID) == {"file_url": "/private/files/x.pdf"}
	assert order == [("perm", "read"), ("download_pdf", 20, 60)]


def test_download_pdf_denied_caller_is_not_counted(monkeypatch):
	_, order = _read_env(monkeypatch, deny=True)
	with pytest.raises(FakePermissionError):
		api.download_pdf(session_uuid=SESSION_UUID)
	assert order == [("perm", "read")]


def test_export_session_counts_the_limit_after_permission(monkeypatch):
	fake, order = _read_env(monkeypatch)

	class _Stop(Exception):
		pass

	def stop(action, *, limit, seconds):
		order.append((action, limit, seconds))
		raise _Stop

	fake.db.get_value = lambda *a, **k: session_row()
	fake.get_doc = lambda *a, **k: SimpleNamespace(user=OWNER)
	monkeypatch.setattr(api.ratelimit, "enforce_user_rate_limit", stop)
	with pytest.raises(_Stop):
		api.export_session(session_uuid=SESSION_UUID)
	assert order == [("perm", "read"), ("export_session", 20, 60)]


# --- POST-only on the non-session mutating endpoints -----------------------------------------


def _whitelist_methods() -> dict:
	with open(_API_PATH, encoding="utf-8") as f:
		tree = ast.parse(f.read())
	methods = {}
	for node in tree.body:
		if not isinstance(node, ast.FunctionDef):
			continue
		for dec in node.decorator_list:
			if isinstance(dec, ast.Call) and ast.unparse(dec.func) == "frappe.whitelist":
				methods[node.name] = next(
					(ast.literal_eval(k.value) for k in dec.keywords if k.arg == "methods"), None
				)
	return methods


@pytest.mark.parametrize(
	"name",
	["start", "stop", "mark_onboarding_seen", "force_stop_phase2", "test_ai_connection", "submit_frontend_metrics", "retry_analyze"],
)
def test_mutating_endpoint_is_post_only(name):
	assert _whitelist_methods()[name] == ["POST"]


def test_no_frappe_rate_limit_decorator_left():
	with open(_API_PATH, encoding="utf-8") as f:
		src = f.read()
	assert "frappe.rate_limiter" not in src and "@rate_limit(" not in src


@pytest.mark.parametrize("endpoint", ["download_pdf", "export_session"])
def test_read_sharee_refusal_does_not_consume_export_quota(monkeypatch, endpoint):
	fake, order = _read_env(monkeypatch)
	fake.session.user = "sharee@example.com"
	# The session is readable, but PDF/JSON exports retain their recording-user gate.
	fake.db.get_value = lambda *a, **k: session_row()
	fake.get_doc = lambda *a, **k: SimpleNamespace(user=OWNER)
	with pytest.raises(FakePermissionError):
		getattr(api, endpoint)(session_uuid=SESSION_UUID)
	assert order == [("perm", "read")]
