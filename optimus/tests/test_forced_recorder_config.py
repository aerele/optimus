# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Optimus's own forced recorder ignores the cached Frappe Recorder UI config.

Frappe caches the Recorder UI's settings for 10 minutes and ``Recorder.__init__`` reads
them even for ``record(force=True)``. ``RecorderConfig.retrieve`` returns Frappe's defaults
only while Optimus forces its recorder; the stored settings are never touched.
"""

import types
from dataclasses import dataclass

import pytest

import optimus
from optimus import analyze, capture, redis_keys
from optimus import hooks_callbacks as hc
from optimus.tests.test_analyze_fetch import FakeCache
from optimus.tests.test_hooks_request_job_flow import env  # noqa: F401

frappe = hc.frappe


@dataclass
class FakeConfig:
	record_requests: int = 1
	record_jobs: int = 1
	record_sql: int = 1
	profile: int = 0
	capture_stack: int = 1
	capture_doc_events: int = 1
	request_filter: str = "/"
	jobs_filter: str = ""
	cached = None

	@classmethod
	def retrieve(cls):
		return cls.cached or cls()


class FakeRecorder:
	"""The branch logic of Frappe 16's Recorder.__init__, reading the config the same way."""

	def __init__(self, force=False):
		self.config = FakeConfig.retrieve()
		self._recording = True
		self.force = force
		self.sql_patched = False
		self.profiler = None
		if (
			self.config.record_requests
			and frappe.request
			and self.config.request_filter in frappe.request.path
		):
			self.path = frappe.request.path
			self.event_type = "HTTP Request"
		elif self.config.record_jobs and frappe.job and self.config.jobs_filter in frappe.job.method:
			self.event_type = "Background Job"
			self.path = frappe.job.method
		elif not self.force:
			self._recording = False
			return
		else:
			self.event_type = "Function Call"
		self.uuid = "u1"
		self.sql_patched = bool(self.config.record_sql)
		self.profiler = object() if self.config.profile else None


def _fake_record(force=False):
	frappe.local._recorder = FakeRecorder(force=force)
	return frappe.local._recorder


@pytest.fixture
def rec_module(monkeypatch):
	import frappe.recorder as rec

	monkeypatch.setattr(rec, "RecorderConfig", FakeConfig, raising=False)
	monkeypatch.setattr(rec, "Recorder", FakeRecorder, raising=False)
	monkeypatch.setattr(rec, "record", _fake_record, raising=False)
	monkeypatch.setattr(FakeConfig, "retrieve", FakeConfig.__dict__["retrieve"])
	optimus._patch_recorder_config(rec)
	monkeypatch.setattr(frappe, "local", types.SimpleNamespace(), raising=False)
	monkeypatch.setattr(frappe, "request", None, raising=False)
	monkeypatch.setattr(frappe, "job", None, raising=False)
	yield rec
	FakeConfig.cached = None


NOISY = FakeConfig(
	record_requests=1,
	record_jobs=0,
	record_sql=0,
	profile=1,
	capture_stack=0,
	capture_doc_events=0,
	request_filter="/x",
)


class TestRetrievePatch:
	def test_stored_config_is_returned_outside_forcing(self, rec_module):
		FakeConfig.cached = NOISY
		assert FakeConfig.retrieve() is NOISY

	def test_defaults_are_returned_while_forcing(self, rec_module):
		FakeConfig.cached = NOISY
		frappe.local.optimus_forcing = True
		got = FakeConfig.retrieve()
		assert got is not NOISY
		assert (got.record_jobs, got.record_sql, got.profile, got.request_filter) == (1, 1, 0, "/")

	def test_stored_config_is_untouched_by_a_forced_record(self, rec_module, monkeypatch):
		FakeConfig.cached = NOISY
		monkeypatch.setattr(frappe, "job", types.SimpleNamespace(method="app.tasks.sync"), raising=False)
		capture.record_forced()
		assert FakeConfig.cached is NOISY
		assert (NOISY.record_jobs, NOISY.record_sql, NOISY.profile) == (0, 0, 1)

	def test_patch_is_idempotent(self, rec_module):
		first = FakeConfig.__dict__["retrieve"]
		optimus._patch_recorder_config(rec_module)
		assert FakeConfig.__dict__["retrieve"] is first

	def test_missing_config_class_is_a_noop(self):
		optimus._patch_recorder_config(types.SimpleNamespace())

	def test_config_without_retrieve_is_a_noop(self):
		cfg = type("Cfg", (), {})
		optimus._patch_recorder_config(types.SimpleNamespace(RecorderConfig=cfg))
		assert not hasattr(cfg, "retrieve")


