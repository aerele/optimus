# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Lock-event plumbing: cap order, interrupts, Frappe's job retry, and the Redis dump/read/cleanup."""

import sys
import types

import pytest

from optimus import capture
from optimus.analyzers import lock_contention
from optimus.analyzers.base import AnalyzeContext
from optimus.tests import test_lock_contention as base
from optimus.tests.test_lock_contention import _event, _FakeLocal, _frames, _lock_error

lock_env = base.lock_env  # noqa: F811  (reuse the fixture; the tests below take it by name)

# ----- capture wrap ----------------------------------------------------------


def test_cap_is_checked_before_the_stack_walk(lock_env, monkeypatch):
	import frappe

	lock_env.local.optimus_lock_events = [_event()] * capture.LOCK_EVENTS_CAP_PER_RECORDING
	walked = []
	monkeypatch.setattr(capture, "_lock_caller_frames", lambda: walked.append(1) or _frames())
	lock_env.raises(_lock_error())
	with pytest.raises(frappe.QueryDeadlockError):
		lock_env.db.sql("SELECT 1")
	assert walked == []
	assert lock_env.local.optimus_lock_events_truncated is True


def test_wrap_records_query_keyword_with_postgres_identifiers(lock_env):
	import frappe

	lock_env.database.db_type = "postgres"
	lock_env.raises(_lock_error())
	with pytest.raises(frappe.QueryDeadlockError):
		lock_env.db.sql(query='UPDATE "tabX" SET a = 5')
	nq = lock_env.local.optimus_lock_events[0]["normalized_query"]
	assert '"tabX"' in nq and "5" not in nq


def test_rq_timeout_during_recording_still_escapes(lock_env, monkeypatch):
	from optimus import safe_call

	class JobTimeout(Exception):
		pass

	monkeypatch.setattr(safe_call, "job_timeout_types", lambda: (JobTimeout,))

	def interrupted():
		raise JobTimeout("rq timeout")

	monkeypatch.setattr(capture, "_lock_caller_frames", interrupted)
	lock_env.raises(_lock_error())
	with pytest.raises(JobTimeout):
		lock_env.db.sql("SELECT 1")
	assert lock_env.local._optimus_recording_lock is False


def test_recording_failure_is_logged_once(lock_env, monkeypatch):
	import frappe

	logged = []
	monkeypatch.setattr(
		frappe, "logger", lambda *a, **k: types.SimpleNamespace(warning=logged.append), raising=False
	)
	monkeypatch.setattr(capture, "_lock_capture_warned", False)
	monkeypatch.setattr(capture, "_lock_caller_frames", lambda: 1 / 0)
	lock_env.raises(_lock_error())
	for _ in range(3):
		with pytest.raises(frappe.QueryDeadlockError):
			lock_env.db.sql("SELECT 1")
	assert len(logged) == 1 and "ZeroDivisionError" in logged[0]


def test_uninstall_leaves_a_wrapper_stacked_on_top(lock_env):
	import functools

	ours = lock_env.database.sql

	@functools.wraps(ours)
	def outer(*a, **k):
		return ours(*a, **k)

	lock_env.database.sql = outer
	capture.uninstall_lock_capture()
	assert lock_env.database.sql is outer
	lock_env.database.sql = ours
	capture.uninstall_lock_capture()
	assert lock_env.database.sql is lock_env.original


# ----- Frappe's in-process retry of a deadlocked background job -------------------


@pytest.fixture
def rq_job(monkeypatch):
	"""A stand-in for the RQ job object, which survives frappe.destroy()."""
	job = types.SimpleNamespace(id="job-1")
	rq = types.ModuleType("rq")
	rq.get_current_job = lambda: job
	monkeypatch.setitem(sys.modules, "rq", rq)
	return job


def test_lock_events_are_mirrored_to_the_rq_job(lock_env, rq_job):
	import frappe

	lock_env.raises(_lock_error())
	with pytest.raises(frappe.QueryDeadlockError):
		lock_env.db.sql("UPDATE `tabX` SET a = 1")
	state = capture.job_retry_state(create=False)
	assert state["lock_events"] is lock_env.local.optimus_lock_events
	assert len(state["lock_events"]) == 1


