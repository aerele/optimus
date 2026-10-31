# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Session lifecycle endpoints in ``optimus.api`` (start, stop, status, retry)."""

import datetime
import time
import types

import pytest

from optimus import api, capture, infra_capture


class FakeSessionStore:
	"""In-memory ``optimus.session``."""

	def __init__(self):
		self.active = {}
		self.meta = {}
		self.pending = {}
		self.draining = {}
		self.recordings = 0

	def get_active_session_for(self, user):
		return self.active.get(user)

	def set_active_session(self, user, uuid):
		self.active[user] = uuid

	def clear_active_session(self, user):
		self.active.pop(user, None)

	def set_session_meta(self, uuid, meta):
		self.meta[uuid] = meta

	def get_session_meta(self, uuid):
		return self.meta.get(uuid)

	def get_pending_jobs(self, uuid):
		return self.pending.get(uuid, [])

	def set_draining(self, uuid, deadline):
		self.draining[uuid] = deadline

	def recording_count(self, uuid):
		return self.recordings


class FakeDB:
	def __init__(self):
		self.rows = {}  # session_uuid -> {"name", "user", "status", ...}

	def get_value(self, doctype, filters, fieldname=None, *a, **kw):
		if isinstance(filters, str):  # lookup by docname
			row = next((r for r in self.rows.values() if r["name"] == filters), None)
			if isinstance(fieldname, list):  # Frappe returns a tuple for a field list
				return tuple(row.get(f) for f in fieldname) if row else None
			return row.get(fieldname) if row else None
		row = self.rows.get(filters.get("session_uuid"))
		if row is None:
			return None
		if "user" in filters and filters["user"] != row["user"]:
			return None
		if isinstance(fieldname, list):
			return {f: row.get(f) for f in fieldname}
		return row.get(fieldname)

	def set_value(self, doctype, name, values, value=None):
		updates = values if isinstance(values, dict) else {values: value}
		for row in self.rows.values():
			if row["name"] == name:
				row.update(updates)


class FakeDoc:
	def __init__(self, fields):
		self.fields = fields
		self.name = "OPT-0001"

	def insert(self, ignore_permissions=False):
		assert ignore_permissions is True  # the profiler user has no create perm
		return self


@pytest.fixture
def env(monkeypatch):
	frappe = api.frappe

	store = FakeSessionStore()
	db = FakeDB()
	created = []
	enqueued = []

	def _get_doc(fields):
		doc = FakeDoc(fields)
		created.append(doc)
		return doc

	monkeypatch.setattr(api, "session", store)
	monkeypatch.setattr(api, "safe_commit", lambda: None)
	monkeypatch.setattr(api, "now_datetime", datetime.datetime.now)
	monkeypatch.setattr(api, "_publish_session_event", lambda *a, **kw: None)
	monkeypatch.setattr(capture, "_force_stop_inflight_capture", lambda **kw: None)
	monkeypatch.setattr(infra_capture, "_force_stop_inflight", lambda *a, **kw: None)
	monkeypatch.setattr(api.ratelimit, "enforce_user_rate_limit", lambda *a, **kw: None)
	monkeypatch.setattr(api, "_require_profiler_user", lambda: "alice@example.com")
	monkeypatch.setattr(api, "_require_session_permission", lambda *a, **kw: "OPT-0001")
	monkeypatch.setattr(frappe, "db", db, raising=False)
	monkeypatch.setattr(frappe, "local", types.SimpleNamespace(), raising=False)
	monkeypatch.setattr(frappe, "get_doc", _get_doc, raising=False)
	monkeypatch.setattr(frappe, "generate_hash", lambda length=10: "h" * length, raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda *a, **kw: None, raising=False)
	monkeypatch.setattr(frappe, "conf", {}, raising=False)
	monkeypatch.setattr(frappe, "enqueue", lambda *a, **kw: enqueued.append((a, kw)), raising=False)
	return types.SimpleNamespace(store=store, db=db, created=created, enqueued=enqueued)


def _scheduler(monkeypatch, disabled):
	import frappe.utils.scheduler as sched

	monkeypatch.setattr(sched, "is_scheduler_disabled", lambda: disabled)


def _row(env, uuid="u1", name="OPT-1", user="alice@example.com", status="Recording"):
	env.db.rows[uuid] = {"name": name, "user": user, "owner": user, "status": status}


