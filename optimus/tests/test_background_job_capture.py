# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the background-job capture path: track the jobs a profiled flow
enqueued and wait for them to finish before analyzing.

  * `frappe.enqueue` monkey-patch registers the RQ job id with the session
  * `session.py` pending-jobs set + post-Stop "draining" window helpers
  * `analyze._bg_wait_for_pending_jobs` (re-enqueue-poll wait, capped, no-op
    when nothing's pending / the wait is disabled / no async worker)
  * the `background_job_wait_seconds` setting (default + clamp)
"""

import sys
import types
from unittest.mock import patch

import pytest

# --------------------------------------------------------------------------
# frappe.enqueue monkey-patch registers the RQ job id with the session
# --------------------------------------------------------------------------

def _reset_profiler_modules():
	for name in list(sys.modules):
		if name == "optimus" or name.startswith("optimus."):
			del sys.modules[name]


class _FakeCache:
	def __init__(self):
		self._store = {}

	def get_value(self, key, **kw):
		return self._store.get(key)

	def set_value(self, key, val, **kw):
		self._store[key] = val

	def delete_value(self, key):
		self._store.pop(key, None)

	def sadd(self, key, *values):
		self._store.setdefault(key, set()).update(values)

	def srem(self, key, *values):
		s = self._store.get(key)
		if isinstance(s, set):
			for v in values:
				s.discard(v)

	def smembers(self, key):
		return self._store.get(key, set())


@pytest.fixture
def fake_frappe(monkeypatch):
	fake = types.ModuleType("frappe")
	fake.session = types.SimpleNamespace(user="alice@example.com")
	fake.conf = {}
	fake.get_roles = lambda user=None: ["Optimus User"]
	fake.cache = _FakeCache()

	fake_utils = types.ModuleType("frappe.utils")
	fake_bg = types.ModuleType("frappe.utils.background_jobs")
	calls = []

	def original_enqueue(method, *args, **kwargs):
		calls.append({"method": method, "kwargs": dict(kwargs)})
		# Mimic frappe.enqueue: returns the RQ Job, or None for `now=True`.
		if kwargs.get("now"):
			return None
		return types.SimpleNamespace(id=f"job-{len(calls)}")

	fake_bg.enqueue = original_enqueue

	monkeypatch.setitem(sys.modules, "frappe", fake)
	monkeypatch.setitem(sys.modules, "frappe.utils", fake_utils)
	monkeypatch.setitem(sys.modules, "frappe.utils.background_jobs", fake_bg)
	yield fake, fake_bg, calls
	# The tests re-import optimus.* bound to the FAKE frappe; drop
	# them again so the next test re-imports against the real one (monkeypatch
	# restores sys.modules['frappe'] for us, but not our re-imports).
	_reset_profiler_modules()


def _pending_key(uuid):
	return f"profiler:session:{uuid}:pending_jobs"


class TestEnqueueRegistersPendingJob:
	def test_registers_job_id_with_active_session(self, fake_frappe):
		fake, fake_bg, calls = fake_frappe
		fake.cache.set_value("profiler:active:alice@example.com", "sess-1")
		_reset_profiler_modules()
		import optimus  # noqa: F401 triggers the patch

		fake_bg.enqueue("myapp.tasks.go", x=1)
		assert calls[-1]["kwargs"]["_profiler_session_id"] == "sess-1"
		assert fake.cache.smembers(_pending_key("sess-1")) == {"job-1"}

	def test_analyze_job_never_carries_a_session_marker(self, fake_frappe):
		fake, fake_bg, calls = fake_frappe
		# The user stopped A and started B: analyze for A must not be tagged B.
		fake.cache.set_value("profiler:active:alice@example.com", "sess-B")
		_reset_profiler_modules()
		import optimus  # noqa: F401

		fake_bg.enqueue("optimus.analyze.run", queue="long", session_uuid="sess-A")

		assert "_profiler_session_id" not in calls[-1]["kwargs"]
		assert "_lp_session_id" not in calls[-1]["kwargs"]
		assert fake.cache.smembers(_pending_key("sess-B")) == set()

	def test_analyze_job_given_as_a_callable_is_skipped_too(self, fake_frappe):
		fake, fake_bg, calls = fake_frappe
		fake.cache.set_value("profiler:active:alice@example.com", "sess-B")
		_reset_profiler_modules()
		import optimus  # noqa: F401
		from optimus import analyze

		fake_bg.enqueue(analyze.run, session_uuid="sess-A")

		assert "_profiler_session_id" not in calls[-1]["kwargs"]


# --------------------------------------------------------------------------
# session.py pending jobs + draining window
# --------------------------------------------------------------------------

@pytest.fixture
def session_with_fake_cache(monkeypatch):
	import frappe

	from optimus import session

	cache = _FakeCache()
	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	return session, cache


class TestSessionBackgroundJobHelpers:
	def test_pending_jobs_round_trip(self, session_with_fake_cache):
		session, _ = session_with_fake_cache
		assert session.get_pending_jobs("s1") == set()
		session.register_pending_job("s1", "j1")
		session.register_pending_job("s1", "j2")
		assert session.get_pending_jobs("s1") == {"j1", "j2"}
		session.clear_pending_job("s1", "j1")
		assert session.get_pending_jobs("s1") == {"j2"}

	def test_register_ignores_blanks(self, session_with_fake_cache):
		session, _ = session_with_fake_cache
		session.register_pending_job("", "j1")
		session.register_pending_job("s1", "")
		assert session.get_pending_jobs("s1") == set()

	def test_draining_window(self, session_with_fake_cache):
		import time

		session, _ = session_with_fake_cache
		assert session.is_draining("s1") is False
		session.set_draining("s1", time.time() + 100)
		assert session.is_draining("s1") is True
		# Past deadline → no longer draining.
		session.set_draining("s1", time.time() - 1)
		assert session.is_draining("s1") is False

	def test_delete_session_state_clears_pending_jobs(self, session_with_fake_cache):
		import time

		session, cache = session_with_fake_cache
		session.register_pending_job("s1", "j1")
		session.set_draining("s1", time.time() + 100)
		session.delete_session_state("s1")
		assert session.get_pending_jobs("s1") == set()
		assert session.is_draining("s1") is False


# --------------------------------------------------------------------------
# analyze._bg_wait_for_pending_jobs
# --------------------------------------------------------------------------

class TestBgWaitForPendingJobs:
	def _patch_common(self, monkeypatch, *, pending, wait_seconds, scheduler_disabled=False):
		from optimus import analyze

		monkeypatch.setattr(analyze.session, "get_pending_jobs", lambda u: set(pending))
		monkeypatch.setattr(analyze.session, "clear_pending_job", lambda u, j: None)
		monkeypatch.setattr(analyze, "is_scheduler_disabled", lambda: scheduler_disabled)
		monkeypatch.setattr(
			"optimus.settings.get_config",
			lambda: types.SimpleNamespace(background_job_wait_seconds=wait_seconds),
		)
		return analyze

	def test_noop_when_nothing_pending(self, monkeypatch):
		analyze = self._patch_common(monkeypatch, pending=[], wait_seconds=60)
		assert analyze._bg_wait_for_pending_jobs("s1", "PS-1", None) == 0

	def test_noop_when_wait_disabled(self, monkeypatch):
		analyze = self._patch_common(monkeypatch, pending=["j1"], wait_seconds=0)
		assert analyze._bg_wait_for_pending_jobs("s1", "PS-1", None) == 0

	def test_noop_when_scheduler_disabled(self, monkeypatch):
		analyze = self._patch_common(
			monkeypatch, pending=["j1"], wait_seconds=60, scheduler_disabled=True
		)
		assert analyze._bg_wait_for_pending_jobs("s1", "PS-1", None) == 0

	def test_proceeds_when_all_jobs_finished(self, monkeypatch):
		analyze = self._patch_common(monkeypatch, pending=["j1", "j2"], wait_seconds=60)
		monkeypatch.setattr(analyze, "_rq_status", lambda jid: "finished")
		assert analyze._bg_wait_for_pending_jobs("s1", "PS-1", None) == 0

	def test_cap_hit_returns_count_of_still_running(self, monkeypatch):
		import time

		analyze = self._patch_common(monkeypatch, pending=["j1", "j2"], wait_seconds=60)
		monkeypatch.setattr(analyze, "_rq_status", lambda jid: "started")
		# Deadline already in the past → proceed, reporting 2 still running.
		assert analyze._bg_wait_for_pending_jobs("s1", "PS-1", time.time() - 1) == 2

	def test_reenqueues_when_jobs_still_running(self, monkeypatch):
		import time

		import frappe

		analyze = self._patch_common(monkeypatch, pending=["j1"], wait_seconds=60)
		monkeypatch.setattr(analyze, "_rq_status", lambda jid: "started")
		monkeypatch.setattr(analyze, "_publish_progress", lambda *a, **k: None)
		monkeypatch.setattr(analyze.time, "sleep", lambda *a, **k: None)
		# The drain keeps the session on "Capturing Background Jobs" (never "Analyzing")
		# while it waits; capture that status write and the re-enqueue.
		statuses = []
		monkeypatch.setattr(
			frappe, "db", types.SimpleNamespace(set_value=lambda *a, **k: statuses.append(a)), raising=False
		)
		monkeypatch.setattr(analyze, "safe_commit", lambda: None)
		enq = []
		monkeypatch.setattr(frappe, "enqueue", lambda *a, **k: enq.append((a, k)), raising=False)

		out = analyze._bg_wait_for_pending_jobs("s1", "PS-1", time.time() + 60)
		assert out is None  # re-enqueued; caller returns
		assert len(enq) == 1
		args, kwargs = enq[0]
		assert args[0] == "optimus.analyze.run"
		assert kwargs.get("session_uuid") == "s1"
		assert "_bg_wait_until" in kwargs
		assert [a[-1] for a in statuses] == ["Capturing Background Jobs"]


# --------------------------------------------------------------------------
# the background_job_wait_seconds setting
# --------------------------------------------------------------------------

class TestBackgroundJobWaitSeconds:
	def test_default(self):
		from optimus import settings

		# v0.7.x: default raised 60 → 300 ("wait until all jobs reach a terminal
		# state", capped at the 300s hard ceiling).
		assert settings.OptimusConfig().background_job_wait_seconds == 300
		assert settings._DEFAULTS["background_job_wait_seconds"] == 300

	def _min_row(self, **overrides):
		row = {
			"max_queries_per_recording": 2000,
			"redundant_doc_threshold": 5,
			"redundant_cache_threshold": 50,
			"redundant_perm_threshold": 10,
			"n_plus_one_min_occurrences": 10,
		}
		row.update(overrides)
		return row

	def test_resolve_clamps_to_ceiling(self):
		from optimus import settings

		with patch.object(settings, "_read_doctype_row", return_value=self._min_row(background_job_wait_seconds=9999)), \
		     patch.object(settings, "_site_conf_fallback", return_value=None):
			assert settings._resolve().background_job_wait_seconds == 300

	def test_resolve_allows_zero(self):
		from optimus import settings

		with patch.object(settings, "_read_doctype_row", return_value=self._min_row(background_job_wait_seconds=0)), \
		     patch.object(settings, "_site_conf_fallback", return_value=None):
			assert settings._resolve().background_job_wait_seconds == 0

	def test_resolve_defaults_when_field_absent(self):
		from optimus import settings

		with patch.object(settings, "_read_doctype_row", return_value=self._min_row()), \
		     patch.object(settings, "_site_conf_fallback", return_value=None):
			assert settings._resolve().background_job_wait_seconds == 300
