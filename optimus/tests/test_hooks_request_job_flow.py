# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Request and job hook callbacks: what is recorded, registered and cleaned up."""

import types

import pytest

from optimus import hooks_callbacks as hc

frappe = hc.frappe  # patch the module object hc really uses


class FakeStore:
	SESSION_TTL_SECONDS = 123

	def __init__(self):
		self.active = {}
		self.meta = {}
		self.draining = set()
		self.registered = []
		self.cap_hit = False
		self.cleared_jobs = []
		self.job_recordings = []

	def get_active_session_for(self, user):
		return self.active.get(user)

	def get_session_meta(self, uuid):
		return self.meta.get(uuid)

	def is_draining(self, uuid):
		return uuid in self.draining

	def register_recording(self, session_uuid, recording_uuid, user=None):
		self.registered.append((session_uuid, recording_uuid, user))
		return not self.cap_hit

	def clear_pending_job(self, session_uuid, job_id):
		self.cleared_jobs.append((session_uuid, job_id))

	def set_job_recording(self, session_uuid, job_id, recording_uuid):
		self.job_recordings.append((session_uuid, job_id, recording_uuid))


@pytest.fixture
def env(monkeypatch):
	import optimus
	from optimus import infra_capture, settings

	store = FakeStore()
	local = types.SimpleNamespace(form_dict={}, response=None)
	state = types.SimpleNamespace(
		store=store,
		local=local,
		errors=[],
		recorded=[],
		pyi_started=[],
		dumped=[],
		headers=[],
		cache_writes={},
		enabled=True,
		skip_users=[],
		bg_started=[],
		bg_finished=[],
	)

	monkeypatch.setattr(hc, "session", store)
	monkeypatch.setattr(frappe, "local", local, raising=False)
	monkeypatch.setattr(frappe, "session", types.SimpleNamespace(user="alice@example.com"), raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda *a, **kw: state.errors.append(kw.get("title")), raising=False)
	monkeypatch.setattr(
		frappe, "logger", lambda: types.SimpleNamespace(warning=lambda *a, **kw: None), raising=False
	)
	monkeypatch.setattr(
		frappe,
		"cache",
		types.SimpleNamespace(
			set_value=lambda key, value, expires_in_sec=None: state.cache_writes.__setitem__(key, value)
		),
		raising=False,
	)
	monkeypatch.setattr(frappe.recorder, "record", lambda **kw: state.recorded.append(kw), raising=False)

	monkeypatch.setattr(optimus, "_try_install_capture_wraps", lambda: None)
	monkeypatch.setattr(settings, "is_enabled", lambda: state.enabled)
	monkeypatch.setattr(
		settings,
		"get_config",
		lambda: types.SimpleNamespace(
			skip_users=state.skip_users,
			skip_request_paths=[],
			pyinstrument_sampler_interval_ms=2.5,
		),
	)
	monkeypatch.setattr(infra_capture, "snapshot", lambda: {"cpu": 1})
	monkeypatch.setattr(infra_capture, "diff", lambda a, b: {"diff": True})
	monkeypatch.setattr(hc._capture, "_start_pyi_session", lambda **kw: state.pyi_started.append(kw))
	monkeypatch.setattr(hc, "_dump_capture_state_to_redis", lambda recording_uuid=None: state.dumped.append(recording_uuid))
	monkeypatch.setattr(
		hc,
		"_inject_correlation_header",
		lambda uuid, response=None: state.headers.append((uuid, response)),
	)
	monkeypatch.setattr(hc, "_track_bg_job_started", lambda uuid, method: state.bg_started.append((uuid, method)))
	monkeypatch.setattr(hc, "_track_bg_job_finished", lambda su, jid: state.bg_finished.append((su, jid)))
	return state