def test_retry_state_keeps_the_cap_and_truncation(lock_env, rq_job):
	import frappe

	lock_env.raises(_lock_error())
	for _ in range(capture.LOCK_EVENTS_CAP_PER_RECORDING + 3):
		with pytest.raises(frappe.QueryDeadlockError):
			lock_env.db.sql("SELECT 1")
	state = capture.job_retry_state(create=False)
	assert len(state["lock_events"]) == capture.LOCK_EVENTS_CAP_PER_RECORDING
	assert state["lock_events_truncated"] is True


def test_job_retry_state_outside_a_job_is_none(monkeypatch):
	rq = types.ModuleType("rq")
	rq.get_current_job = lambda: None
	monkeypatch.setitem(sys.modules, "rq", rq)
	assert capture.job_retry_state() is None


def _before_job_env(monkeypatch):
	"""Stub the world around hooks_callbacks.before_job; returns (module, fresh frappe.local)."""
	import frappe

	import optimus
	from optimus import hooks_callbacks as hc
	from optimus import session as sess
	from optimus import settings

	local = _FakeLocal()
	monkeypatch.setattr(frappe, "local", local, raising=False)
	monkeypatch.setattr(frappe, "session", types.SimpleNamespace(user="a@b.c"), raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda **kw: None, raising=False)
	monkeypatch.setattr(frappe, "recorder", types.SimpleNamespace(record=lambda **kw: None), raising=False)
	monkeypatch.setattr(hc, "_should_skip_user", lambda user: False)
	monkeypatch.setattr(hc, "_track_bg_job_started", lambda *a: None)
	monkeypatch.setattr(sess, "get_active_session_for", lambda user: "SESS")
	monkeypatch.setattr(sess, "get_session_meta", lambda s: {"capture_python_tree": False})
	monkeypatch.setattr(optimus, "_try_install_capture_wraps", lambda: None)
	monkeypatch.setattr(settings, "is_enabled", lambda: True)
	return hc, local


def test_retried_job_regains_its_session_and_lock_events(monkeypatch, rq_job):
	hc, local = _before_job_env(monkeypatch)
	kwargs = {"_profiler_session_id": "SESS"}
	hc.before_job(method="m", kwargs=kwargs)
	assert local.optimus_session_id == "SESS"
	assert "_profiler_session_id" not in kwargs

	events = [_event()]
	state = capture.job_retry_state()
	state["lock_events"] = events
	state["lock_events_truncated"] = True

	# frappe.destroy() wipes frappe.local, then execute_job re-runs with the same kwargs.
	hc, local = _before_job_env(monkeypatch)
	hc.before_job(method="m", kwargs=kwargs)
	assert local.optimus_session_id == "SESS"
	assert local.optimus_lock_events is events
	assert local.optimus_lock_events_truncated is True


def test_job_without_marker_or_retry_state_stays_unprofiled(monkeypatch, rq_job):
	hc, local = _before_job_env(monkeypatch)
	hc.before_job(method="m", kwargs={})
	assert not hasattr(local, "optimus_session_id")


# ----- Redis dump / read / cleanup / clear of lock events ---------------------------


class _Cache:
	def __init__(self):
		self.store = {}
		self.ttls = {}

	def set_value(self, key, value, expires_in_sec=None, **kw):
		self.store[key] = value
		self.ttls[key] = expires_in_sec

	def get_value(self, key, **kw):
		return self.store.get(key)

	def delete_value(self, key):
		self.store.pop(key, None)

	def hget(self, name, key):
		return self.store.get((name, key))

	def hdel(self, name, key):
		self.store.pop((name, key), None)


@pytest.fixture
def redis_env(monkeypatch):
	import frappe

	cache, local = _Cache(), _FakeLocal()
	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	monkeypatch.setattr(frappe, "local", local, raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda **kw: None, raising=False)
	return types.SimpleNamespace(cache=cache, local=local)


