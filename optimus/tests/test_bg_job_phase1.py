# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Phase-1 completeness for background jobs: enqueue wrapper, attribution, status labels."""

import sys
import types

import pytest

from optimus.tests.test_enqueue_patch import _reset_profiler_modules, fake_frappe  # noqa: F401

SESSION = "sess-1"
USER_KEY = "profiler:active:alice@example.com"


@pytest.fixture
def env(fake_frappe):  # noqa: F811
	"""fake_frappe plus the bits phase-1 tracking needs (local, hashes, sets)."""
	fake, fake_bg, calls = fake_frappe
	fake.local = types.SimpleNamespace()
	fake.in_test = False
	fake.generate_hash = lambda length=10: "h" * length
	fake_bg.create_job_id = lambda job_id=None: f"site::{job_id}"
	store = fake.cache._store
	fake.cache.hset = lambda k, f, v: store.setdefault(k, {}).__setitem__(f, v)
	fake.cache.hget = lambda k, f: (store.get(k) or {}).get(f)
	fake.cache.hgetall = lambda k: dict(store.get(k) or {})
	fake.cache.srem = lambda k, *v: store.get(k, set()).difference_update(v)
	fake.cache.set_value(USER_KEY, SESSION)
	fake_utils = sys.modules["frappe.utils"]
	fake_utils.now_datetime = lambda: "2026-10-26 10:00:00"
	ret = {"job": {"id": None}}  # what the "original" enqueue returns

	def original(method, *args, **kwargs):
		calls.append({"method": method, "args": args, "kwargs": dict(kwargs)})
		return ret["job"]

	fake_bg.enqueue = original
	_reset_profiler_modules()
	import optimus  # noqa: F401 installs the wrapper

	return types.SimpleNamespace(fake=fake, bg=fake_bg, calls=calls, ret=ret, enqueue=fake_bg.enqueue)


def _pending(env):
	from optimus import session

	return session.get_pending_jobs(SESSION)


def _jobs(env):
	from optimus import session

	return {j["job_id"]: j for j in session.get_jobs(SESSION)}


class TestSynchronousEnqueue:
	def test_now_true_gets_no_marker_and_no_tracking(self, env):
		env.enqueue("m.f", now=True, x=1)
		kw = env.calls[0]["kwargs"]
		assert "_profiler_session_id" not in kw and "_lp_session_id" not in kw
		assert kw["x"] == 1
		assert _pending(env) == set()

	def test_is_async_false_outside_tests_is_synchronous(self, env):
		env.enqueue("m.f", is_async=False)
		assert "_profiler_session_id" not in env.calls[0]["kwargs"]

	def test_is_async_false_inside_tests_still_queues_with_marker(self, env):
		env.fake.in_test = True
		env.enqueue("m.f", is_async=False)
		assert env.calls[0]["kwargs"]["_profiler_session_id"] == SESSION

	def test_positional_now_gets_no_marker(self, env):
		env.enqueue("m.f", "default", None, None, True, None, True)
		assert "_profiler_session_id" not in env.calls[0]["kwargs"]
		assert _pending(env) == set()

	def test_positional_is_async_false_gets_no_marker(self, env):
		env.enqueue("m.f", "default", None, None, False)
		assert "_profiler_session_id" not in env.calls[0]["kwargs"]

	def test_async_enqueue_keeps_marker(self, env):
		env.enqueue("m.f")
		assert env.calls[0]["kwargs"]["_profiler_session_id"] == SESSION


class TestDeferredEnqueue:
	def test_after_commit_job_gets_known_id_and_is_tracked(self, env):
		env.enqueue("m.f", enqueue_after_commit=True)
		kw = env.calls[0]["kwargs"]
		assert kw["job_id"] == "h" * 20
		assert _pending(env) == {"site::" + "h" * 20}
		job = _jobs(env)["site::" + "h" * 20]
		assert job["method"] == "m.f" and job["status"] == "Queued"

	def test_caller_supplied_job_id_is_kept(self, env):
		env.enqueue("m.f", enqueue_after_commit=True, job_id="mine")
		assert env.calls[0]["kwargs"]["job_id"] == "mine"
		assert _pending(env) == {"site::mine"}

	def test_deduplicate_without_job_id_is_left_for_frappe_to_reject(self, env):
		env.enqueue("m.f", enqueue_after_commit=True, deduplicate=True)
		assert "job_id" not in env.calls[0]["kwargs"]

	def test_plain_enqueue_does_not_invent_a_job_id(self, env):
		env.enqueue("m.f")
		assert "job_id" not in env.calls[0]["kwargs"]


