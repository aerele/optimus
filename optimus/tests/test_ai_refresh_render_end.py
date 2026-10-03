"""Report recovery consumes saved answers without repeating provider requests."""

from importlib import import_module

import pytest

from optimus.tests.test_ai_refresh_journal import journal as _journal_fixture  # noqa: F401
from optimus.tests.test_ai_refresh_worker import worker as _worker_fixture  # noqa: F401

pytestmark = pytest.mark.rq


def test_worker_renders_committed_answers_before_releasing_reservation(worker, monkeypatch):
	seen = []
	def render(run_id, token):
		run = worker.journal.run
		assert run["state"] == "running" and run["render_pending"]
		assert len(worker.journal.db.rows["answers"]) == 5
		assert not worker.journal.db.active
		seen.append(run_id)
		with worker.journal.db.transaction():
			worker.journal.mod._update(worker.journal.mod.RUN, run, render_pending=0)
	monkeypatch.setattr(worker.jobs, "_render_pending", render)
	worker.jobs.run_ai_refresh_slice("fake-run")
	worker.jobs.run_ai_refresh_slice("fake-run")
	assert seen == ["fake-run"] and len(worker.calls) == 5
	assert worker.journal.run["state"] == "complete" and not worker.journal.run["render_pending"]


def test_render_failure_preserves_answers_usage_and_pending_recovery(worker, monkeypatch):
	def fail(*a):
		raise OSError("fake render failure")
	monkeypatch.setattr(worker.jobs, "_render_pending", fail)
	worker.jobs.run_ai_refresh_slice("fake-run")
	assert worker.journal.run["state"] == "stopped"
	assert worker.journal.run["end_reason"] == "render_failed"
	assert worker.journal.run["render_pending"] == 1
	assert worker.journal.run["tokens_reported"] == 35
	assert len(worker.journal.db.rows["answers"]) == 5
	assert worker.logs == ["OSError"]
	worker.jobs.run_ai_refresh_slice("fake-run")
	assert len(worker.calls) == 5


def test_render_timeout_preserves_saved_answers_and_escapes_fresh(worker, monkeypatch):
	JobTimeoutException = pytest.importorskip("rq.timeouts").JobTimeoutException
	def fail(*a):
		raise JobTimeoutException("fake render timeout")
	monkeypatch.setattr(worker.jobs, "_render_pending", fail)
	with pytest.raises(JobTimeoutException) as caught:
		worker.jobs.run_ai_refresh_slice("fake-run")
	assert caught.value.__context__ is None
	assert worker.journal.run["render_pending"] == 1
	assert worker.journal.run["uncertain"] == 0
	assert worker.journal.run["tokens_reported"] == 35
	assert len(worker.journal.db.rows["answers"]) == 5


@pytest.mark.parametrize("reason", ["deadline", "permission", "not_ready", "phase2"])
def test_unsafe_terminal_state_does_not_attempt_render(worker, monkeypatch, reason):
	monkeypatch.setattr(worker.jobs, "_render_pending", lambda *a: pytest.fail("unsafe render"))
	with worker.journal.db.transaction():
		worker.journal.mod.claim("fake-run", slice_no=0, worker_token="worker", now=20, lease_seconds=200)
	worker.jobs._end("fake-run", "worker", reason)
	assert worker.journal.run["end_reason"] == reason


def test_pending_report_from_previous_run_is_recovered_without_a_new_answer(worker, monkeypatch):
	# Exercise the production helper, not the worker fixture's rendering seam.
	jobs = worker.jobs
	render = import_module("optimus.report_refresh")
	seen = []
	monkeypatch.setattr(render, "render_report", lambda *a, **kw: seen.append((a, kw)))
	monkeypatch.setattr(jobs.frappe, "db", worker.journal.db)
	with worker.journal.db.transaction():
		worker.journal.mod.claim("fake-run", slice_no=0, worker_token="worker", now=20, lease_seconds=200)
	worker.journal.db.rows[worker.journal.mod.RUN]["old-run"] = {
		"name": "old-run", "session_name": "fake-session-doc", "render_pending": 1,
	}
	worker.render_pending("fake-run", "worker")
	assert seen == [(("fake-session-doc",), {"run_id": "fake-run", "worker_token": "worker"})]
