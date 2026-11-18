# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Concurrency guards on analyze.

Two analyze jobs at once roughly double peak RAM (the OOM trigger on long flows).
A global single-flight (only one session does the heavy phase at a time), built
on the re-enqueue/yield pattern rather than a held lock, can't deadlock with
run()'s self-re-enqueue and self-heals if a worker dies. Duplicate analyze
enqueues for the same session are deduped.
"""

import inspect
import types

import frappe
import pytest

from optimus import analyze
from optimus.tests.singleflight_fakes import FlagCache


class FakeDB:
	def __init__(self):
		self.set_calls = []

	def set_value(self, doctype, name, field_or_dict, value=None):
		self.set_calls.append((doctype, name, field_or_dict, value))

	def commit(self):
		pass


@pytest.fixture
def sf_env(monkeypatch):
	"""Wire the single-flight helper's collaborators with fakes."""
	cache = FlagCache()
	db = FakeDB()
	enqueued = []

	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	monkeypatch.setattr(frappe, "db", db, raising=False)
	monkeypatch.setattr(frappe, "conf", types.SimpleNamespace(get=lambda k, d=None: d), raising=False)
	monkeypatch.setattr(frappe, "enqueue", lambda *a, **k: enqueued.append((a, k)), raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda *a, **k: None, raising=False)
	# Collaborators bound on the analyze module.
	monkeypatch.setattr(analyze, "is_scheduler_disabled", lambda: False, raising=False)
	monkeypatch.setattr(analyze, "_publish_progress", lambda *a, **k: None, raising=False)
	monkeypatch.setattr(analyze, "safe_commit", lambda: None, raising=False)
	monkeypatch.setattr(analyze.time, "sleep", lambda *a, **k: None)
	return types.SimpleNamespace(cache=cache, db=db, enqueued=enqueued)


_KEY = "optimus:analyze:inflight"


class TestAcquireSingleFlight:
	def test_skipped_when_scheduler_disabled(self, sf_env, monkeypatch):
		monkeypatch.setattr(analyze, "is_scheduler_disabled", lambda: True)
		sf_env.cache.set_value(_KEY, "OTHER")
		# Inline path: no worker to yield to → proceed, don't touch cache/enqueue.
		assert analyze._acquire_singleflight("B", "PS-B", None) is True
		assert sf_env.enqueued == []


class TestRunWiring:
	def test_run_releases_singleflight_in_finally(self):
		src = inspect.getsource(analyze.run)
		# Release must be in the finally so a crash never strands the flag.
		assert "_release_singleflight" in src
		finally_idx = src.rfind("finally:")
		assert finally_idx != -1 and "_release_singleflight" in src[finally_idx:]

	def test_self_reenqueues_stay_anonymous(self):
		"""Neither the bg-wait nor the single-flight re-enqueue may carry a
		stable job_id that would deadlock the running job against its own
		continuation."""
		for fn in (analyze._bg_wait_for_pending_jobs, analyze._acquire_singleflight):
			src = inspect.getsource(fn)
			assert "job_id" not in src, f"{fn.__name__} must re-enqueue anonymously"


def _stub_scheduler(monkeypatch, disabled):
	import sys
	mod = types.ModuleType("frappe.utils.scheduler")
	mod.is_scheduler_disabled = lambda: disabled
	monkeypatch.setitem(sys.modules, "frappe.utils.scheduler", mod)


class TestEnqueueAnalyzeAsync:
	"""The async enqueue must NOT use a stable job_id + is_job_enqueued dedup
	(that guard stranded sessions when a zombie STARTED job blocked the enqueue).
	Concurrent-analyze RAM is bounded by analyze.run's single-flight, so it always
	enqueues anonymously."""

	def test_async_enqueue_is_anonymous_and_always_fires(self, monkeypatch):
		from optimus import api
		_stub_scheduler(monkeypatch, disabled=False)
		captured = {}
		monkeypatch.setattr(frappe, "enqueue", lambda *a, **k: captured.update(k))
		monkeypatch.setattr(frappe, "log_error", lambda *a, **k: None, raising=False)

		ran_inline = api._enqueue_analyze("uuid-1", docname="PS-1")
		assert ran_inline is False  # async path, session still mid-flight
		assert captured.get("now") is False
		assert captured.get("session_uuid") == "uuid-1"
		# No stable job_id → no is_job_enqueued strand.
		assert "job_id" not in captured
