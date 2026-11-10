"""A live line profiling run keeps its active key while profiled traffic continues."""

import types

import frappe
import pytest

from optimus import redis_keys, session
from optimus.line_profile import capture as cap
from optimus.line_profile import hooks as lp_hooks


class FakeProfiler:
	def enable_by_count(self):
		pass

	def disable_by_count(self):
		pass

	def get_stats(self):
		return types.SimpleNamespace(timings={}, unit=1e-6)


@pytest.fixture
def env(monkeypatch):
	monkeypatch.setattr(frappe, "log_error", lambda *a, **k: None, raising=False)
	monkeypatch.setattr(frappe, "session", types.SimpleNamespace(user="u@x.com"), raising=False)
	monkeypatch.setattr(frappe, "local", types.SimpleNamespace(), raising=False)
	monkeypatch.setattr(frappe, "conf", types.SimpleNamespace(get=lambda k, d=None: 0 if "budget" in k else d), raising=False)
	monkeypatch.setattr(cap, "make_profiler", lambda run: FakeProfiler())
	monkeypatch.setattr(cap, "flush_samples", lambda run, samples: None)
	monkeypatch.setattr(lp_hooks.hooks_callbacks, "_should_skip_request", lambda: False, raising=False)
	renewed = []
	monkeypatch.setattr(cap, "renew_active", renewed.append)
	yield types.SimpleNamespace(renewed=renewed, monkeypatch=monkeypatch)
	frappe.local._lp_profiler = None
	if cap.active_profiler_count():
		cap.decr_active_profilers()


def test_profiled_request_renews_the_active_key(env):
	env.monkeypatch.setattr(cap, "is_active", lambda user: "run-1")
	lp_hooks.before_request_line_profile()
	assert env.renewed == ["u@x.com"]
	lp_hooks.after_request_line_profile()


def test_request_without_a_run_does_not_renew(env):
	env.monkeypatch.setattr(cap, "is_active", lambda user: None)
	lp_hooks.before_request_line_profile()
	assert env.renewed == []


def test_skipped_optimus_request_does_not_renew(env):
	env.monkeypatch.setattr(cap, "is_active", lambda user: "run-1")
	env.monkeypatch.setattr(lp_hooks.hooks_callbacks, "_should_skip_request", lambda: True, raising=False)
	lp_hooks.before_request_line_profile()
	assert env.renewed == []


def test_profiled_job_renews_only_for_its_own_run(env):
	env.monkeypatch.setattr(cap, "is_active", lambda user: "run-1")
	lp_hooks.before_job_line_profile(kwargs={"_lp_session_id": "run-2"})
	assert env.renewed == []
	lp_hooks.before_job_line_profile(kwargs={"_lp_session_id": "run-1"})
	assert env.renewed == ["u@x.com"]
	lp_hooks.after_job_line_profile()


@pytest.mark.parametrize("hook", ["request", "job"])
def test_a_failed_renewal_is_logged_as_such_and_keeps_profiling(env, hook):
	def boom(user):
		raise ConnectionError("synthetic Redis failure")
	logged = []
	env.monkeypatch.setattr(cap, "renew_active", boom)
	env.monkeypatch.setattr(lp_hooks, "_log_capture_failure", lambda title, exc: logged.append(title))
	env.monkeypatch.setattr(cap, "is_active", lambda user: "run-1")
	if hook == "request":
		lp_hooks.before_request_line_profile()
	else:
		lp_hooks.before_job_line_profile(kwargs={"_lp_session_id": "run-1"})
	assert logged == ["line profiling active key renewal failed"]
	assert frappe.local._lp_profiler is not None
	if hook == "request":
		lp_hooks.after_request_line_profile()
	else:
		lp_hooks.after_job_line_profile()


def test_renew_falls_back_to_redis_expire_on_frappe_15(monkeypatch):
	calls = []
	cache = types.SimpleNamespace(
		make_key=lambda key: f"site|{key}",
		expire=lambda key, ttl: calls.append((key, ttl)),
	)
	monkeypatch.setattr(session, "frappe", types.SimpleNamespace(cache=cache))
	cap.renew_active("u@x.com")
	assert calls == [(f"site|{redis_keys.lp_active('u@x.com')}", cap.SESSION_TTL_SECONDS)]


def test_renew_uses_expire_so_a_stopped_run_is_not_recreated(monkeypatch):
	calls = []
	cache = types.SimpleNamespace(
		expire_key=lambda key, ttl: calls.append((key, ttl)),
		set_value=lambda *a, **k: pytest.fail("renewal must not SET the key"),
	)
	monkeypatch.setattr(session, "frappe", types.SimpleNamespace(cache=cache))
	cap.renew_active("u@x.com")
	assert calls == [(redis_keys.lp_active("u@x.com"), cap.SESSION_TTL_SECONDS)]