class TestRecordForced:
	def test_job_with_jobs_off_in_the_cached_config_is_a_background_job(self, rec_module, monkeypatch):
		FakeConfig.cached = NOISY
		monkeypatch.setattr(frappe, "job", types.SimpleNamespace(method="app.tasks.sync"), raising=False)
		rec = capture.record_forced()
		assert (rec.event_type, rec.path) == ("Background Job", "app.tasks.sync")

	def test_request_with_a_non_matching_filter_is_an_http_request(self, rec_module, monkeypatch):
		FakeConfig.cached = NOISY
		monkeypatch.setattr(frappe, "request", types.SimpleNamespace(path="/api/method/ping"), raising=False)
		assert capture.record_forced().event_type == "HTTP Request"

	def test_without_the_override_the_old_failure_is_reproduced(self, rec_module, monkeypatch):
		FakeConfig.cached = NOISY
		monkeypatch.setattr(frappe, "job", types.SimpleNamespace(method="app.tasks.sync"), raising=False)
		rec = rec_module.record(force=True)
		assert rec.event_type == "Function Call"
		assert not hasattr(rec, "path")

	def test_flag_is_set_during_the_call_and_cleared_after(self, rec_module, monkeypatch):
		seen = []
		monkeypatch.setattr(
			rec_module, "record", lambda force=False: seen.append(frappe.local.optimus_forcing)
		)
		capture.record_forced()
		assert seen == [True]
		assert frappe.local.optimus_forcing is False

	def test_flag_is_cleared_when_record_raises(self, rec_module, monkeypatch):
		def boom(force=False):
			raise RuntimeError("x")

		monkeypatch.setattr(rec_module, "record", boom)
		with pytest.raises(RuntimeError):
			capture.record_forced()
		assert frappe.local.optimus_forcing is False

	def test_forces_the_recorder(self, rec_module, monkeypatch):
		calls = []
		monkeypatch.setattr(rec_module, "record", lambda **kw: calls.append(kw))
		capture.record_forced()
		assert calls == [{"force": True}]


class TestNonRecordingRecorderIsNotReused:
	def test_request_replaces_a_recorder_that_is_not_recording(self, env):  # noqa: F811
		env.store.active["alice@example.com"] = "s1"
		env.store.meta["s1"] = {}
		env.local._recorder = types.SimpleNamespace(_recording=False)

		hc.before_request()

		assert env.recorded == [{"force": True}]

	def test_request_still_reuses_a_recording_recorder(self, env):  # noqa: F811
		env.store.active["alice@example.com"] = "s1"
		env.store.meta["s1"] = {}
		env.local._recorder = types.SimpleNamespace(_recording=True, uuid="g")

		hc.before_request()

		assert env.recorded == []

	def test_job_replaces_a_recorder_that_is_not_recording(self, env):  # noqa: F811
		env.store.active["alice@example.com"] = "s1"
		env.local._recorder = types.SimpleNamespace(_recording=False)

		hc.before_job(method="app.tasks.sync", kwargs={"_profiler_session_id": "s1"})

		assert env.recorded == [{"force": True}]

	def test_job_still_reuses_a_recording_recorder(self, env):  # noqa: F811
		env.store.active["alice@example.com"] = "s1"
		env.local._recorder = types.SimpleNamespace(_recording=True, uuid="g")

		hc.before_job(method="app.tasks.sync", kwargs={"_profiler_session_id": "s1"})

		assert env.recorded == []


class TestWiring:
	def test_patch_recorder_installs_the_config_override(self, rec_module, monkeypatch):
		monkeypatch.setattr(FakeConfig, "retrieve", classmethod(lambda cls: cls.cached or cls()))
		optimus._patch_recorder()
		FakeConfig.cached = NOISY
		frappe.local.optimus_forcing = True
		assert FakeConfig.retrieve() is not NOISY

	def test_request_hook_forces_the_recorder_with_the_override_on(self, env, monkeypatch):  # noqa: F811
		seen = []
		monkeypatch.setattr(
			frappe.recorder, "record", lambda **kw: seen.append(env.local.optimus_forcing), raising=False
		)
		env.store.active["alice@example.com"] = "s1"
		env.store.meta["s1"] = {}

		hc.before_request()

		assert seen == [True]
		assert env.local.optimus_forcing is False

	def test_job_hook_forces_the_recorder_with_the_override_on(self, env, monkeypatch):  # noqa: F811
		seen = []
		monkeypatch.setattr(
			frappe.recorder, "record", lambda **kw: seen.append(env.local.optimus_forcing), raising=False
		)
		env.store.active["alice@example.com"] = "s1"

		hc.before_job(method="app.tasks.sync", kwargs={"_profiler_session_id": "s1"})

		assert seen == [True]