class TestBeforeRequest:
	def test_user_without_active_session_is_not_recorded(self, env):
		hc.before_request()

		assert env.recorded == []
		assert not hasattr(env.local, "optimus_session_id")

	def test_active_session_activates_recorder_infra_and_pyinstrument(self, env):
		env.store.active["alice@example.com"] = "s1"
		env.store.meta["s1"] = {"capture_python_tree": True}

		hc.before_request()

		assert env.recorded == [{"force": True}]
		assert env.local.optimus_session_id == "s1"
		assert env.local.optimus_infra_start == {"cpu": 1}
		assert env.local._profiler_active_session_id == "s1"
		assert env.pyi_started[0]["interval_ms"] == 2.5

	def test_sql_only_session_skips_the_python_tree_capture(self, env):
		env.store.active["alice@example.com"] = "s1"
		env.store.meta["s1"] = {"capture_python_tree": False}

		hc.before_request()

		assert env.recorded == [{"force": True}]
		assert env.pyi_started == []
		assert not hasattr(env.local, "_profiler_active_session_id")

	@pytest.mark.parametrize("user", ["Guest", None, ""])
	def test_guest_and_anonymous_are_never_recorded(self, env, monkeypatch, user):
		env.store.active[user] = "s1"
		monkeypatch.setattr(frappe, "session", types.SimpleNamespace(user=user), raising=False)

		hc.before_request()

		assert env.recorded == []

	def test_master_kill_switch_wins_over_an_active_session(self, env):
		env.store.active["alice@example.com"] = "s1"
		env.enabled = False

		hc.before_request()

		assert env.recorded == []

	def test_requests_made_by_the_analyze_pipeline_are_not_recorded(self, env):
		env.store.active["alice@example.com"] = "s1"
		env.local.optimus_analyzing = True

		hc.before_request()

		assert env.recorded == []

	def test_skip_listed_user_is_not_recorded(self, env):
		env.store.active["alice@example.com"] = "s1"
		env.skip_users = ["alice@example.com"]

		hc.before_request()

		assert env.recorded == []

	def test_profiler_own_api_calls_are_not_recorded(self, env):
		env.store.active["alice@example.com"] = "s1"
		env.local.form_dict = {"cmd": "optimus.api.status"}

		hc.before_request()

		assert env.recorded == []

	def test_existing_standalone_recorder_is_reused_not_replaced(self, env):
		env.store.active["alice@example.com"] = "s1"
		existing = object()
		env.local._recorder = existing

		hc.before_request()

		assert env.local._recorder is existing
		assert env.recorded == []
		assert env.local.optimus_session_id == "s1"  # still tagged so after_request registers it

	def test_infra_snapshot_failure_is_logged_but_recording_still_starts(self, env, monkeypatch):
		from optimus import infra_capture

		def _boom():
			raise RuntimeError("psutil")

		monkeypatch.setattr(infra_capture, "snapshot", _boom)
		env.store.active["alice@example.com"] = "s1"

		hc.before_request()

		assert "optimus infra start snapshot" in env.errors
		assert env.pyi_started

	def test_a_profiler_bug_never_breaks_the_request(self, env, monkeypatch):
		monkeypatch.setattr(env.store, "get_active_session_for", lambda u: 1 / 0)

		hc.before_request()  # must not raise

		assert env.errors == ["optimus before_request"]


class TestAfterRequest:
	def _recorded_request(self, env, uuid="rec-1"):
		env.local.optimus_session_id = "s1"
		env.local._recorder = types.SimpleNamespace(uuid=uuid)
		env.local.optimus_infra_start = {"cpu": 0}

	def test_registers_recording_dumps_state_and_cleans_up(self, env):
		self._recorded_request(env)
		response = object()

		hc.after_request(response=response)

		assert env.store.registered == [("s1", "rec-1", "alice@example.com")]
		assert env.dumped == ["rec-1"]
		assert env.headers == [("rec-1", response)]
		assert not hasattr(env.local, "optimus_session_id")
		assert not hasattr(env.local, "optimus_infra_start")

	def test_writes_the_infra_diff_for_the_recording(self, env):
		from optimus import redis_keys

		self._recorded_request(env)

		hc.after_request()

		assert env.cache_writes[redis_keys.infra("rec-1")] == {"diff": True}

	def test_request_outside_a_session_registers_nothing_and_adds_no_header(self, env):
		env.local._recorder = types.SimpleNamespace(uuid="global-recorder")

		hc.after_request()

		assert env.store.registered == []
		assert env.headers == []

	def test_session_without_a_recorder_registers_nothing(self, env):
		env.local.optimus_session_id = "s1"

		hc.after_request()

		assert env.store.registered == []
		assert env.dumped == [None]

	def test_recorder_without_uuid_registers_nothing(self, env):
		env.local.optimus_session_id = "s1"
		env.local._recorder = types.SimpleNamespace(uuid=None)

		hc.after_request()

		assert env.store.registered == []

	def test_cap_hit_still_dumps_state_and_does_not_raise(self, env):
		self._recorded_request(env)
		env.store.cap_hit = True

		hc.after_request()

		assert env.errors == []
		assert env.dumped == ["rec-1"]

	def test_register_failure_is_logged_and_cleanup_still_runs(self, env, monkeypatch):
		self._recorded_request(env)
		monkeypatch.setattr(env.store, "register_recording", lambda *a, **kw: 1 / 0)

		hc.after_request()

		assert "optimus after_request" in env.errors
		assert env.dumped == ["rec-1"]
		assert not hasattr(env.local, "optimus_session_id")

	def test_header_injection_failure_is_contained(self, env, monkeypatch):
		self._recorded_request(env)

		def _boom(*a, **kw):
			raise RuntimeError("headers")

		monkeypatch.setattr(hc, "_inject_correlation_header", _boom)

		hc.after_request()

		assert "optimus header injection" in env.errors
		assert not hasattr(env.local, "optimus_session_id")

	def test_new_doc_name_from_the_response_is_stashed_for_analyze(self, env, monkeypatch):
		from optimus import redis_keys
		from optimus.renderer import doc_event_renderer

		monkeypatch.setattr(
			doc_event_renderer, "resolve_target_doc_from_response", lambda form, resp: "SO-0001"
		)
		self._recorded_request(env)

		hc.after_request()

		assert env.cache_writes[redis_keys.resolved_doc("rec-1")] == "SO-0001"


