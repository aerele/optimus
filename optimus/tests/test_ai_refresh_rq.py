"""Every helper called before the queued provider request must honor RQ aborts."""

from types import SimpleNamespace

import pytest

from optimus import ai_fix, ai_jobs, analyze


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