class TestStaleUiConfigDoesNotChangeTheForcedRecorder:
	def test_sql_is_recorded_and_no_profiler_runs_for_a_job(self, rec_module, monkeypatch):
		FakeConfig.cached = NOISY
		monkeypatch.setattr(frappe, "job", types.SimpleNamespace(method="app.tasks.sync"), raising=False)
		rec = capture.record_forced()
		assert rec.sql_patched is True
		assert rec.profiler is None

	def test_sql_is_recorded_and_no_profiler_runs_for_a_request(self, rec_module, monkeypatch):
		FakeConfig.cached = NOISY
		monkeypatch.setattr(frappe, "request", types.SimpleNamespace(path="/api/method/ping"), raising=False)
		rec = capture.record_forced()
		assert rec.sql_patched is True
		assert rec.profiler is None

	def test_without_the_override_sql_is_lost_and_a_profiler_runs(self, rec_module, monkeypatch):
		FakeConfig.cached = NOISY
		monkeypatch.setattr(frappe, "request", types.SimpleNamespace(path="/api/method/ping"), raising=False)
		rec = rec_module.record(force=True)
		assert rec.sql_patched is False
		assert rec.profiler is not None


class TestSqlOffNote:
	def _recorded(self, env, record_sql, uuid="rec-1"):  # noqa: F811
		env.local.optimus_session_id = "s1"
		env.local._recorder = types.SimpleNamespace(
			uuid=uuid, config=types.SimpleNamespace(record_sql=record_sql)
		)

	def test_request_on_a_recorder_with_sql_off_is_flagged(self, env):  # noqa: F811
		self._recorded(env, 0)
		hc.after_request(response=object())
		assert env.cache_writes[redis_keys.sql_off("rec-1")] is True

	def test_job_on_a_recorder_with_sql_off_is_flagged(self, env):  # noqa: F811
		self._recorded(env, 0)
		hc.after_job()
		assert env.cache_writes[redis_keys.sql_off("rec-1")] is True

	def test_sql_on_is_not_flagged(self, env):  # noqa: F811
		self._recorded(env, 1)
		hc.after_request(response=object())
		hc.after_job()
		assert redis_keys.sql_off("rec-1") not in env.cache_writes

	def test_recorder_without_a_config_is_not_flagged(self, env):  # noqa: F811
		env.local.optimus_session_id = "s1"
		env.local._recorder = types.SimpleNamespace(uuid="rec-1")
		hc.after_request(response=object())
		assert redis_keys.sql_off("rec-1") not in env.cache_writes

	def test_no_session_means_no_flag(self, env):  # noqa: F811
		env.local._recorder = types.SimpleNamespace(uuid="rec-1", config=types.SimpleNamespace(record_sql=0))
		hc.after_request(response=object())
		assert env.cache_writes == {}

	def test_analyze_warns_with_the_count_of_flagged_actions(self, monkeypatch):
		store = {redis_keys.sql_off("a"): True, redis_keys.sql_off("c"): True}
		monkeypatch.setattr(frappe, "cache", FakeCache(store), raising=False)
		warning = analyze._sql_off_warning([{"uuid": "a"}, {"uuid": "b"}, {"uuid": "c"}, {}])
		assert warning == (
			"2 actions were recorded while Frappe's Recorder was running with SQL recording "
			"turned off, so their queries are missing."
		)

	def test_analyze_warns_in_the_singular(self, monkeypatch):
		monkeypatch.setattr(frappe, "cache", FakeCache({redis_keys.sql_off("a"): True}), raising=False)
		assert analyze._sql_off_warning([{"uuid": "a"}]).startswith("1 action was recorded while")

	def test_analyze_adds_no_warning_when_nothing_is_flagged(self, monkeypatch):
		monkeypatch.setattr(frappe, "cache", FakeCache({}), raising=False)
		assert analyze._sql_off_warning([{"uuid": "a"}]) is None
