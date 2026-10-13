"""Every helper called before the queued provider request must honor RQ aborts."""

# ruff: noqa: F811  (the imported fixtures are used by name as parameters)
import sys
from types import SimpleNamespace

import pytest

from optimus import ai_fix, ai_jobs, analyze
from optimus.tests.test_ai_refresh_journal import journal as _journal_fixture  # noqa: F401
from optimus.tests.test_ai_refresh_worker import worker  # noqa: F401


@pytest.mark.parametrize("kind", ["rq", "exit"])
def test_sql_rollback_failure_preserves_the_original_interrupt(monkeypatch, kind):
	JobTimeoutException = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = JobTimeoutException("fake timeout") if kind == "rq" else SystemExit()

	def interrupted():
		raise original

	def disconnected():
		raise ConnectionError("fake disconnected rollback")

	monkeypatch.setattr(ai_jobs, "frappe", SimpleNamespace(db=SimpleNamespace(rollback=disconnected)))
	with pytest.raises(type(original)) as caught:
		ai_jobs._transaction(interrupted)
	assert caught.value.__context__ is None and caught.value.__cause__ is None
	assert (caught.value is original) is (kind == "exit")


def test_phase2_grounding_does_not_swallow_the_workers_timeout(monkeypatch):
	JobTimeoutException = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = JobTimeoutException("fake grounding timeout")

	def fail(*a):
		raise original

	monkeypatch.setattr(analyze.renderer, "_build_line_drilldown_callsite_index", fail)
	with pytest.raises(JobTimeoutException) as caught:
		analyze._phase2_index_for(SimpleNamespace())
	assert caught.value is not original and caught.value.__context__ is None


def test_missing_phase2_context_is_reported_without_breaking_enrichment(monkeypatch):
	logs = []

	def fail(*a):
		raise ValueError("fake corrupt context")

	monkeypatch.setattr(analyze.renderer, "_build_line_drilldown_callsite_index", fail)
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **kw: logs.append(type(a[1]).__name__))
	assert analyze._phase2_index_for(SimpleNamespace()) == {}
	assert logs == ["ValueError"]


@pytest.mark.parametrize("helper", ["attempt insert", "site mutex"])
def test_a_job_timeout_inside_a_journal_lock_or_insert_escapes_fresh(monkeypatch, helper):
	"""The duplicate-key and busy-mutex handlers catch ordinary errors only: an RQ timeout
	during the attempt INSERT or the NOWAIT mutex read must stop the job, raised again as a
	fresh instance, never read as "already attempted" or "busy"."""
	from optimus import ai_refresh_store as store

	JobTimeoutException = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = JobTimeoutException("fake job timeout")

	def interrupted(*a, **kw):
		raise original

	if helper == "attempt insert":
		monkeypatch.setattr(store, "_insert", interrupted)

		def call():
			return store._insert_new(store.ATTEMPT, "fake-attempt", attempt_id="fake-attempt")
	else:
		monkeypatch.setattr(store, "frappe", SimpleNamespace(db=SimpleNamespace(get_value=interrupted)))
		call = store._lock_site_mutex
	with pytest.raises(JobTimeoutException) as caught:
		call()
	assert caught.value is not original
	assert caught.value.__context__ is None and caught.value.__cause__ is None


@pytest.mark.parametrize("entry", ["recovery sweep", "cancel"])
def test_a_job_timeout_escapes_the_failure_isolation_fresh(monkeypatch, entry):
	"""The sweep keeps one row's ordinary failure from stopping the rest, and cancel turns a
	lock wait into a busy refusal; an RQ timeout in either must still stop the job, fresh."""
	from optimus import ai_refresh_store as store

	JobTimeoutException = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = JobTimeoutException("fake job timeout")

	def interrupted(*a, **kw):
		raise original

	monkeypatch.setattr(
		ai_jobs,
		"frappe",
		SimpleNamespace(
			get_all=lambda *a, **kw: [{"name": "fake-run", "session_name": "fake-doc"}],
			db=SimpleNamespace(rollback=lambda: None),
		),
	)
	monkeypatch.setattr(ai_jobs, "_transaction", lambda operation: operation())
	monkeypatch.setattr(store, "interrupt", interrupted)
	monkeypatch.setattr(store, "cancel", interrupted)
	with pytest.raises(JobTimeoutException) as caught:
		if entry == "cancel":
			ai_jobs.cancel_refresh("fake-run", docname="fake-doc", requested_by="fake-user")
		else:
			ai_jobs.recover_pending()
	assert caught.value is not original
	assert caught.value.__context__ is None and caught.value.__cause__ is None


