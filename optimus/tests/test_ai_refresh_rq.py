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