class TestBeforeJob:
	def _job(self, env, kwargs, method="app.tasks.sync"):
		env.store.active["alice@example.com"] = "s1"
		hc.before_job(method=method, kwargs=kwargs)

	def test_marker_is_popped_so_the_user_method_never_sees_it(self, env):
		kwargs = {"_profiler_session_id": "s1", "doc": "X"}

		self._job(env, kwargs)

		assert kwargs == {"doc": "X"}

	def test_active_session_job_is_recorded_and_tracked(self, env):
		self._job(env, {"_profiler_session_id": "s1"})

		assert env.recorded == [{"force": True}]
		assert env.local.optimus_session_id == "s1"
		assert env.bg_started == [("s1", "app.tasks.sync")]
		assert env.local.optimus_bg_session_uuid == "s1"
		assert env.pyi_started

	def test_job_without_marker_is_ignored(self, env):
		kwargs = {"doc": "X"}

		self._job(env, kwargs)

		assert env.recorded == []
		assert env.bg_started == []
		assert kwargs == {"doc": "X"}

	def test_disabled_profiler_still_strips_the_marker(self, env):
		env.enabled = False
		kwargs = {"_profiler_session_id": "s1"}

		self._job(env, kwargs)

		assert kwargs == {}
		assert env.recorded == []

	def test_none_kwargs_are_tolerated(self, env):
		hc.before_job(method="m", kwargs=None)

		assert env.recorded == []
		assert env.errors == []

	def test_non_dict_kwargs_are_logged_not_raised(self, env):
		hc.before_job(method="m", kwargs=["x"])

		assert env.errors == ["optimus before_job unexpected kwargs"]

	def test_the_analyze_job_itself_is_never_recorded(self, env):
		env.local.optimus_analyzing = True

		self._job(env, {"_profiler_session_id": "s1"})

		assert env.recorded == []

	def test_guest_job_is_tracked_but_not_recorded(self, env, monkeypatch):
		monkeypatch.setattr(frappe, "session", types.SimpleNamespace(user="Guest"), raising=False)

		self._job(env, {"_profiler_session_id": "s1"})

		assert env.bg_started == [("s1", "app.tasks.sync")]
		assert env.recorded == []

	def test_skip_listed_user_job_is_not_recorded(self, env):
		env.skip_users = ["alice@example.com"]

		self._job(env, {"_profiler_session_id": "s1"})

		assert env.recorded == []

	def test_job_of_a_superseded_session_is_dropped(self, env):
		env.store.active["alice@example.com"] = "newer-session"

		hc.before_job(method="m", kwargs={"_profiler_session_id": "old"})

		assert env.recorded == []

	def test_late_job_is_recorded_while_the_stopped_session_is_draining(self, env):
		env.store.draining.add("s1")  # user has no active session any more

		hc.before_job(method="m", kwargs={"_profiler_session_id": "s1"})

		assert env.recorded == [{"force": True}]

	def test_late_job_is_dropped_when_not_draining(self, env):
		hc.before_job(method="m", kwargs={"_profiler_session_id": "s1"})

		assert env.recorded == []

	def test_existing_recorder_is_reused(self, env):
		env.local._recorder = object()

		self._job(env, {"_profiler_session_id": "s1"})

		assert env.recorded == []
		assert env.local.optimus_session_id == "s1"

	def test_sql_only_session_skips_python_tree(self, env):
		env.store.meta["s1"] = {"capture_python_tree": False}

		self._job(env, {"_profiler_session_id": "s1"})

		assert env.recorded == [{"force": True}]
		assert env.pyi_started == []

	def test_infra_snapshot_failure_is_logged_but_job_recording_continues(self, env, monkeypatch):
		from optimus import infra_capture

		monkeypatch.setattr(infra_capture, "snapshot", lambda: 1 / 0)

		self._job(env, {"_profiler_session_id": "s1"})

		assert "optimus infra start snapshot (job)" in env.errors
		assert env.pyi_started

	def test_a_profiler_bug_never_breaks_the_job(self, env, monkeypatch):
		monkeypatch.setattr(env.store, "get_active_session_for", lambda u: 1 / 0)

		self._job(env, {"_profiler_session_id": "s1"})

		assert "optimus before_job" in env.errors