_TIMED_OUT = "unknown: the job timed out during the outcome write; the attempt is now uncertain"


@pytest.mark.parametrize("where", ["answer write", "fallback write"])
def test_a_job_timeout_during_the_outcome_write_keeps_the_known_tokens_and_escapes_fresh(
	worker, monkeypatch, where
):
	"""The reply came back with its tokens; the outcome write then waited on lock waits and the
	RQ job timeout fired inside it (in the answer's write, or in the fallback after the answer's
	write failed). Both guards must raise it again, fresh, and one row, written while no
	exception is being handled, must keep the tokens: no outcome was saved, and the cleanup
	leaves the attempt uncertain."""
	JobTimeoutException = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = JobTimeoutException("fake: the job timed out while the outcome write waited on a lock")
	worker.count = 1
	worker.tokens = 5000
	writes = []

	def settle(*a, **kw):
		writes.append(kw["outcome"])
		if where == "fallback write" and len(writes) == 1:
			raise OSError("fake: the answer's write failed")
		raise original

	rows = []

	def log(title, exc=None, **context):
		rows.append((title, exc, context, sys.exc_info()[1]))

	monkeypatch.setattr(worker.journal.mod, "settle_attempt", settle)
	monkeypatch.setattr(ai_fix, "log_ai_failure", log)
	with pytest.raises(JobTimeoutException) as caught:
		worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert caught.value is not original
	assert caught.value.__context__ is None and caught.value.__cause__ is None
	assert len(worker.calls) == 1 and writes == (
		["succeeded", "failed"] if where == "fallback write" else ["succeeded"]
	)
	[attempt] = worker.journal.db.rows[worker.journal.mod.ATTEMPT].values()
	[(exc, context)] = [(r[1], r[2]) for r in rows if r[0] == "optimus ai refresh result storage"]
	assert exc is None and context["outcome"] == _TIMED_OUT
	assert type(context["tokens"]) is int and context["tokens"] == 5000
	assert context["docname"] == "fake-session-doc" and context["run_id"] == "fake-run"
	assert context["attempt"] == attempt["name"]
	assert [r for r in rows if r[3] is not None] == []
	run = worker.journal.db.rows[worker.journal.mod.RUN]["fake-run"]
	assert attempt["state"] == "uncertain" and run["state"] == "interrupted"
	assert run["tokens_reported"] == 0 and run["uncertain"] == 1
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert len(worker.calls) == 1


def test_the_outcome_write_raises_a_job_timeout_again_fresh(monkeypatch):
	"""The outcome write's own guard: a timeout inside the settlement transaction is never
	read as a committed or a failed write."""
	JobTimeoutException = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = JobTimeoutException("fake job timeout")

	def interrupted():
		raise original

	monkeypatch.setattr(ai_jobs, "_transaction", lambda operation: operation())
	with pytest.raises(JobTimeoutException) as caught:
		ai_jobs._write_outcome(interrupted, "fake-attempt")
	assert caught.value is not original
	assert caught.value.__context__ is None and caught.value.__cause__ is None


def test_a_job_timeout_after_the_outcome_is_saved_writes_no_unknown_row(worker, monkeypatch):
	"""The outcome committed; the timeout fired afterwards, while its rows were written. The
	tokens are saved, so no row may call them unknown."""
	JobTimeoutException = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	worker.count = 1
	rows = []

	def log(title, exc=None, **context):
		rows.append((title, context))
		if title == "optimus ai refresh result storage" and len(rows) == 1:
			raise JobTimeoutException("fake: the job timed out while a row was written")

	transaction = worker.jobs._transaction
	committed = []

	def commit_then_fail(operation):
		out = transaction(operation)
		if operation.__name__ == "settle" and not committed:
			committed.append(True)
			raise OSError("fake: the COMMIT reported an error after it went through")
		return out

	monkeypatch.setattr(worker.jobs, "_transaction", commit_then_fail)
	monkeypatch.setattr(ai_fix, "log_ai_failure", log)
	with pytest.raises(JobTimeoutException):
		worker.jobs.run_ai_refresh_slice("fake-run", 0)
	run = worker.journal.db.rows[worker.journal.mod.RUN]["fake-run"]
	assert committed and run["completed"] == 1 and run["tokens_reported"] == 7
	assert [c["outcome"] for t, c in rows if t == "optimus ai refresh result storage"] == [
		ai_jobs._COMMITTED_AFTER_ERROR
	]