def _bg_wait(monkeypatch, seconds):
	from optimus import settings

	monkeypatch.setattr(
		settings, "get_config", lambda: types.SimpleNamespace(background_job_wait_seconds=seconds)
	)


class TestStart:
	def test_creates_recording_row_meta_and_activates_user(self, env):
		out = api.start(label="  checkout flow  ", notes="  click pay  ")

		fields = env.created[0].fields
		assert fields["status"] == "Recording"
		assert fields["title"] == "checkout flow"
		assert fields["notes"] == "click pay"
		assert fields["user"] == "alice@example.com"
		assert out["session_uuid"] == "h" * 16
		assert env.store.active["alice@example.com"] == out["session_uuid"]
		meta = env.store.meta[out["session_uuid"]]
		assert meta["docname"] == "OPT-0001"
		assert meta["capture_python_tree"] is True

	def test_blank_notes_are_not_persisted_and_title_is_defaulted(self, env):
		out = api.start(label="   ", notes="   ")

		assert "notes" not in env.created[0].fields
		assert out["title"].startswith("Profiling session @ ")

	def test_overlong_label_is_trimmed_to_the_data_field_width(self, env):
		out = api.start(label="x" * 500)

		assert len(out["title"]) == 140

	def test_capture_python_tree_false_is_recorded(self, env):
		out = api.start(capture_python_tree=False)

		assert env.store.meta[out["session_uuid"]]["capture_python_tree"] is False

	def test_starting_twice_stops_the_previous_session_first(self, env, monkeypatch):
		_scheduler(monkeypatch, disabled=False)
		env.store.active["alice@example.com"] = "old-uuid"
		_row(env, uuid="old-uuid", name="OPT-OLD")

		out = api.start()

		assert env.db.rows["old-uuid"]["status"] == "Stopping"
		assert env.enqueued, "previous session's analyze was queued"
		assert env.store.active["alice@example.com"] == out["session_uuid"] != "old-uuid"


class TestStop:
	def test_without_active_session_reports_nothing_stopped(self, env):
		assert api.stop() == {"stopped": False, "reason": "no active session"}

	def test_async_stop_marks_stopping_and_queues_analyze(self, env, monkeypatch):
		_scheduler(monkeypatch, disabled=False)
		env.store.active["alice@example.com"] = "u1"
		_row(env)

		out = api.stop()

		assert out == {
			"stopped": True,
			"session_uuid": "u1",
			"docname": "OPT-1",
			"ran_inline": False,
			"status": None,
			"empty": False,
		}
		assert "alice@example.com" not in env.store.active
		assert env.db.rows["u1"]["status"] == "Stopping"
		assert env.enqueued[0][1]["now"] is False

	def test_inline_stop_returns_the_final_status_read_back_from_the_row(self, env, monkeypatch):
		frappe = api.frappe

		_scheduler(monkeypatch, disabled=True)
		env.store.active["alice@example.com"] = "u1"
		_row(env)

		def _inline_enqueue(*a, **kw):
			assert kw["now"] is True
			env.db.rows["u1"]["status"] = "Failed"

		monkeypatch.setattr(frappe, "enqueue", _inline_enqueue, raising=False)

		out = api.stop()

		assert out["ran_inline"] is True
		assert out["status"] == "Failed"

	def test_stop_of_a_session_with_no_row_does_not_queue_analyze(self, env, monkeypatch):
		_scheduler(monkeypatch, disabled=False)
		env.store.active["alice@example.com"] = "ghost"

		out = api.stop()

		assert out["stopped"] is True
		assert out["docname"] is None
		assert out["ran_inline"] is False
		assert env.enqueued == []

	def test_background_jobs_open_a_draining_window_and_status(self, env, monkeypatch):
		_scheduler(monkeypatch, disabled=False)
		_bg_wait(monkeypatch, 30)
		env.store.active["alice@example.com"] = "u1"
		env.store.pending["u1"] = ["job-1"]
		_row(env)

		monkeypatch.setattr(api.time, "time", lambda: 1000.0)
		api.stop()

		assert env.db.rows["u1"]["status"] == "Capturing Background Jobs"
		# deadline = wait + 60s analyze grace
		assert env.store.draining["u1"] == 1090.0

	def test_no_pending_jobs_means_no_draining_window(self, env, monkeypatch):
		_scheduler(monkeypatch, disabled=False)
		_bg_wait(monkeypatch, 30)
		env.store.active["alice@example.com"] = "u1"
		_row(env)

		api.stop()

		assert env.store.draining == {}
		assert env.db.rows["u1"]["status"] == "Stopping"