class TestNestedAndForeignJobs:
	def test_nested_job_in_drain_window_inherits_recording_session(self, env):
		env.fake.cache.delete_value(USER_KEY)  # Stop cleared the pointer
		env.fake.local.optimus_session_id = SESSION  # we are recording a job
		env.enqueue("m.nested")
		assert env.calls[0]["kwargs"]["_profiler_session_id"] == SESSION

	def test_child_of_orphaned_job_keeps_the_orphans_session(self, env):
		env.fake.local.optimus_bg_session_uuid = "old-session"  # the job's own session, not the user's new one
		env.enqueue("m.nested")
		assert env.calls[0]["kwargs"]["_profiler_session_id"] == "old-session"

	def test_no_session_no_marker(self, env):
		env.fake.cache.delete_value(USER_KEY)
		env.enqueue("m.scheduler_like")
		assert "_profiler_session_id" not in env.calls[0]["kwargs"]
		assert _pending(env) == set()

	def test_duplicate_skipped_by_frappe_is_not_tracked(self, env):
		env.ret["job"] = None  # frappe returns None for a deduplicated job
		env.enqueue("m.f", job_id="dup", deduplicate=True)
		assert _pending(env) == set()

	def test_same_job_tracked_twice_is_one_entry(self, env):
		from optimus import session

		session.register_pending_job(SESSION, "j1")
		session.register_pending_job(SESSION, "j1")
		assert _pending(env) == {"j1"}


class TestParentAttribution:
	def test_enqueue_records_parent_recording(self, env):
		env.fake.local._recorder = types.SimpleNamespace(uuid="rec-parent")
		env.ret["job"] = types.SimpleNamespace(id="j1")
		env.enqueue("m.f")
		assert _jobs(env)["j1"]["parent_recording_uuid"] == "rec-parent"

	def test_no_recorder_means_no_parent_field(self, env):
		env.ret["job"] = types.SimpleNamespace(id="j1")
		env.enqueue("m.f")
		assert "parent_recording_uuid" not in _jobs(env)["j1"]

	def test_queued_stamp_never_clobbers_a_fast_worker(self, env):
		from optimus import session

		session.set_job_status(SESSION, "j1", status="Running")
		session.record_enqueued_job(SESSION, "j1", "m.f", "rec-parent")
		assert _jobs(env)["j1"]["status"] == "Running"


@pytest.fixture
def fake_rq(monkeypatch):
	"""Minimal rq / rq.job / rq.exceptions so the tests run with or without real rq."""
	rq = types.ModuleType("rq")
	rq_job = types.ModuleType("rq.job")
	rq_exc = types.ModuleType("rq.exceptions")

	class NoSuchJobError(Exception):
		pass

	class Job:
		@staticmethod
		def fetch(job_id, connection=None):
			raise NoSuchJobError(job_id)

	rq_job.Job = Job
	rq_exc.NoSuchJobError = NoSuchJobError
	rq.job, rq.exceptions = rq_job, rq_exc
	for name, mod in (("rq", rq), ("rq.job", rq_job), ("rq.exceptions", rq_exc)):
		monkeypatch.setitem(sys.modules, name, mod)
	return types.SimpleNamespace(Job=Job, NoSuchJobError=NoSuchJobError)


class TestWaitCapLabels:
	@pytest.fixture
	def an(self, monkeypatch, fake_rq):
		from optimus import analyze

		self.set_calls = []
		monkeypatch.setattr(
			analyze.session, "set_job_status",
			lambda su, jid, **kw: self.set_calls.append((jid, kw)), raising=False,
		)
		return analyze

	def test_waiting_job_is_labelled_queued(self, an, monkeypatch):
		monkeypatch.setattr(an, "_rq_job_active", lambda j: True)
		monkeypatch.setattr(an, "_active_job_label", lambda j: "Queued")
		an._finalize_pending_statuses(SESSION, ["j1"])
		assert self.set_calls == [("j1", {"status": "Queued"})]

	def test_active_label_from_rq_status(self, an, fake_rq, monkeypatch):
		import frappe.utils.background_jobs as bj

		monkeypatch.setattr(bj, "get_redis_conn", lambda: object(), raising=False)
		for rq_status, label in (("queued", "Queued"), ("deferred", "Queued"), ("started", "Running")):
			job = types.SimpleNamespace(get_status=lambda refresh=True, s=rq_status: s)
			monkeypatch.setattr(fake_rq.Job, "fetch", staticmethod(lambda j, connection=None, jb=job: jb))
			assert an._active_job_label("j1") == label

	def test_vanished_queued_job_is_stopped_with_reason(self, an, monkeypatch):
		monkeypatch.setattr(an.session, "_read_job", lambda s, j: {"status": "Queued"}, raising=False)
		an._mark_vanished_job(SESSION, "j1")
		jid, kw = self.set_calls[0]
		assert kw["status"] == "Stopped" and "Never ran" in kw["error"]

	def test_vanished_job_keeps_terminal_status(self, an, monkeypatch):
		monkeypatch.setattr(an.session, "_read_job", lambda s, j: {"status": "Completed"}, raising=False)
		an._mark_vanished_job(SESSION, "j1")
		assert self.set_calls == []

	def test_fetch_miss_routes_to_vanished(self, an, fake_rq, monkeypatch):
		import frappe.utils.background_jobs as bj

		monkeypatch.setattr(bj, "get_redis_conn", lambda: object(), raising=False)

		def boom(j, connection=None):
			raise fake_rq.NoSuchJobError("gone")

		monkeypatch.setattr(fake_rq.Job, "fetch", staticmethod(boom))
		seen = []
		monkeypatch.setattr(an, "_mark_vanished_job", lambda s, j: seen.append(j))
		an._capture_job_terminal_status(SESSION, "j1")
		assert seen == ["j1"]


