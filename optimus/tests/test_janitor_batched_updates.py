# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the janitor sweeps' batched updates.

The lifecycle sweeps call ``frappe.db.set_value`` ONCE with a ``{"name": ("in",
[...])}`` filter; ``_sweep_old_sessions`` preloads every File-doc name in a
single ``frappe.get_all``. Line profiling additionally checks current ownership under row locks. ``frappe`` is stubbed via
``monkeypatch.setitem(sys.modules, ...)`` so these run pure (no bench / no
MariaDB) and the real module is restored at teardown.
"""

import importlib
import sys
import types

import pytest


def _install_frappe_stub(monkeypatch):
	"""Build a minimal ``frappe`` stub the janitor module can import. All
	``sys.modules`` mutations go through ``monkeypatch.setitem`` so pytest
	restores the originals at teardown.
	"""
	stub = types.ModuleType("frappe")
	stub._set_value_calls = []
	stub._get_all_calls = []
	stub._delete_doc_calls = []
	stub._committed = 0
	stub._logger = types.SimpleNamespace(info=lambda msg: None)
	stub._get_all_return = {}  # keyed by doctype → list of rows
	stub._enqueue_calls = []

	class _DB:
		def get_all(self, doctype, filters=None, fields=None, **kwargs):
			stub._get_all_calls.append((doctype, filters, tuple(fields or ())))
			return list(stub._get_all_return.get(doctype, []))

		def set_value(self, doctype, name_or_filters, fieldname_or_dict, *args, **kwargs):
			stub._set_value_calls.append((doctype, name_or_filters, fieldname_or_dict))
			return True

		def commit(self):
			stub._committed += 1

		def rollback(self):
			pass

		def get_value(self, *args, **kwargs):
			return None

		def sql(self, *args, **kwargs):
			return []

	stub.db = _DB()
	# Allow `frappe.db.get_all` (used by janitor) AND `frappe.get_all`
	# (used by other modules) to share the recorder.
	stub.get_all = stub.db.get_all
	stub.delete_doc = lambda *a, **kw: stub._delete_doc_calls.append((a, kw))
	stub.enqueue = lambda *a, **kw: stub._enqueue_calls.append((a, kw))
	stub.log_error = lambda **kw: None
	stub.logger = lambda: stub._logger
	stub.as_json = lambda v: '["stub"]'
	stub.conf = {}
	monkeypatch.setitem(sys.modules, "frappe", stub)

	# Helper utils janitor.py imports.
	fu = types.ModuleType("frappe.utils")
	def _now():
		from datetime import datetime
		return datetime(2026, 5, 13, 12, 0, 0)
	def _add_to_date(d, **kw):
		# Cheap shim: return a sentinel string the test inspects via filters.
		return f"cutoff({kw})"
	fu.now_datetime = _now
	fu.add_to_date = _add_to_date
	monkeypatch.setitem(sys.modules, "frappe.utils", fu)

	# Session-helper stubs. get_active_session_for drives the stale-recording
	# liveness check; default None = "pointer expired" so all candidates are
	# treated as stale (tests override stub._active_pointers per user).
	stub._active_pointers = {}
	session_mod = types.ModuleType("optimus.session")
	session_mod.clear_active_session = lambda user: None
	session_mod.get_active_session_for = lambda user: stub._active_pointers.get(user)
	monkeypatch.setitem(sys.modules, "optimus.session", session_mod)
	# janitor binds via ``from optimus import session``: that reads the package
	# ATTRIBUTE, not just sys.modules so patch the attribute too or the reload
	# picks up the real session module (which needs a live frappe.cache).
	import optimus as _optimus_pkg
	monkeypatch.setattr(_optimus_pkg, "session", session_mod, raising=False)

	# Line profiling capture stub.
	lp_capture = types.ModuleType("optimus.line_profile.capture")
	lp_capture.cleanup_run = lambda run_uuid: None
	monkeypatch.setitem(sys.modules, "optimus.line_profile.capture", lp_capture)

	# analyze.is_singleflight_holder stub (the stuck-analyzing sweep consults it
	# so it won't fail a session that's still actively heartbeating). Default:
	# nobody holds the flag; tests override stub._holder_uuid to simulate a live
	# run.
	stub._holder_uuid = None
	analyze_mod = types.ModuleType("optimus.analyze")
	def run_step(fn, **kwargs):
		try:
			return fn(), False
		except Exception:
			return None, True
	analyze_mod._run_ai_step = run_step
	analyze_mod.is_singleflight_holder = lambda uuid: uuid is not None and uuid == stub._holder_uuid
	monkeypatch.setitem(sys.modules, "optimus.analyze", analyze_mod)

	# api.claim_for_stop locks the row and moves it Recording -> Stopping, returning
	# its name, or None when the row already left Recording. Tests list the rows
	# another caller already took in stub._taken.
	stub._taken = set()
	stub._claimed = []
	stub._claim_raises = set()
	api_mod = types.ModuleType("optimus.api")
	def claim_for_stop(name):
		stub._claimed.append(name)
		if name in stub._claim_raises:
			raise RuntimeError("lock wait timeout")
		return None if name in stub._taken else name
	api_mod.claim_for_stop = claim_for_stop
	monkeypatch.setitem(sys.modules, "optimus.api", api_mod)

	return stub


def _reload_janitor(monkeypatch):
	"""Re-import janitor.py under the fresh frappe stub via
	``importlib.reload`` so its top-level code re-runs against the current
	stub.
	"""
	import optimus.janitor as janitor
	return importlib.reload(janitor)


# --------------------------------------------------------------------------
# Stale Recording sweep single batched UPDATE.
# --------------------------------------------------------------------------

class TestSweepStaleRecording:
	def test_each_stale_row_is_claimed_and_analyzed_once(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Session"] = [
			{"name": "PS-1", "session_uuid": "u1", "user": "a@b.com"},
			{"name": "PS-2", "session_uuid": "u2", "user": "c@d.com"},
			{"name": "PS-3", "session_uuid": "u3", "user": "e@f.com"},
		]
		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stale_recording()

		assert stub._claimed == ["PS-1", "PS-2", "PS-3"]
		assert [c[1]["session_uuid"] for c in stub._enqueue_calls] == ["u1", "u2", "u3"]
		# No unlocked bulk status write: the claim does the transition under a row lock.
		assert [c for c in stub._set_value_calls if c[0] == "Optimus Session"] == []

	def test_a_row_another_caller_already_took_is_not_analyzed_a_second_time(self, monkeypatch):
		"""A Stop click that won the race has already queued analyze for this row."""
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Session"] = [
			{"name": "PS-TAKEN", "session_uuid": "u-taken", "user": "a@b.com"},
			{"name": "PS-FREE", "session_uuid": "u-free", "user": "c@d.com"},
		]
		stub._taken = {"PS-TAKEN"}
		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stale_recording()

		assert [c[1]["session_uuid"] for c in stub._enqueue_calls] == ["u-free"]

	def test_a_claim_that_raises_skips_that_row_and_the_batch_goes_on(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Session"] = [
			{"name": "PS-LOCKED", "session_uuid": "u-locked", "user": "a@b.com"},
			{"name": "PS-FREE", "session_uuid": "u-free", "user": "c@d.com"},
		]
		stub._claim_raises = {"PS-LOCKED"}
		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stale_recording()

		assert stub._claimed == ["PS-LOCKED", "PS-FREE"]
		assert [c[1]["session_uuid"] for c in stub._enqueue_calls] == ["u-free"]

	def test_no_claim_when_no_stale_rows(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Session"] = []
		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stale_recording()
		assert stub._claimed == [] and stub._enqueue_calls == []

	def test_live_recording_with_active_pointer_is_not_stopped(self, monkeypatch):
		"""A long but LIVE recording (Redis active pointer still points to it)
		must not be force-stopped; only rows whose pointer expired or moved are
		stopped."""
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Session"] = [
			{"name": "PS-LIVE", "session_uuid": "u-live", "user": "live@x.com"},
			{"name": "PS-GONE", "session_uuid": "u-gone", "user": "gone@x.com"},
		]
		stub._active_pointers = {"live@x.com": "u-live"}  # live@x still recording; gone@x expired
		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stale_recording()

		assert stub._claimed == ["PS-GONE"]
		assert len(stub._enqueue_calls) == 1

	def test_pointer_moved_to_new_session_is_kept(self, monkeypatch):
		"""Stopping a stale session must not clear a pointer that now names the user's new session."""
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Session"] = [
			{"name": "PS-OLD", "session_uuid": "u-old", "user": "a@x.com"},
		]
		stub._active_pointers = {"a@x.com": "u-new"}
		cleared = []
		sys.modules["optimus.session"].clear_active_session = cleared.append
		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stale_recording()

		assert len(stub._enqueue_calls) == 1  # the old session is still stopped
		assert cleared == []


