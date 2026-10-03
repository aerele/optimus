"""Queue loss delays a SQL intent; it cannot erase or execute it inline."""

from importlib import import_module
from types import SimpleNamespace

import pytest

from optimus import ai_fix
from optimus.tests.test_ai_refresh_journal import journal as _journal_fixture  # noqa: F401


@pytest.fixture
def queue(journal, monkeypatch):
	jobs = import_module("optimus.ai_jobs")
	journal.run.update(lease_until=1800, dispatch_pending=1, dispatched_at_epoch=0)
	callbacks, sent, logs = [], [], []

	def transaction(fn):
		with journal.db.transaction():
			out = fn()
		for callback in callbacks[:]:
			callbacks.remove(callback)
			callback()
		return out

	def enqueue(method, **kw):
		assert kw["enqueue_after_commit"] is True and kw["is_async"] is True
		assert not kw.get("now")

		def deliver():
			assert not journal.db.active
			sent.append((method, kw))

		callbacks.append(deliver)

	def profiler_wrapper(*a, **kw):
		raise AssertionError("refresh job went through the profiler wrapper")

	profiler_wrapper.__wrapped__ = enqueue
	monkeypatch.setattr(jobs, "frappe", SimpleNamespace(db=journal.db, enqueue=profiler_wrapper, conf={}))
	monkeypatch.setattr(jobs, "_transaction", transaction)
	monkeypatch.setattr(jobs, "_now", lambda: 50)
	monkeypatch.setattr(jobs, "_call_timeout", lambda: 60)
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **kw: logs.append(type(a[1]).__name__))
	return SimpleNamespace(jobs=jobs, journal=journal, sent=sent, logs=logs, callbacks=callbacks)


def test_dispatch_runs_only_after_commit_and_bypasses_capture(queue):
	assert queue.jobs._dispatch_pending("fake-run") is True
	method, kw = queue.sent[0]
	assert method == "optimus.ai_jobs.run_ai_refresh_slice"
	assert kw["run_id"] == "fake-run" and kw["slice_no"] == 0
	assert kw["deduplicate"] is True and kw["timeout"] == 480
	assert queue.journal.run["dispatch_pending"] == 1
	assert queue.jobs._dispatch_pending("fake-run") is False
	assert len(queue.sent) == 1


def test_redis_error_keeps_intent_and_reports_failure(queue, monkeypatch):
	def fail(*a, **kw):
		raise ConnectionError("fake Redis down")

	monkeypatch.setattr(queue.jobs.frappe.enqueue, "__wrapped__", fail)
	assert queue.jobs._dispatch_pending("fake-run") is False
	assert queue.journal.run["state"] == "queued" and queue.journal.run["dispatch_pending"] == 1
	assert queue.logs == ["ConnectionError"] and not queue.sent


def test_worker_claim_is_the_only_delivery_acknowledgement(queue):
	queue.jobs._dispatch_pending("fake-run")
	with queue.journal.db.transaction():
		queue.journal.mod.claim("fake-run", slice_no=0, worker_token="worker", now=51, lease_seconds=300)
	assert queue.journal.run["dispatch_pending"] == 0
	assert queue.jobs._dispatch_pending("fake-run") is False


def test_after_commit_enqueue_failure_also_preserves_intent(queue, monkeypatch):
	def enqueue(*a, **kw):
		def fail():
			raise ConnectionError("fake after-commit queue failure")

		queue.callbacks.append(fail)

	monkeypatch.setattr(queue.jobs.frappe.enqueue, "__wrapped__", enqueue)
	assert queue.jobs._dispatch_pending("fake-run") is False
	assert queue.journal.run["dispatch_pending"] == 1 and queue.logs == ["ConnectionError"]


def test_public_progress_never_returns_internal_tokens_or_job_metadata(queue):
	run = {
		**queue.journal.run,
		"worker_token": "fake-worker-token",
		"job_id": "fake-job",
		"active_attempt": "fake-attempt",
		"uncertain": 1,
		"usage_incomplete": 1,
		"tokens_reported": 7,
	}
	out = queue.jobs.public_state(run)
	assert out["usage"] == {"tokens_reported": 7, "incomplete_attempts": 1}
	assert out["uncertain"] == 1 and out["state"] == "queued"
	assert not {"worker_token", "job_id", "active_attempt"}.intersection(out)
	assert "fake-worker-token" not in str(out)


def test_recovery_expires_a_lost_provider_call_without_requeueing_it(queue, monkeypatch):
	with queue.journal.db.transaction():
		queue.journal.mod.claim("fake-run", slice_no=0, worker_token="worker", now=10, lease_seconds=20)
		attempt = queue.journal.mod.begin_attempt(
			"fake-run", worker_token="worker", kind="fix", target="fake-finding", input_hash="fake", now=11
		)
	monkeypatch.setattr(queue.jobs.frappe, "get_all", queue.journal.db.get_all, raising=False)
	queue.jobs.recover_pending()
	assert queue.journal.run["state"] == "interrupted"
	assert queue.journal.db.rows[queue.journal.mod.ATTEMPT][attempt["name"]]["state"] == "uncertain"
	assert not queue.sent