class TestMarkStopping:
	def test_only_the_owner_can_stop_a_session(self, env):
		_row(env, user="bob@example.com")

		assert api._mark_stopping("alice@example.com", "u1") is None
		assert env.db.rows["u1"]["status"] == "Recording"


class TestEnqueueAnalyzeInlineCap:
	def test_oversized_session_is_marked_failed_with_retry_guidance_and_not_analyzed(
		self, env, monkeypatch
	):
		_scheduler(monkeypatch, disabled=True)
		env.store.recordings = 51
		_row(env, status="Stopping")

		assert api._enqueue_analyze("u1", docname="OPT-1") is True

		assert env.db.rows["u1"]["status"] == "Failed"
		assert "Retry Analyze" in env.db.rows["u1"]["analyzer_warnings"]
		assert env.enqueued == []

	def test_site_config_can_raise_the_inline_cap(self, env, monkeypatch):
		frappe = api.frappe

		_scheduler(monkeypatch, disabled=True)
		monkeypatch.setattr(frappe, "conf", {"optimus_inline_analyze_limit": 100}, raising=False)
		env.store.recordings = 51

		assert api._enqueue_analyze("u1", docname="OPT-1") is True

		assert env.enqueued[0][1]["now"] is True

	def test_inline_analyze_crash_is_swallowed_so_stop_does_not_500(self, env, monkeypatch):
		frappe = api.frappe

		_scheduler(monkeypatch, disabled=True)

		def _boom(*a, **kw):
			raise RuntimeError("analyze blew up")

		monkeypatch.setattr(frappe, "enqueue", _boom, raising=False)

		assert api._enqueue_analyze("u1") is True

	def test_scheduler_probe_failure_falls_back_to_the_async_queue(self, env, monkeypatch):
		import frappe.utils.scheduler as sched

		def _boom():
			raise RuntimeError("redis down")

		monkeypatch.setattr(sched, "is_scheduler_disabled", _boom)

		assert api._enqueue_analyze("u1") is False
		assert env.enqueued[0][1]["now"] is False


class TestStatusEndpoints:
	def test_status_inactive(self, env):
		assert api.status() == {"active": False}

	def test_status_active_returns_meta_fields(self, env):
		env.store.active["alice@example.com"] = "u1"
		env.store.meta["u1"] = {"docname": "OPT-1", "label": "flow", "started_at": "t0", "user": "x"}

		assert api.status() == {
			"active": True,
			"session_uuid": "u1",
			"docname": "OPT-1",
			"label": "flow",
			"started_at": "t0",
		}

	def test_get_active_session_returns_none_or_full_meta(self, env):
		assert api.get_active_session() is None
		env.store.active["alice@example.com"] = "u1"
		env.store.meta["u1"] = {"docname": "OPT-1"}

		assert api.get_active_session() == {"docname": "OPT-1"}


class TestDrainProgress:
	def test_reports_pending_jobs_and_remaining_window_without_the_grace(self, env, monkeypatch):
		_bg_wait(monkeypatch, 300)
		_row(env, status="Capturing Background Jobs")
		env.store.pending["u1"] = ["a", "b"]
		monkeypatch.setattr(api.time, "time", lambda: 1000.0)
		env.store.meta["u1"] = {"draining_until": 1000.0 + 60 + 120}

		out = api.drain_progress("u1")

		assert out["status"] == "Capturing Background Jobs"
		assert out["pending"] == 2
		assert out["remaining_seconds"] == 120
		assert out["window_seconds"] == 300

	def test_remaining_never_goes_negative_after_the_window_closes(self, env, monkeypatch):
		_bg_wait(monkeypatch, 300)
		_row(env, status="Analyzing")
		env.store.meta["u1"] = {"draining_until": time.time() - 500}

		assert api.drain_progress("u1")["remaining_seconds"] == 0

	def test_garbage_deadline_and_disabled_wait_degrade_to_none(self, env, monkeypatch):
		_bg_wait(monkeypatch, 0)
		_row(env, status="Ready")
		env.store.meta["u1"] = {"draining_until": "not-a-number"}

		out = api.drain_progress("u1")

		assert out["remaining_seconds"] is None
		assert out["window_seconds"] is None
		assert out["pending"] == 0