# --------------------------------------------------------------------------
# Stuck Analyzing sweep single batched UPDATE.
# --------------------------------------------------------------------------

class TestSweepStuckAnalyzing:
	def test_one_set_value_call_for_N_rows(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Session"] = [
			{"name": "PS-A"},
			{"name": "PS-B"},
		]
		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stuck_analyzing()
		set_value_calls = [c for c in stub._set_value_calls if c[0] == "Optimus Session"]
		assert len(set_value_calls) == 1
		_, filters, fields = set_value_calls[0]
		assert filters["name"] == ("in", ["PS-A", "PS-B"])
		assert filters["status"] == "Analyzing" and "modified" in filters
		assert fields["status"] == "Failed"
		assert "analyzer_warnings" in fields

	def test_the_failed_note_names_a_lapsed_heartbeat(self, monkeypatch):
		"""A run whose flag lapsed looks stuck to this sweep, so the note names that
		cause next to a timeout or a crash."""
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Session"] = [{"name": "PS-A", "session_uuid": "u-a"}]
		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stuck_analyzing()
		_, _, fields = stub._set_value_calls[0]
		note = fields["analyzer_warnings"]
		assert "its analyze heartbeat lapsed (the optimus log names a failed or yielded heartbeat)" in note
		assert "timed out" in note and "crashed" in note and "Retry Analyze" in note

	def test_no_set_value_when_no_stuck_rows(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Session"] = []
		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stuck_analyzing()
		assert stub._set_value_calls == []

	def test_live_singleflight_holder_is_not_failed(self, monkeypatch):
		"""A long-but-live analyze (still heartbeating the single-flight flag)
		must NOT be force-failed; only the genuinely-wedged row is failed."""
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Session"] = [
			{"name": "PS-LIVE", "session_uuid": "u-live"},   # still heartbeating
			{"name": "PS-DEAD", "session_uuid": "u-dead"},   # genuinely wedged
		]
		stub._holder_uuid = "u-live"  # u-live currently holds the flag
		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stuck_analyzing()

		set_value_calls = [c for c in stub._set_value_calls if c[0] == "Optimus Session"]
		assert len(set_value_calls) == 1
		_, filters, fields = set_value_calls[0]
		# Only the dead one is failed; the live holder is skipped.
		assert filters["name"] == ("in", ["PS-DEAD"])
		assert filters["status"] == "Analyzing" and "modified" in filters
		assert fields["status"] == "Failed"


