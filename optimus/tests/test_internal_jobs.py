# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Jobs outside the user's flow are never tagged into a recording session: Optimus's own
jobs, scheduled jobs, and anything queued from inside an Optimus job."""

import sys
import types

import pytest

from optimus.tests.test_bg_job_phase1 import SESSION, _pending, env  # noqa: F401
from optimus.tests.test_enqueue_patch import fake_frappe  # noqa: F401

RUN_SCHEDULED = "frappe.core.doctype.scheduled_job_type.scheduled_job_type.run_scheduled_job"


def _marked(env):  # noqa: F811
	kw = env.calls[-1]["kwargs"]
	return "_profiler_session_id" in kw or "_lp_session_id" in kw


class TestScheduledJobs:
	@pytest.mark.parametrize("job_type", ["erpnext.accounts.tasks.daily", "optimus.ai_jobs.recover_pending"])
	def test_a_cron_from_the_scheduler_is_not_tagged(self, env, job_type):  # noqa: F811
		env.enqueue(RUN_SCHEDULED, job_type=job_type)
		assert not _marked(env)
		assert _pending(env) == set()

	def test_a_cron_started_from_a_request_is_tagged(self, env):  # noqa: F811
		env.fake.local.request = object()
		env.enqueue(RUN_SCHEDULED, job_type="erpnext.accounts.tasks.daily")
		assert env.calls[-1]["kwargs"]["_profiler_session_id"] == SESSION

	def test_a_cron_queued_from_inside_a_job_is_not_a_scheduler_enqueue(self, env):  # noqa: F811
		# No web request, but a worker job is running: this is not the scheduler.
		env.fake.local.job = object()
		env.enqueue(RUN_SCHEDULED, job_type="erpnext.accounts.tasks.daily")
		assert env.calls[-1]["kwargs"]["_profiler_session_id"] == SESSION

	def test_an_optimus_cron_started_from_a_request_is_not_tagged(self, env):  # noqa: F811
		env.fake.local.request = object()
		env.enqueue(RUN_SCHEDULED, job_type="optimus.janitor.sweep_stale_sessions")
		assert not _marked(env)

	def test_a_user_job_from_a_request_is_still_tagged(self, env):  # noqa: F811
		env.fake.local.request = object()
		env.enqueue("myapp.tasks.go")
		assert env.calls[-1]["kwargs"]["_profiler_session_id"] == SESSION


class TestDetectionFailure:
	def test_a_failing_session_lookup_still_enqueues_and_is_logged(self, env, monkeypatch):  # noqa: F811
		from optimus import safe_call, session

		def boom(user):
			raise RuntimeError("redis down")

		monkeypatch.setattr(session, "get_active_session_for", boom)
		lines = []
		monkeypatch.setattr(safe_call, "log_error_line", lambda msg, **kw: lines.append(msg))
		env.enqueue("myapp.tasks.go")
		env.enqueue("myapp.tasks.go")
		assert len(env.calls) == 2 and not _marked(env)
		assert len(lines) == 2 and all("RuntimeError" in m for m in lines)


class TestOptimusMethods:
	@pytest.mark.parametrize(
		"method",
		["optimus.ai_jobs.run_ai_refresh_slice", "optimus.line_profile.jobs.analyze", "optimus.analyze.run"],
	)
	def test_an_optimus_method_is_never_tagged(self, env, method):  # noqa: F811
		env.enqueue(method)
		assert not _marked(env)

	def test_an_app_that_only_starts_with_optimus_is_tagged(self, env):  # noqa: F811
		env.enqueue("optimus_extras.tasks.go")
		assert env.calls[-1]["kwargs"]["_profiler_session_id"] == SESSION


class TestNestedEnqueue:
	@pytest.mark.parametrize("flag", ["optimus_internal_job", "optimus_analyzing"])
	def test_a_job_queued_from_inside_an_optimus_job_is_not_tagged(self, env, flag):  # noqa: F811
		setattr(env.fake.local, flag, True)
		env.enqueue("frappe.some.side_job")
		assert not _marked(env)
		assert _pending(env) == set()


class TestBeforeJob:
	@pytest.fixture
	def hc(self, env, monkeypatch):  # noqa: F811
		from optimus import hooks_callbacks

		# A stub module keeps the real optimus.settings out of sys.modules, where it
		# would outlive the test without an attribute on the restored package.
		settings = types.ModuleType("optimus.settings")
		settings.is_enabled = lambda: True
		monkeypatch.setitem(sys.modules, "optimus.settings", settings)
		monkeypatch.setattr(hooks_callbacks, "frappe", env.fake, raising=False)
		return hooks_callbacks

	def test_an_internal_job_flags_the_worker_and_drops_stale_markers(self, hc, env):  # noqa: F811
		kwargs = {"_profiler_session_id": "s", "_lp_session_id": "l", "session_uuid": "u"}
		hc.before_job(method="optimus.ai_jobs.run_ai_refresh_slice", kwargs=kwargs)
		assert kwargs == {"session_uuid": "u"}
		assert env.fake.local.optimus_internal_job is True

	def test_an_optimus_cron_is_internal(self, hc, env):  # noqa: F811
		kwargs = {"job_type": "optimus.ai_jobs.recover_pending", "_profiler_session_id": "s"}
		hc.before_job(method=RUN_SCHEDULED, kwargs=kwargs)
		assert "_profiler_session_id" not in kwargs
		assert env.fake.local.optimus_internal_job is True

	def test_internal_job_is_flagged_even_with_the_profiler_off(self, hc, env, monkeypatch):  # noqa: F811
		monkeypatch.setattr(sys.modules["optimus.settings"], "is_enabled", lambda: False)
		hc.before_job(method="optimus.janitor.sweep_stale_sessions", kwargs={})
		assert env.fake.local.optimus_internal_job is True

	def test_other_jobs_are_not_flagged(self, hc, env, monkeypatch):  # noqa: F811
		monkeypatch.setattr(hc, "_track_bg_job_started", lambda *a: None)
		hc.before_job(method="myapp.tasks.go", kwargs={})
		assert not hasattr(env.fake.local, "optimus_internal_job")

	def test_a_foreign_cron_is_flagged_so_its_child_is_not_tagged(self, hc, env):  # noqa: F811
		hc.before_job(method=RUN_SCHEDULED, kwargs={"job_type": "erpnext.accounts.tasks.daily"})
		assert env.fake.local.optimus_internal_job is True
		env.enqueue("erpnext.accounts.tasks.child")
		assert not _marked(env)
		assert _pending(env) == set()

	def test_a_foreign_cron_the_user_started_is_recorded_not_internal(self, hc, env, monkeypatch):  # noqa: F811
		# Execute on a Scheduled Job Type enqueues from the user's request, so the cron
		# arrives carrying the marker the enqueue wrapper injected.
		tracked = []
		monkeypatch.setattr(hc, "_track_bg_job_started", lambda *a: tracked.append(a))
		env.fake.local._recorder = object()  # stop at the recorder start, which needs real frappe
		kwargs = {"job_type": "erpnext.x", "_profiler_session_id": SESSION}
		hc.before_job(method=RUN_SCHEDULED, kwargs=kwargs)
		assert tracked == [(SESSION, RUN_SCHEDULED)]
		assert env.fake.local.optimus_session_id == SESSION
		assert "_profiler_session_id" not in kwargs
		assert not getattr(env.fake.local, "optimus_internal_job", False)

	def test_a_child_of_a_cron_the_user_started_is_still_tagged(self, hc, env, monkeypatch):  # noqa: F811
		monkeypatch.setattr(hc, "_track_bg_job_started", lambda *a: None)
		env.fake.local._recorder = object()
		hc.before_job(method=RUN_SCHEDULED, kwargs={"job_type": "erpnext.x", "_profiler_session_id": SESSION})
		env.enqueue("erpnext.accounts.tasks.child")
		assert env.calls[-1]["kwargs"]["_profiler_session_id"] == SESSION

	def test_a_cron_the_user_started_is_dropped_when_the_profiler_is_off(self, hc, env, monkeypatch):  # noqa: F811
		monkeypatch.setattr(sys.modules["optimus.settings"], "is_enabled", lambda: False)
		kwargs = {"job_type": "erpnext.x", "_profiler_session_id": SESSION}
		hc.before_job(method=RUN_SCHEDULED, kwargs=kwargs)
		assert kwargs == {"job_type": "erpnext.x"}
		assert not getattr(env.fake.local, "optimus_internal_job", False)

	def test_a_scheduler_cron_is_not_flagged_when_the_profiler_is_off(self, hc, env, monkeypatch):  # noqa: F811
		monkeypatch.setattr(sys.modules["optimus.settings"], "is_enabled", lambda: False)
		hc.before_job(method=RUN_SCHEDULED, kwargs={"job_type": "erpnext.x"})
		assert not hasattr(env.fake.local, "optimus_internal_job")

	def test_a_retried_cron_the_user_started_is_still_recorded(self, hc, env, monkeypatch):  # noqa: F811
		# Frappe re-runs a deadlocked job with the marker already popped.
		tracked = []
		monkeypatch.setattr(hc, "_track_bg_job_started", lambda *a: tracked.append(a))
		monkeypatch.setattr(hc._capture, "job_retry_state", lambda create=True: {"session_uuid": SESSION})
		env.fake.local._recorder = object()
		hc.before_job(method=RUN_SCHEDULED, kwargs={"job_type": "erpnext.x"})
		assert tracked == [(SESSION, RUN_SCHEDULED)]
		assert not getattr(env.fake.local, "optimus_internal_job", False)

	def test_a_retried_cron_with_no_session_stays_internal(self, hc, env, monkeypatch):  # noqa: F811
		tracked = []
		monkeypatch.setattr(hc, "_track_bg_job_started", lambda *a: tracked.append(a))
		monkeypatch.setattr(hc._capture, "job_retry_state", lambda create=True: {"lock_events": []})
		hc.before_job(method=RUN_SCHEDULED, kwargs={"job_type": "erpnext.x"})
		assert tracked == []
		assert env.fake.local.optimus_internal_job is True

	def test_a_foreign_cron_with_no_marker_is_not_recorded(self, hc, env, monkeypatch):  # noqa: F811
		tracked = []
		monkeypatch.setattr(hc, "_track_bg_job_started", lambda *a: tracked.append(a))
		hc.before_job(method=RUN_SCHEDULED, kwargs={"job_type": "erpnext.x"})
		assert tracked == []

	def test_after_a_foreign_cron_the_next_user_job_is_tagged_again(self, hc, env):  # noqa: F811
		kw = {"job_type": "erpnext.x"}
		hc.before_job(method=RUN_SCHEDULED, kwargs=kw)
		hc.after_job(method=RUN_SCHEDULED, kwargs=kw)
		env.enqueue("myapp.tasks.go")
		assert env.calls[-1]["kwargs"]["_profiler_session_id"] == SESSION

	def test_after_job_pops_only_what_its_own_before_job_pushed(self, hc, env):  # noqa: F811
		outer, inner = {}, {}
		hc.before_job(method="optimus.outer", kwargs=outer)
		# A user job whose before_job pushed nothing must not unwind the outer job.
		hc.before_job(method="myapp.tasks.go", kwargs=inner)
		hc.after_job(method="myapp.tasks.go", kwargs=inner)
		assert env.fake.local.optimus_internal_job is True
		hc.after_job(method="optimus.outer", kwargs=outer)
		assert env.fake.local.optimus_internal_job is False

	def test_an_inline_internal_job_leaves_the_requests_flag_as_it_found_it(self, hc, env):  # noqa: F811
		kw = {}
		hc.before_job(method="optimus.analyze.run", kwargs=kw)
		assert env.fake.local.optimus_internal_job is True
		hc.after_job(method="optimus.analyze.run", kwargs=kw)
		assert env.fake.local.optimus_internal_job is False
		env.enqueue("myapp.tasks.go")
		assert env.calls[-1]["kwargs"]["_profiler_session_id"] == SESSION

	def test_nested_inline_internal_jobs_unwind_in_order(self, hc, env):  # noqa: F811
		a, b = {}, {}
		hc.before_job(method="optimus.a", kwargs=a)
		hc.before_job(method="optimus.b", kwargs=b)
		hc.after_job(method="optimus.b", kwargs=b)
		assert env.fake.local.optimus_internal_job is True
		hc.after_job(method="optimus.a", kwargs=a)
		assert env.fake.local.optimus_internal_job is False

	def test_after_job_without_an_internal_job_changes_nothing(self, hc, env):  # noqa: F811
		hc.after_job(method="myapp.tasks.go", kwargs={})
		assert not hasattr(env.fake.local, "optimus_internal_job")