class TestSchedulerJobsAreNotCaptured:
	def test_before_job_without_marker_never_tracks_or_records(self, monkeypatch):
		import frappe

		from optimus import hooks_callbacks as hc

		local = types.SimpleNamespace()
		monkeypatch.setattr(frappe, "local", local, raising=False)
		tracked = []
		monkeypatch.setattr(hc, "_track_bg_job_started", lambda *a: tracked.append(a))
		hc.before_job(method="app.scheduled.daily", kwargs={"x": 1})
		assert tracked == []
		assert not hasattr(local, "optimus_session_id")


class TestTriggeredBy:
	def _actions(self):
		return [
			{"idx": 0, "event_type": "HTTP Request", "action_label": "POST: submit", "recording_uuid": "r0"},
			{"idx": 1, "event_type": "RQ Job", "action_label": "RQ Job: a.b.send", "recording_uuid": "r1",
				"path": "a.b.send"},
		]

	def test_job_points_at_requesting_action(self):
		from optimus.renderer._internal import build_background_jobs

		tracked = [
			{"recording_uuid": "r1", "status": "Completed", "parent_recording_uuid": "r0"},
			{"recording_uuid": "", "status": "Queued", "method": "a.b.late", "parent_recording_uuid": "r1"},
		]
		jobs = build_background_jobs(self._actions(), {}, tracked_jobs=tracked)["jobs"]
		by_method = {j["method"]: j for j in jobs}
		assert by_method["a.b.send"]["triggered_by"] == {"label": "POST: submit"}
		nested = by_method["a.b.late"]["triggered_by"]
		assert nested["label"].startswith("job ")

	def test_unknown_parent_is_none(self):
		from optimus.renderer._internal import build_background_jobs

		tracked = [{"recording_uuid": "r1", "status": "Completed", "parent_recording_uuid": "gone"}]
		job = build_background_jobs(self._actions(), {}, tracked_jobs=tracked)["jobs"][0]
		assert job["triggered_by"] is None

	def test_template_shows_triggered_by(self):
		import os

		path = os.path.join(os.path.dirname(__file__), "..", "templates", "report.html")
		src = open(path).read()
		assert "Triggered by" in src and "job.triggered_by" in src


class TestRecordingCap:
	def test_cap_hit_sets_warning_and_drops(self, monkeypatch):
		import frappe

		from optimus import session

		monkeypatch.setattr(frappe, "conf", {"optimus_max_recordings_per_session": 1}, raising=False)
		monkeypatch.setattr(session, "recording_count", lambda s: 1)
		meta = {}
		monkeypatch.setattr(session, "get_session_meta", lambda s: meta)
		monkeypatch.setattr(session, "set_session_meta", lambda s, m: meta.update(m))
		assert session.register_recording("s", "rec-job") is False
		assert "recording cap" in meta["cap_warning"]


class TestJobKeyTtl:
	def test_job_writes_bound_the_keys_lifetime(self, monkeypatch):
		import frappe

		from optimus import session

		expiries = {}
		cache = types.SimpleNamespace(
			store={},
			hset=lambda k, f, v: cache.store.setdefault(k, {}).__setitem__(f, v),
			hget=lambda k, f: (cache.store.get(k) or {}).get(f),
			sadd=lambda k, *v: None,
			expire_key=lambda k, ttl: expiries.__setitem__(k, ttl),
		)
		monkeypatch.setattr(frappe, "cache", cache, raising=False)
		session.set_job_status("s1", "j1", status="Completed")  # late write after cleanup
		session.register_pending_job("s1", "j2")
		assert set(expiries.values()) == {session.JOB_KEYS_TTL_SECONDS}
		assert len(expiries) == 2