class TestRetryAnalyze:
	@pytest.fixture(autouse=True)
	def _roles(self, env, monkeypatch):
		frappe = api.frappe
		from optimus import ai_jobs

		self.roles = ["Optimus User"]
		monkeypatch.setattr(frappe, "get_roles", lambda user=None: self.roles, raising=False)

		def _has_permission(doctype, ptype, name, user=None):
			owner = next((r["owner"] for r in env.db.rows.values() if r["name"] == name), None)
			if "System Manager" in self.roles:
				return True
			return ptype == "read" and owner == user

		def _prepare(docname, session_uuid, *, requested_by):
			row = env.db.rows[session_uuid]
			row.update(status="Stopping", analyzer_warnings=None)
			return True

		def _throw(msg, exc=None, **kw):
			raise (exc or frappe.ValidationError)(msg)

		monkeypatch.setattr(frappe, "has_permission", _has_permission, raising=False)
		monkeypatch.setattr(frappe, "session", types.SimpleNamespace(user="alice@example.com"), raising=False)
		monkeypatch.setattr(ai_jobs, "prepare_analyze_retry", _prepare)
		monkeypatch.setattr(frappe, "throw", _throw, raising=False)
		monkeypatch.setattr(api, "_", lambda m: m)

	def test_requires_a_session_uuid(self, env):
		frappe = api.frappe

		with pytest.raises(frappe.ValidationError, match="session_uuid is required"):
			api.retry_analyze("")

	def test_unknown_session_is_rejected(self, env):
		frappe = api.frappe

		with pytest.raises(frappe.DoesNotExistError, match="No Optimus Session found"):
			api.retry_analyze("missing")

	def test_other_users_failed_session_is_refused(self, env):
		frappe = api.frappe

		_row(env, user="bob@example.com", status="Failed")

		with pytest.raises(frappe.PermissionError):
			api.retry_analyze("u1")

		assert env.db.rows["u1"]["status"] == "Failed"

	def test_system_manager_may_retry_anyones_session(self, env, monkeypatch):
		_scheduler(monkeypatch, disabled=False)
		self.roles = ["System Manager"]
		_row(env, user="bob@example.com", status="Failed")

		assert api.retry_analyze("u1")["retried"] is True

	def test_failed_session_is_reset_and_requeued_asynchronously(self, env, monkeypatch):
		from optimus import pdf_export

		cleared = []
		monkeypatch.setattr(pdf_export, "clear_cached_pdf", cleared.append)
		_scheduler(monkeypatch, disabled=False)
		_row(env, status="Failed")
		env.db.rows["u1"]["analyzer_warnings"] = "old error"

		out = api.retry_analyze("u1")

		assert out == {
			"retried": True,
			"session_uuid": "u1",
			"docname": "OPT-1",
			"ran_inline": False,
			"status": None,
		}
		assert env.db.rows["u1"]["status"] == "Stopping"
		assert env.db.rows["u1"]["analyzer_warnings"] is None
		assert cleared == ["u1"], "stale PDF must be dropped so it regenerates from the new HTML"
		assert env.enqueued[0][1]["now"] is False

	def test_retry_refused_when_the_session_changed_before_the_reset(self, env, monkeypatch):
		frappe = api.frappe
		from optimus import ai_jobs

		monkeypatch.setattr(ai_jobs, "prepare_analyze_retry", lambda *a, **kw: False)
		_row(env, status="Failed")

		with pytest.raises(frappe.ValidationError, match="changed before Retry Analyze"):
			api.retry_analyze("u1")

		assert env.enqueued == []

	def test_inline_retry_reports_the_final_status(self, env, monkeypatch):
		frappe = api.frappe

		_scheduler(monkeypatch, disabled=True)
		_row(env, status="Failed")

		def _inline(*a, **kw):
			env.db.rows["u1"]["status"] = "Ready"

		monkeypatch.setattr(frappe, "enqueue", _inline, raising=False)

		out = api.retry_analyze("u1")

		assert out["ran_inline"] is True
		assert out["status"] == "Ready"

	def test_pdf_cache_clear_failure_does_not_block_the_retry(self, env, monkeypatch):
		from optimus import pdf_export

		def _boom(uuid):
			raise RuntimeError("fs")

		monkeypatch.setattr(pdf_export, "clear_cached_pdf", _boom)
		_scheduler(monkeypatch, disabled=False)
		_row(env, status="Failed")

		assert api.retry_analyze("u1")["retried"] is True
