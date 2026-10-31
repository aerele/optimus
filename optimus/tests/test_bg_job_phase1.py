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
def env(fake_frappe, monkeypatch):  # noqa: F811
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
	lp = types.ModuleType("optimus.line_profile.capture")
	lp.is_active = lambda user: "lp-run-1"  # phase 2 is armed for the user
	monkeypatch.setitem(sys.modules, "optimus.line_profile.capture", lp)
	pkg = types.ModuleType("optimus.line_profile")
	pkg.capture = lp
	monkeypatch.setitem(sys.modules, "optimus.line_profile", pkg)
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

	def test_sync_calls_never_get_the_line_profile_marker(self, env):
		env.enqueue("m.f", now=True)
		env.enqueue("m.f", "default", None, None, True, None, True)
		env.enqueue("m.f", is_async=False)
		assert all("_lp_session_id" not in c["kwargs"] for c in env.calls)

	def test_async_calls_get_the_line_profile_marker(self, env):
		env.enqueue("m.f")
		assert env.calls[0]["kwargs"]["_lp_session_id"] == "lp-run-1"

	def test_legacy_async_keyword_is_synchronous(self, env):
		env.enqueue("m.f", **{"async": False})
		kw = env.calls[0]["kwargs"]
		assert "_profiler_session_id" not in kw and "_lp_session_id" not in kw

	def test_legacy_async_true_still_queues(self, env):
		env.enqueue("m.f", **{"async": True})
		assert env.calls[0]["kwargs"]["_profiler_session_id"] == SESSION


class TestFlagPositions:
	def test_positions_follow_the_real_signature(self):
		from optimus import _enqueue_positions

		def enqueue(method, queue="default", now=False, is_async=True, enqueue_after_commit=False, **kw):
			pass

		assert _enqueue_positions(enqueue) == {"now": 1, "is_async": 2, "enqueue_after_commit": 3}

	def test_unreadable_signature_falls_back_to_defaults(self):
		from optimus import _ENQUEUE_POSITIONS, _enqueue_positions

		assert _enqueue_positions(object()) == _ENQUEUE_POSITIONS

	def test_default_table_matches_frappe_when_installed(self):
		import inspect

		try:
			import frappe.utils.background_jobs as bg
		except ImportError:
			pytest.skip("frappe is not installed")
		if not hasattr(bg.enqueue, "__code__"):
			pytest.skip("frappe is stubbed")

		from optimus import _ENQUEUE_POSITIONS, _enqueue_positions

		original = getattr(bg.enqueue, "__wrapped__", bg.enqueue)
		assert _enqueue_positions(original) == _ENQUEUE_POSITIONS, inspect.signature(original)


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

	def test_positional_after_commit_is_tracked(self, env):
		env.enqueue("m.f", "default", None, None, True, None, False, True)
		assert env.calls[0]["kwargs"]["job_id"] == "h" * 20
		assert _pending(env) == {"site::" + "h" * 20}

	def test_deduplicated_after_commit_job_with_job_id_is_not_tracked(self, env):
		env.enqueue("m.f", enqueue_after_commit=True, job_id="dup", deduplicate=True)
		assert env.calls[0]["kwargs"]["job_id"] == "dup"
		assert _pending(env) == set()

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
		from optimus import session

		session.set_session_meta("old-session", {"user": "alice@example.com", "draining_until": 9e12})
		env.fake.local.optimus_bg_session_uuid = "old-session"  # draining, not the user's new one
		env.enqueue("m.nested")
		assert env.calls[0]["kwargs"]["_profiler_session_id"] == "old-session"

	def test_child_of_finished_session_does_not_resurrect_it(self, env):
		env.fake.local.optimus_bg_session_uuid = "gone-session"  # meta already deleted by cleanup
		env.enqueue("m.nested")
		assert env.calls[0]["kwargs"]["_profiler_session_id"] == SESSION  # the user's live one
		assert "gone-session" not in str(_pending(env))

	def test_child_of_finished_session_without_active_user_session_is_not_tracked(self, env):
		env.fake.cache.delete_value(USER_KEY)
		env.fake.local.optimus_bg_session_uuid = "gone-session"
		env.enqueue("m.nested")
		assert "_profiler_session_id" not in env.calls[0]["kwargs"]

	def test_child_of_expired_drain_window_is_not_inherited(self, env):
		from optimus import session

		env.fake.cache.delete_value(USER_KEY)
		session.set_session_meta("old-session", {"draining_until": 1.0})
		env.fake.local.optimus_bg_session_uuid = "old-session"
		env.enqueue("m.nested")
		assert "_profiler_session_id" not in env.calls[0]["kwargs"]

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
		monkeypatch.setattr(an, "_rq_status", lambda j: "queued")
		an._finalize_pending_statuses(SESSION, ["j1"])
		assert self.set_calls == [("j1", {"status": "Queued"})]

	def test_active_label_from_rq_status(self, an, fake_rq, monkeypatch):
		import frappe.utils.background_jobs as bj

		monkeypatch.setattr(bj, "get_redis_conn", lambda: object(), raising=False)
		for rq_status, label in (("queued", "Queued"), ("deferred", "Queued"), ("started", "Running")):
			job = types.SimpleNamespace(get_status=lambda refresh=True, s=rq_status: s)
			monkeypatch.setattr(fake_rq.Job, "fetch", staticmethod(lambda j, connection=None, jb=job: jb))
			assert an._active_job_label(an._rq_status("j1")) == label

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

	def test_template_explains_unfinished_jobs(self):
		import os

		path = os.path.join(os.path.dirname(__file__), "..", "templates", "report.html")
		src = open(path).read()
		assert 'job.status in ("Queued", "Running")' in src and "when this report was prepared" in src


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