def test_recovery_delivers_a_committed_intent_that_never_reached_redis(queue, monkeypatch):
	monkeypatch.setattr(queue.jobs.frappe, "get_all", queue.journal.db.get_all, raising=False)
	queue.jobs.recover_pending()
	assert len(queue.sent) == 1 and queue.journal.run["state"] == "queued"
	queue.jobs.recover_pending()
	assert len(queue.sent) == 1


def test_missing_worker_refuses_admission_without_creating_a_run(queue, monkeypatch):
	queue.journal.db.rows[queue.journal.mod.RUN] = {}
	monkeypatch.setattr(queue.jobs, "_workers_listening", lambda: False)
	monkeypatch.setattr(queue.jobs, "_check_run", lambda run: None)
	monkeypatch.setattr(ai_fix, "is_available", lambda: True)
	out = queue.jobs.start_refresh(
		docname="fake-session-doc", session_uuid="fake-session", requested_by="fake-owner"
	)
	assert out == {"status": "refused", "reason": "no_worker"}
	assert not queue.journal.db.rows[queue.journal.mod.RUN] and not queue.sent


def test_existing_refresh_is_visible_even_when_provider_or_queue_is_unavailable(queue, monkeypatch):
	monkeypatch.setattr(queue.jobs, "_check_run", lambda run: None)
	monkeypatch.setattr(ai_fix, "is_available", lambda: False)

	def unavailable():
		raise AssertionError("existing progress must not require Redis")

	monkeypatch.setattr(queue.jobs, "_workers_listening", unavailable)
	out = queue.jobs.start_refresh(
		docname="fake-session-doc", session_uuid="fake-session", requested_by="fake-owner"
	)
	assert out["status"] == "already_running" and out["refresh"]["run_id"] == "fake-run"
	assert len(queue.journal.db.rows[queue.journal.mod.RUN]) == 1 and not queue.sent


def test_start_commits_reservation_before_enqueuing(queue, monkeypatch):
	queue.journal.db.rows[queue.journal.mod.RUN] = {}
	monkeypatch.setattr(queue.jobs, "_workers_listening", lambda: True)
	monkeypatch.setattr(queue.jobs, "_check_run", lambda run: None)
	monkeypatch.setattr(ai_fix, "is_available", lambda: True)
	out = queue.jobs.start_refresh(
		docname="fake-session-doc", session_uuid="fake-session", requested_by="fake-owner"
	)
	assert out["status"] == "queued" and len(queue.sent) == 1
	assert out["refresh"]["state"] == "queued"
	assert len(queue.journal.db.rows[queue.journal.mod.RUN]) == 1


def test_recovery_handles_a_crash_between_admission_commit_and_dispatch(queue, monkeypatch):
	from optimus.tests.test_ai_refresh_journal import admit

	queue.journal.db.rows[queue.journal.mod.RUN] = {}
	admit(queue.journal)
	monkeypatch.setattr(queue.jobs.frappe, "get_all", queue.journal.db.get_all, raising=False)
	queue.jobs.recover_pending()
	assert len(queue.sent) == 1


def test_recovery_enforces_total_budget_even_if_queue_lease_is_still_alive(queue, monkeypatch):
	queue.journal.run.update(deadline=40, lease_until=1800)
	monkeypatch.setattr(queue.jobs.frappe, "get_all", queue.journal.db.get_all, raising=False)
	queue.jobs.recover_pending()
	assert queue.journal.run["state"] == "interrupted" and not queue.sent


def test_admission_honors_the_planned_site_configuration_names(queue, monkeypatch):
	queue.journal.db.rows[queue.journal.mod.RUN] = {}
	queue.jobs.frappe.conf.update(optimus_ai_refresh_max_seconds=900, optimus_ai_max_active_refreshes=1)
	monkeypatch.setattr(queue.jobs, "_workers_listening", lambda: True)
	monkeypatch.setattr(queue.jobs, "_check_run", lambda run: None)
	monkeypatch.setattr(ai_fix, "is_available", lambda: True)
	queue.jobs.start_refresh(
		docname="fake-session-doc", session_uuid="fake-session", requested_by="fake-owner"
	)
	assert next(iter(queue.journal.db.rows[queue.journal.mod.RUN].values()))["deadline"] == 950


@pytest.mark.parametrize("recover", [True, False])
def test_serialization_conflicts_have_a_bounded_sql_only_retry(queue, monkeypatch, recover):
	class SerializationFailure(Exception):
		pgcode = "40001"

	calls = []
	monkeypatch.setattr(queue.jobs.time, "sleep", lambda seconds: None)

	def operation():
		calls.append(True)
		if not recover or len(calls) == 1:
			raise SerializationFailure("fake concurrent update")
		return "ok"

	if recover:
		assert queue.jobs._retry_sql(operation) == "ok"
		assert len(calls) == 2
	else:
		with pytest.raises(SerializationFailure):
			queue.jobs._retry_sql(operation)
		assert len(calls) == 3
	assert not queue.sent


def test_nonserialization_errors_are_not_retried_as_admissions(queue):
	calls = []

	def fail():
		calls.append(True)
		raise ValueError("fake invalid input")

	with pytest.raises(ValueError):
		queue.jobs._retry_sql(fail)
	assert len(calls) == 1
