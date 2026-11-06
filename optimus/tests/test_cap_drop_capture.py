# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""A recording dropped at the per-session cap leaves nothing behind in Redis or on the thread.

Frappe's Recorder entries have no TTL and sit in persistent_cache_keys, and a
pyinstrument profiler left running makes every later request on the thread fail
to start its own ("There is already a profiler running").
"""

import types

import pytest
from frappe.recorder import RECORDER_REQUEST_HASH, RECORDER_REQUEST_SPARSE_HASH

from optimus import hooks_callbacks as hc
from optimus import infra_capture, redis_keys

frappe = hc.frappe


class _Cache:
	def __init__(self):
		self.writes = {}
		self.hdels = []

	def set_value(self, key, value, expires_in_sec=None, **kw):
		self.writes[key] = value

	def hdel(self, name, key):
		self.hdels.append((name, key))


class _Prof:
	def __init__(self):
		self.stopped = 0
		self.last_session = {"frames": 1}

	def stop(self):
		self.stopped += 1


class _Store:
	def __init__(self, cap_hit):
		self.cap_hit = cap_hit
		self.job_recordings = []

	def register_recording(self, *a, **kw):
		return not self.cap_hit

	def clear_pending_job(self, *a):
		pass

	def set_job_recording(self, *a):
		self.job_recordings.append(a)


@pytest.fixture
def make_env(monkeypatch):
	def _make(cap_hit):
		cache = _Cache()
		recorder = types.SimpleNamespace(uuid="rec-1", _recording=True, cleaned=0)
		recorder.cleanup = lambda: setattr(recorder, "cleaned", recorder.cleaned + 1)
		prof = _Prof()
		local = types.SimpleNamespace(
			form_dict={},
			response=None,
			_recorder=recorder,
			optimus_session_id="s1",
			optimus_pyinstrument=prof,
			optimus_sidecar=[{"a": 1}],
			optimus_infra_start={"cpu": 0},
			_profiler_active_session_id="s1",
		)
		store = _Store(cap_hit)
		headers = []
		monkeypatch.setattr(frappe, "cache", cache, raising=False)
		monkeypatch.setattr(frappe, "local", local, raising=False)
		monkeypatch.setattr(frappe, "session", types.SimpleNamespace(user="a@b.c"), raising=False)
		monkeypatch.setattr(frappe, "log_error", lambda *a, **kw: None, raising=False)
		monkeypatch.setattr(
			frappe,
			"logger",
			lambda *a, **kw: types.SimpleNamespace(warning=lambda m: None, debug=lambda m: None),
			raising=False,
		)
		monkeypatch.setattr(hc, "session", store)
		monkeypatch.setattr(hc, "_inject_correlation_header", lambda *a, **kw: headers.append(a))
		monkeypatch.setattr(infra_capture, "snapshot", lambda: {"cpu": 1})
		monkeypatch.setattr(infra_capture, "diff", lambda a, b: {"diff": True})
		monkeypatch.setattr(hc, "_track_bg_job_finished", lambda *a: None)
		return types.SimpleNamespace(
			cache=cache, local=local, recorder=recorder, prof=prof, store=store, headers=headers
		)

	return _make


def _nothing_written_for_the_recording(env):
	assert env.cache.writes == {}
	assert env.headers == []
	assert env.store.job_recordings == []


def test_an_over_cap_request_we_recorded_keeps_the_recorder_from_dumping(make_env):
	env = make_env(cap_hit=True)
	env.local.optimus_owns_recorder = True

	hc.after_request()

	assert env.recorder._recording is False
	_nothing_written_for_the_recording(env)


def test_an_over_cap_request_stops_the_running_profiler_and_clears_the_thread(make_env):
	env = make_env(cap_hit=True)
	env.local.optimus_owns_recorder = True

	hc.after_request()

	assert env.prof.stopped == 1
	for attr in (
		"optimus_pyinstrument",
		"optimus_sidecar",
		"_profiler_active_session_id",
		"optimus_infra_start",
		"optimus_capture_dropped",
		"optimus_owns_recorder",
	):
		assert not hasattr(env.local, attr), attr


def test_an_over_cap_request_has_nothing_to_purge_because_the_http_dump_comes_later(make_env):
	env = make_env(cap_hit=True)
	env.local.optimus_owns_recorder = True

	hc.after_request()

	assert env.cache.hdels == []


def test_an_over_cap_job_deletes_the_entry_the_recorder_already_dumped(make_env):
	env = make_env(cap_hit=True)
	env.local.optimus_owns_recorder = True

	hc.after_job()

	assert sorted(env.cache.hdels) == sorted(
		[(RECORDER_REQUEST_HASH, "rec-1"), (RECORDER_REQUEST_SPARSE_HASH, "rec-1")]
	)
	assert env.prof.stopped == 1
	_nothing_written_for_the_recording(env)


def test_a_request_that_borrowed_frappes_recorder_is_never_suppressed_or_purged(make_env):
	env = make_env(cap_hit=True)  # no optimus_owns_recorder: the Recorder UI started it

	hc.after_request()
	hc.after_job()

	assert env.recorder._recording is True
	assert env.cache.hdels == []


def test_a_recording_under_the_cap_is_dumped_as_before(make_env):
	env = make_env(cap_hit=False)
	env.local.optimus_owns_recorder = True

	hc.after_request()

	assert env.recorder._recording is True
	assert redis_keys.tree("rec-1") in env.cache.writes
	assert redis_keys.infra("rec-1") in env.cache.writes
	assert env.headers
	assert not hasattr(env.local, "optimus_owns_recorder")


def test_the_recorder_is_marked_ours_only_when_we_started_it(monkeypatch):
	import optimus
	from optimus import settings

	store = types.SimpleNamespace(
		get_active_session_for=lambda user: "s1",
		get_session_meta=lambda uuid: {"capture_python_tree": False},
		is_draining=lambda uuid: False,
	)
	monkeypatch.setattr(hc, "session", store)
	monkeypatch.setattr(optimus, "_try_install_capture_wraps", lambda: None)
	monkeypatch.setattr(settings, "is_enabled", lambda: True)
	monkeypatch.setattr(
		settings,
		"get_config",
		lambda: types.SimpleNamespace(
			skip_users=[], skip_request_paths=[], pyinstrument_sampler_interval_ms=2.5
		),
	)
	monkeypatch.setattr(infra_capture, "snapshot", lambda: {})
	monkeypatch.setattr(hc, "_track_bg_job_started", lambda *a: None)
	monkeypatch.setattr(frappe, "session", types.SimpleNamespace(user="a@b.c"), raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda *a, **kw: None, raising=False)
	monkeypatch.setattr(frappe.recorder, "record", lambda **kw: None, raising=False)

	for hook, args in (
		(hc.before_request, {}),
		(hc.before_job, {"method": "m", "kwargs": {"_profiler_session_id": "s1"}}),
	):
		ours = types.SimpleNamespace(form_dict={})
		monkeypatch.setattr(frappe, "local", ours, raising=False)
		hook(**args)
		assert ours.optimus_owns_recorder is True

		borrowed = types.SimpleNamespace(form_dict={}, _recorder=object())
		monkeypatch.setattr(frappe, "local", borrowed, raising=False)
		hook(**args)
		assert not hasattr(borrowed, "optimus_owns_recorder")


def test_an_over_cap_request_releases_the_recorders_cprofile_and_sql_patch(make_env):
	import cProfile

	env = make_env(cap_hit=True)
	env.local.optimus_owns_recorder = True
	profile = cProfile.Profile()
	profile.enable()
	env.recorder.cleanup = profile.disable  # what Recorder.cleanup does with profile=True

	hc.after_request()

	assert env.recorder._recording is False
	next_profile = cProfile.Profile()
	next_profile.enable()  # raises "Another profiling tool is already active" if still held
	next_profile.disable()


def test_an_over_cap_job_cleans_up_the_recorder_too(make_env):
	env = make_env(cap_hit=True)
	env.local.optimus_owns_recorder = True

	hc.after_job()

	assert env.recorder.cleaned == 1


def test_a_borrowed_recorder_is_not_cleaned_up(make_env):
	env = make_env(cap_hit=True)

	hc.after_request()

	assert env.recorder.cleaned == 0