class TestAiJobEnqueues:
	"""The AI refresh job enqueues must not be marked or tracked by the profiler."""

	def _ai_enqueue(self, enqueue):
		enqueue(
			"optimus.ai_jobs.run_slice",
			queue="long",
			is_async=True,
			enqueue_after_commit=True,
			deduplicate=True,
			job_id="optimus-ai-r1-1",
			run_id="r1",
			slice_no=1,
		)

	def test_unwrapped_enqueue_is_untouched_and_untracked(self, env):
		assert env.enqueue.__wrapped__ is not env.enqueue
		self._ai_enqueue(env.enqueue.__wrapped__)
		kw = env.calls[0]["kwargs"]
		assert "_profiler_session_id" not in kw and "_lp_session_id" not in kw
		assert kw["job_id"] == "optimus-ai-r1-1"
		assert _pending(env) == set()

	def test_without_session_ai_enqueue_is_not_marked_or_tracked(self, env):
		env.fake.cache.delete_value(USER_KEY)
		self._ai_enqueue(env.enqueue)
		kw = env.calls[0]["kwargs"]
		assert "_profiler_session_id" not in kw
		assert kw["job_id"] == "optimus-ai-r1-1"
		assert _pending(env) == set()


class TestAfterCommitGrace:
	"""An after-commit job has no RQ record until its transaction commits."""

	@pytest.fixture
	def an(self, monkeypatch, fake_rq):
		import frappe.utils as fu
		import frappe.utils.background_jobs as bj

		from optimus import analyze

		self.now = "2026-10-26 10:01:00"
		self.meta = {"status": "Queued", "enqueued_at": "2026-10-26 10:00:30"}
		self.set_calls = []
		monkeypatch.setattr(bj, "get_redis_conn", lambda: object(), raising=False)
		monkeypatch.setattr(fu, "now_datetime", lambda: self.parse(self.now), raising=False)
		monkeypatch.setattr(fu, "get_datetime", lambda v: self.parse(v), raising=False)
		monkeypatch.setattr(analyze.session, "_read_job", lambda s, j: self.meta, raising=False)
		monkeypatch.setattr(
			analyze.session, "set_job_status",
			lambda su, jid, **kw: self.set_calls.append((jid, kw)), raising=False,
		)
		return analyze

	@staticmethod
	def parse(value):
		import datetime

		return datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")

	def test_unknown_recent_job_is_in_flight_and_labelled_queued(self, an):
		assert an._job_in_flight(SESSION, "j1") is True
		an._finalize_pending_statuses(SESSION, ["j1"])
		assert self.set_calls == [("j1", {"status": "Queued"})]

	def test_unknown_old_job_is_vanished_and_stopped(self, an):
		self.now = "2026-10-26 10:10:00"
		assert an._job_in_flight(SESSION, "j1") is False
		an._finalize_pending_statuses(SESSION, ["j1"])
		jid, kw = self.set_calls[0]
		assert kw["status"] == "Stopped" and "Never ran" in kw["error"]

	def test_recent_unknown_job_is_not_stopped_by_the_status_capture(self, an):
		an._capture_job_terminal_status(SESSION, "j1")
		assert self.set_calls == []

	def test_known_terminal_job_is_not_held_by_the_grace(self, an, fake_rq, monkeypatch):
		job = types.SimpleNamespace(get_status=lambda refresh=True: "finished")
		monkeypatch.setattr(fake_rq.Job, "fetch", staticmethod(lambda j, connection=None: job))
		assert an._job_in_flight(SESSION, "j1") is False

	def test_job_is_fetched_once_per_pending_check(self, an, fake_rq, monkeypatch):
		calls = []
		job = types.SimpleNamespace(get_status=lambda refresh=True: "started")
		monkeypatch.setattr(
			fake_rq.Job, "fetch", staticmethod(lambda j, connection=None: calls.append(j) or job)
		)
		an._finalize_pending_statuses(SESSION, ["j1"])
		assert calls == ["j1"] and self.set_calls == [("j1", {"status": "Running"})]

	def test_started_job_has_no_grace(self, an):
		self.meta = {"status": "Running", "enqueued_at": "2026-10-26 10:00:55"}
		assert an._awaiting_commit(SESSION, "j1") is False