def test_dump_stores_lock_events_with_ttl_and_clears_locals(redis_env):
	from optimus import hooks_callbacks as hc
	from optimus.session import SESSION_TTL_SECONDS

	redis_env.local.optimus_lock_events = [_event(), _event("lock_wait_timeout")]
	hc._dump_capture_state_to_redis(recording_uuid="rec-1")

	key = "profiler:lock_events:rec-1"
	assert redis_env.cache.store[key] == [_event(), _event("lock_wait_timeout")]
	assert redis_env.cache.ttls[key] == SESSION_TTL_SECONDS
	assert not hasattr(redis_env.local, "optimus_lock_events")


def test_dump_appends_truncation_marker(redis_env):
	from optimus import hooks_callbacks as hc

	redis_env.local.optimus_lock_events = [_event()]
	redis_env.local.optimus_lock_events_truncated = True
	hc._dump_capture_state_to_redis(recording_uuid="rec-1")

	stored = redis_env.cache.store["profiler:lock_events:rec-1"]
	assert stored == [_event(), {"_truncated": True}]
	assert not hasattr(redis_env.local, "optimus_lock_events_truncated")


def test_dump_without_events_writes_nothing(redis_env):
	from optimus import hooks_callbacks as hc

	hc._dump_capture_state_to_redis(recording_uuid="rec-1")
	assert "profiler:lock_events:rec-1" not in redis_env.cache.store


def test_dump_without_recording_still_clears_lock_locals(redis_env):
	from optimus import hooks_callbacks as hc

	redis_env.local.optimus_lock_events = [_event()]
	redis_env.local.optimus_lock_events_truncated = True
	hc._dump_capture_state_to_redis(recording_uuid=None)
	assert not hasattr(redis_env.local, "optimus_lock_events")
	assert not hasattr(redis_env.local, "optimus_lock_events_truncated")


def test_clear_capture_locals_clears_lock_event_locals(redis_env):
	from optimus import hooks_callbacks as hc

	redis_env.local.optimus_lock_events = [_event()]
	redis_env.local.optimus_lock_events_truncated = True
	hc._clear_capture_locals()
	assert not hasattr(redis_env.local, "optimus_lock_events")
	assert not hasattr(redis_env.local, "optimus_lock_events_truncated")


def test_force_stop_clears_lock_event_locals():
	local = _FakeLocal()
	local.optimus_lock_events = [_event()]
	local.optimus_lock_events_truncated = True
	capture._force_stop_inflight_capture(local_proxy=local)
	assert not hasattr(local, "optimus_lock_events")
	assert not hasattr(local, "optimus_lock_events_truncated")


def test_fetch_recordings_reads_lock_events(redis_env):
	from frappe.recorder import RECORDER_REQUEST_HASH

	from optimus import analyze

	redis_env.cache.store[(RECORDER_REQUEST_HASH, "rec-1")] = {"uuid": "rec-1", "calls": []}
	redis_env.cache.store[(RECORDER_REQUEST_HASH, "rec-2")] = {"uuid": "rec-2", "calls": []}
	redis_env.cache.store["profiler:lock_events:rec-1"] = [_event()]

	recs = list(analyze._fetch_recordings(["rec-1", "rec-2"]))
	assert recs[0]["lock_events"] == [_event()]
	assert recs[1]["lock_events"] == []


def test_cleanup_redis_deletes_lock_events(redis_env, monkeypatch):
	from optimus import analyze
	from optimus import session as ps_session

	monkeypatch.setattr(ps_session, "delete_session_state", lambda uuid: None)
	redis_env.cache.store["profiler:lock_events:rec-1"] = [_event()]
	analyze._cleanup_redis("sess", ["rec-1"])
	assert "profiler:lock_events:rec-1" not in redis_env.cache.store


# ----- summary ------------------------------------------------------------------


def test_summary_does_not_call_a_zero_impact_finding_the_biggest_issue():
	from optimus import analyze

	finding = lock_contention.analyze([{"lock_events": [_event()]}], AnalyzeContext(session_uuid="t", docname="t")).findings[0]
	finding["action_ref"] = None
	ctx = AnalyzeContext(session_uuid="t", docname="t")
	ctx.actions = [{"action_label": "GET x", "duration_ms": 50000, "recording_uuid": "r0"}]
	ctx.findings = [finding]
	assert "biggest issue" not in analyze._build_summary_html(ctx, total_queries=1, recordings=[])