# --------------------------------------------------------------------------
# Stale Stopping sweep re-enqueue analyze for sessions stranded at Stopping.
# --------------------------------------------------------------------------

class TestSweepStaleStopping:
	def test_reenqueues_each_stale_stopping_row(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Session"] = [
			{"name": "PS-1", "session_uuid": "u1"},
			{"name": "PS-2", "session_uuid": "u2"},
		]
		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stale_stopping()

		# One batched set_value re-affirming Stopping (bumps modified → backoff).
		set_value_calls = [c for c in stub._set_value_calls if c[0] == "Optimus Session"]
		assert len(set_value_calls) == 1
		_, filters, fields = set_value_calls[0]
		assert filters["name"] == ("in", ["PS-1", "PS-2"])
		assert filters["status"] == ["in", ["Stopping", "Capturing Background Jobs"]]
		assert fields["status"] == "Stopping"
		# Analyze re-enqueued once per stranded row.
		assert len(stub._enqueue_calls) == 2
		methods = [c[0][0] for c in stub._enqueue_calls]
		assert methods == ["optimus.analyze.run", "optimus.analyze.run"]

	def test_filters_on_stopping_status(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Session"] = []
		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stale_stopping()
		# Selected by status IN (Stopping, Capturing Background Jobs).
		ga = [c for c in stub._get_all_calls if c[0] == "Optimus Session"]
		assert ga and ga[0][1].get("status") == ["in", ["Stopping", "Capturing Background Jobs"]]
		assert stub._enqueue_calls == []

	def test_wired_into_sweep_stale_sessions(self, monkeypatch):
		import inspect
		_install_frappe_stub(monkeypatch)
		janitor = _reload_janitor(monkeypatch)
		assert "_sweep_stale_stopping" in inspect.getsource(janitor.sweep_stale_sessions)


# --------------------------------------------------------------------------
# Stale line profiling sweep one batched UPDATE per branch (Recording + Analyzing).
# --------------------------------------------------------------------------

class TestSweepStalePhase2Runs:
	def test_only_locked_expired_recordings_are_cleaned(self, monkeypatch):
		from optimus import ai_jobs
		from optimus.line_profile import capture, jobs
		stub = _install_frappe_stub(monkeypatch)
		recordings = [
			{"name": "rec-1", "parent": "fake-parent", "run_uuid": "expired"},
			{"name": "rec-2", "parent": "fake-parent", "run_uuid": "newer-generation"},
		]
		analyses = [{"name": "ana-1", "parent": "fake-parent", "run_uuid": "stale-analysis"}]
		stub.db.get_all = lambda doctype, **kw: recordings if kw["filters"]["status"] == "Recording" else analyses
		calls, cleanups = [], []
		def expire(parent, run_uuid, **kw):
			calls.append((run_uuid, kw["status"]))
			return run_uuid != "newer-generation"
		monkeypatch.setattr(jobs, "expire_legacy", expire)
		monkeypatch.setattr(capture, "has_samples", lambda run_uuid: False)
		monkeypatch.setattr(capture, "cleanup_run", cleanups.append)
		monkeypatch.setattr(ai_jobs, "_transaction", lambda fn: fn())
		monkeypatch.setattr(ai_jobs, "_retry_sql", lambda fn: fn())
		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stale_phase2_runs()
		assert calls == [("expired", "Recording"), ("newer-generation", "Recording"), ("stale-analysis", "Analyzing")]
		assert cleanups == ["expired"], "analysis failure must preserve samples for retry"
		assert not stub._set_value_calls, "the journal checks current status under a lock"

	def test_one_phase2_cleanup_failure_does_not_skip_other_rows(self, monkeypatch):
		from optimus import ai_jobs
		from optimus.line_profile import capture, jobs
		stub = _install_frappe_stub(monkeypatch)
		rows = [{"parent": "p", "run_uuid": name} for name in ("bad", "good")]
		stub.db.get_all = lambda doctype, **kw: rows if kw["filters"]["status"] == "Recording" else []
		seen = []
		def cleanup(run_uuid):
			seen.append(run_uuid)
			if run_uuid == "bad":
				raise ConnectionError("synthetic Redis failure")
		monkeypatch.setattr(jobs, "expire_legacy", lambda *a, **kw: True)
		monkeypatch.setattr(capture, "has_samples", lambda run_uuid: False)
		monkeypatch.setattr(capture, "cleanup_run", cleanup)
		monkeypatch.setattr(ai_jobs, "_transaction", lambda fn: fn())
		monkeypatch.setattr(ai_jobs, "_retry_sql", lambda fn: fn())
		_reload_janitor(monkeypatch)._sweep_stale_phase2_runs()
		assert seen == ["bad", "good"]

	def _expiry(self, monkeypatch, has_samples, expired):
		from optimus import ai_jobs
		from optimus.line_profile import capture, jobs
		stub = _install_frappe_stub(monkeypatch)
		stub.db.get_all = lambda doctype, **kw: (
			[{"parent": "p", "run_uuid": "run-1"}] if kw["filters"]["status"] == "Recording" else [])
		seen = types.SimpleNamespace(expire=[], analyzed=[], cleaned=[])
		def expire(parent, run_uuid, **kw):
			seen.expire.append(kw["samples"])
			return expired(kw["samples"])
		monkeypatch.setattr(jobs, "expire_legacy", expire)
		monkeypatch.setattr(jobs, "analyze_expired", lambda *a, **kw: seen.analyzed.append((*a, sorted(kw))))
		monkeypatch.setattr(capture, "has_samples", has_samples)
		monkeypatch.setattr(capture, "cleanup_run", seen.cleaned.append)
		monkeypatch.setattr(ai_jobs, "_transaction", lambda fn: fn())
		monkeypatch.setattr(ai_jobs, "_retry_sql", lambda fn: fn())
		_reload_janitor(monkeypatch)._sweep_stale_phase2_runs()
		return seen

	def test_expired_recording_with_samples_is_analyzed_and_keeps_its_input(self, monkeypatch):
		seen = self._expiry(monkeypatch, lambda run_uuid: True, lambda samples: "analyze" if samples else True)
		assert seen.expire == [True]
		assert seen.analyzed == [("p", "run-1", ["cutoff"])]
		assert seen.cleaned == []

	def test_expired_recording_without_samples_fails_and_cleans(self, monkeypatch):
		seen = self._expiry(monkeypatch, lambda run_uuid: False, lambda samples: True)
		assert seen.analyzed == [] and seen.cleaned == ["run-1"]

	def test_unknown_sample_count_retries_next_sweep_and_never_deletes_input(self, monkeypatch):
		def broken(run_uuid):
			raise ConnectionError("redis down")
		seen = self._expiry(monkeypatch, broken, lambda samples: "retry" if samples is None else True)
		assert seen.expire == [None]
		assert seen.analyzed == [] and seen.cleaned == []

	@pytest.mark.parametrize("known", [True, None])
	def test_run_failed_after_the_retry_bound_keeps_its_samples_for_retry(self, monkeypatch, known):
		"""expire_legacy fails the run (True) but samples exist or are unknown: nothing is cleaned."""
		def has_samples(run_uuid):
			if known is None:
				raise ConnectionError("redis down")
			return known
		seen = self._expiry(monkeypatch, has_samples, lambda samples: True)
		assert seen.expire == [known]
		assert seen.analyzed == [] and seen.cleaned == []

	def test_unavailable_sample_count_is_logged_with_the_error_type_only(self, monkeypatch):
		from optimus import safe_call
		lines = []
		monkeypatch.setattr(safe_call, "log_error_line", lines.append)
		def broken(run_uuid):
			raise ConnectionError("secret redis host")
		self._expiry(monkeypatch, broken, lambda samples: "retry")
		assert len(lines) == 1 and "run-1" in lines[0] and "ConnectionError" in lines[0]
		assert "secret redis host" not in lines[0]

	def test_known_sample_count_logs_nothing(self, monkeypatch):
		from optimus import safe_call
		lines = []
		monkeypatch.setattr(safe_call, "log_error_line", lines.append)
		self._expiry(monkeypatch, lambda run_uuid: True, lambda samples: "analyze")
		assert lines == []

	def test_no_set_value_when_no_phase2_rows(self, monkeypatch):
		from optimus import ai_jobs
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Phase Two Run"] = []
		monkeypatch.setattr(ai_jobs, "_transaction", lambda fn: fn())
		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_stale_phase2_runs()
		set_value_calls = [c for c in stub._set_value_calls if c[0] == "Optimus Phase Two Run"]
		assert set_value_calls == []


# --------------------------------------------------------------------------
# _delete_older_than_days (a.k.a. _sweep_old_sessions) ONE bulk File fetch.
# --------------------------------------------------------------------------

class TestSweepOldSessionsBulkFileFetch:
	def test_one_get_all_for_files_regardless_of_session_count(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		# 3 sessions, each with 2 file URLs → 6 candidate URLs total.
		sessions = [
			{"name": f"PS-{i}", "title": f"t{i}",
			 "raw_report_file": f"/private/files/r{i}.html",
			 "raw_report_pdf_file": f"/private/files/r{i}.pdf"}
			for i in range(3)
		]
		# get_all sequence: first call → Optimus Session list; second call → File lookup.
		state = {"i": 0}
		def _get_all(doctype, filters=None, fields=None, **kwargs):
			stub._get_all_calls.append((doctype, filters, tuple(fields or ())))
			state["i"] += 1
			if doctype == "Optimus Session":
				return list(sessions)
			if doctype == "File":
				# Return matching File rows for half the URLs to exercise both
				# "found" and "not found" branches.
				urls = filters["file_url"][1] if isinstance(filters, dict) else []
				return [{"name": f"File-{u}", "file_url": u} for u in urls if "0" in u or "1" in u]
			return []
		stub.db.get_all = _get_all
		stub.get_all = _get_all

		janitor = _reload_janitor(monkeypatch)
		janitor._sweep_old_sessions()

		# Core parent deletion checks links before removing attached files.
		# No File is looked up/deleted by URL ahead of that transaction.
		assert not [c for c in stub._get_all_calls if c[0] == "File"]
		assert not [c for c in stub._delete_doc_calls if c[0][0] == "File"]
		assert stub._committed == 4  # candidate snapshot, then each parent
		# Session deletion fires per row (that part isn't batched it's a
		# delete with permissions + lifecycle events).
		ps_delete_calls = [c for c in stub._delete_doc_calls if c[0][0] == "Optimus Session"]
		assert len(ps_delete_calls) == 3


# --------------------------------------------------------------------------
# v0.13.x: ``session_retention_days = 0`` means keep forever the daily
# sweep early-returns without deleting anything. Closes the gap where the
# pre-v0.13.x ``or DEFAULT_RETENTION_DAYS`` silently fell back to 90 and
# made the field description's "Set to 0 to keep forever" promise a lie.
# --------------------------------------------------------------------------

class TestSweepOldSessionsForeverRetention:
	def _stub_get_config(self, monkeypatch, retention_days):
		"""Install a fake ``optimus.settings`` whose ``get_config()`` returns
		the chosen ``session_retention_days``, so the janitor's lazy import
		picks up the fake instead of the real settings reader.
		"""
		fake = types.ModuleType("optimus.settings")
		fake.get_config = lambda: types.SimpleNamespace(
			session_retention_days=retention_days,
		)
		monkeypatch.setitem(sys.modules, "optimus.settings", fake)

	def test_zero_retention_is_a_silent_noop(self, monkeypatch):
		"""``session_retention_days = 0`` → the sweep doesn't query
		Optimus Session at all (early-return before the get_all). No
		deletions, no File lookups."""
		stub = _install_frappe_stub(monkeypatch)
		self._stub_get_config(monkeypatch, retention_days=0)
		janitor = _reload_janitor(monkeypatch)

		janitor._sweep_old_sessions()

		# No queries at all the early-return fired before the
		# Optimus Session get_all.
		session_calls = [c for c in stub._get_all_calls if c[0] == "Optimus Session"]
		assert session_calls == [], (
			f"forever-retention must skip the session query, got {session_calls!r}"
		)
		# And no deletions.
		assert stub._delete_doc_calls == []

	def test_positive_retention_sweeps_normally(self, monkeypatch):
		"""Sanity: a positive value still sweeps. Guards against an
		over-eager ``<= 0`` check that would also skip the legit case."""
		stub = _install_frappe_stub(monkeypatch)
		stub._get_all_return["Optimus Session"] = []
		self._stub_get_config(monkeypatch, retention_days=30)
		janitor = _reload_janitor(monkeypatch)

		janitor._sweep_old_sessions()

		# get_all WAS called on Optimus Session this time.
		session_calls = [c for c in stub._get_all_calls if c[0] == "Optimus Session"]
		assert len(session_calls) >= 1