class TestBackgroundJobRowsPersist:
	"""The seam that writes tracked jobs into the Optimus Session child table."""

	class Doc:
		def __init__(self):
			self.rows = []

		def set(self, field, value):
			self.rows = list(value)

		def append(self, field, row):
			self.rows.append(row)

	@pytest.fixture
	def an(self, monkeypatch):
		from optimus import analyze

		self.jobs = []
		self.captured = []
		monkeypatch.setattr(analyze.session, "get_jobs", lambda su: self.jobs, raising=False)
		monkeypatch.setattr(analyze, "_capture_job_terminal_status", lambda su, j: self.captured.append(j))
		return analyze

	def test_queued_job_gets_its_terminal_status_recaptured(self, an):
		self.jobs = [
			{"job_id": "j1", "status": "Queued"},
			{"job_id": "j2", "status": ""},
			{"job_id": "j3", "status": "Completed"},
			{"job_id": "j4", "status": "Running"},
		]
		an._set_background_job_rows(self.Doc(), SESSION)
		assert self.captured == ["j1", "j2"]

	def test_job_without_status_is_stored_queued_not_running(self, an):
		self.jobs = [{"job_id": "j1", "method": "m.f"}]
		doc = self.Doc()
		an._set_background_job_rows(doc, SESSION)
		assert doc.rows[0]["status"] == "Queued"

	def test_parent_recording_uuid_lands_in_the_row(self, an):
		self.jobs = [{"job_id": "j1", "status": "Completed", "parent_recording_uuid": "rec-parent"}]
		doc = self.Doc()
		an._set_background_job_rows(doc, SESSION)
		assert doc.rows[0]["parent_recording_uuid"] == "rec-parent"
		assert doc.rows[0]["duration_ms"] == 0


class TestJobKeyTtlIsSetOnce:
	def test_existing_expiry_is_not_refreshed_by_a_job_chain(self, monkeypatch):
		import frappe

		from optimus import session

		ttls = {"fresh": -1, "aging": 500, "gone": -2}
		expired = []
		redis = types.SimpleNamespace(ttl=lambda k: ttls[k], expire=lambda k, t: expired.append((k, t)))
		monkeypatch.setattr(session, "_raw_redis", lambda: redis)
		monkeypatch.setattr(frappe, "cache", types.SimpleNamespace(make_key=lambda k: k), raising=False)
		for key in ("fresh", "aging", "gone"):
			session._bound_ttl(key)
		assert expired == [("fresh", session.JOB_KEYS_TTL_SECONDS)]