class TestAfterJob:
	@pytest.fixture
	def rq(self, monkeypatch):
		import sys

		holder = types.SimpleNamespace(job=types.SimpleNamespace(id="rq-1"))
		fake = types.ModuleType("rq")
		fake.get_current_job = lambda: holder.job
		monkeypatch.setitem(sys.modules, "rq", fake)
		return holder

	def _finished_job(self, env):
		env.local.optimus_session_id = "s1"
		env.local.optimus_bg_session_uuid = "s1"
		env.local.optimus_bg_job_start_mono = 1.0
		env.local._recorder = types.SimpleNamespace(uuid="rec-9")
		env.local.optimus_infra_start = {"cpu": 0}

	def test_registers_links_and_finalises_the_job(self, env, rq):
		from optimus import redis_keys

		self._finished_job(env)

		hc.after_job(method="m", kwargs={}, result=None)

		assert env.store.registered == [("s1", "rec-9", "alice@example.com")]
		assert env.store.cleared_jobs == [("s1", "rq-1")]
		assert env.store.job_recordings == [("s1", "rq-1", "rec-9")]
		assert env.bg_finished == [("s1", "rq-1")]
		assert env.cache_writes[redis_keys.infra("rec-9")] == {"diff": True}
		assert env.dumped == ["rec-9"]

	def test_all_per_job_markers_are_cleared(self, env, rq):
		self._finished_job(env)

		hc.after_job()

		for marker in (
			"optimus_session_id",
			"optimus_bg_session_uuid",
			"optimus_bg_job_start_mono",
			"optimus_infra_start",
		):
			assert not hasattr(env.local, marker)

	def test_orphan_job_still_gets_a_terminal_status_via_the_bg_stash(self, env, rq):
		env.local.optimus_bg_session_uuid = "s1"

		hc.after_job()

		assert env.store.registered == []
		assert env.bg_finished == [("s1", "rq-1")]
		assert env.store.job_recordings == []  # no recording was produced

	def test_cap_hit_does_not_raise(self, env, rq):
		self._finished_job(env)
		env.store.cap_hit = True

		hc.after_job()

		assert env.errors == []

	def test_register_failure_is_logged_and_cleanup_still_runs(self, env, rq, monkeypatch):
		self._finished_job(env)
		monkeypatch.setattr(env.store, "register_recording", lambda *a, **kw: 1 / 0)

		hc.after_job()

		assert "optimus after_job" in env.errors
		assert env.bg_finished == [("s1", "rq-1")]
		assert not hasattr(env.local, "optimus_session_id")

	def test_missing_rq_job_skips_pending_bookkeeping_without_error(self, env, rq):
		rq.job = None
		self._finished_job(env)

		hc.after_job()

		assert env.store.cleared_jobs == []
		assert env.errors == []

	def test_rq_failure_never_breaks_the_job(self, env, rq, monkeypatch):
		import sys

		def _boom():
			raise RuntimeError("redis")

		monkeypatch.setattr(sys.modules["rq"], "get_current_job", _boom)
		self._finished_job(env)

		hc.after_job()

		assert not hasattr(env.local, "optimus_session_id")
