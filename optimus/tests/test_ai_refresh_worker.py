"""Exercise worker delivery against the transactional journal, not a mock ledger."""

from importlib import import_module
from types import SimpleNamespace

import pytest

from optimus import ai_fix
from optimus.tests.test_ai_refresh_journal import journal as _journal_fixture  # noqa: F401

pytestmark = pytest.mark.rq


@pytest.fixture
def worker(journal, monkeypatch):
	jobs = import_module("optimus.ai_jobs")
	clock = [20.0]
	calls, written, queued, logs = [], [], [], []

	def transaction(fn):
		with journal.db.transaction():
			return fn()

	monkeypatch.setattr(jobs, "_transaction", transaction)
	monkeypatch.setattr(jobs, "_now", lambda: clock[0])
	monkeypatch.setattr(jobs.time, "monotonic", lambda: clock[0])
	monkeypatch.setattr(jobs, "_read_run", lambda rid: journal.db.get_value(journal.mod.RUN, rid, "*"))
	monkeypatch.setattr(jobs, "slice_seconds", lambda: 30)
	monkeypatch.setattr(jobs, "_call_timeout", lambda: 10)
	monkeypatch.setattr(jobs, "_check_run", lambda run, **kw: None)
	monkeypatch.setattr(jobs, "_dispatch_pending", queued.append)
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **kw: logs.append(type(a[1]).__name__))
	monkeypatch.setattr(
		jobs, "frappe", SimpleNamespace(db=SimpleNamespace(rollback=lambda: None), local=SimpleNamespace())
	)
	env = SimpleNamespace(
		journal=journal,
		jobs=jobs,
		calls=calls,
		written=written,
		clock=clock,
		queued=queued,
		logs=logs,
		count=5,
		send_hook=None,
		persist_hook=None,
		valid=True,
	)

	def next_item(run, memo):
		tried = {r["target_name"] for r in journal.db.rows.get(journal.mod.ATTEMPT, {}).values()}
		for i in range(env.count):
			name = f"finding-{i}"
			if name in tried:
				continue

			def send(timeout, name=name):
				assert not journal.db.active, "provider call held a SQL transaction"
				calls.append((name, timeout))
				attempts = list(journal.db.rows[journal.mod.ATTEMPT].values())
				assert attempts[-1]["state"] == "calling", "send intent must commit first"
				if env.send_hook:
					env.send_hook()
				return {"suggestion": "fake answer", "tokens": {"total_tokens": 7}, "usage_complete": True}

			def persist(result, name=name):
				assert journal.db.active
				if env.persist_hook:
					return env.persist_hook()
				written.append(name)
				journal.db.rows.setdefault("answers", {})[name] = result
				return True

			return jobs.PreparedItem("fix", name, "fake-hash", send, lambda: env.valid, persist)
		return None

	monkeypatch.setattr(jobs, "_next_item", next_item)
	return env


def test_replayed_rq_delivery_sends_and_accounts_each_item_once(worker):
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert len(worker.calls) == 5 and len(worker.written) == 5
	assert worker.journal.run["state"] == "complete"
	assert worker.journal.run["tokens_reported"] == 35
	assert worker.journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_refresh_count"] == 1


@pytest.mark.parametrize(
	"kind,expected", [("auth", 1), ("quota", 1), ("rate_limited", 1), ("timeout", 3), ("server", 3)]
)
def test_provider_failure_limits_survive_the_real_journal(worker, kind, expected):
	def fail():
		raise ai_fix.AiFixError("fake provider error", kind=kind)

	worker.send_hook = fail
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert len(worker.calls) == expected and not worker.written
	assert worker.journal.run["end_reason"] == "breaker"
	assert worker.journal.run["usage_incomplete"] == expected


def test_cancel_mid_call_keeps_late_usage_without_writing_answer(worker):
	def cancel():
		with worker.journal.db.transaction():
			worker.journal.mod.cancel("fake-run", now=21)

	worker.send_hook = cancel
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert len(worker.calls) == 1 and not worker.written
	assert worker.journal.run["state"] == "cancelled" and worker.journal.run["tokens_reported"] == 7


def test_slice_yields_after_an_item_and_old_delivery_cannot_repeat_it(worker):
	worker.send_hook = lambda: worker.clock.__setitem__(0, worker.clock[0] + 31)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert len(worker.calls) == 1 and worker.journal.run["slice_no"] == 1
	assert worker.queued == ["fake-run"]
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert len(worker.calls) == 1
	worker.jobs.run_ai_refresh_slice("fake-run", 1)
	assert len(worker.calls) == 2 and worker.journal.run["tokens_reported"] == 14


def test_deadline_prevents_another_provider_call(worker):
	worker.journal.run["deadline"] = 29
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert not worker.calls and worker.journal.run["end_reason"] == "deadline"


def test_stale_input_recheck_prevents_send(worker):
	worker.valid = False
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert not worker.calls and worker.journal.run["attempted"] == 0
	assert worker.journal.run["state"] == "queued"


def test_rejected_answer_retains_usage_and_does_not_report_success(worker):
	worker.count = 1
	worker.persist_hook = lambda: False
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert worker.journal.run["completed"] == 0 and worker.journal.run["skipped"] == 1
	assert worker.journal.run["tokens_reported"] == 7


def test_rq_timeout_marks_uncertain_and_escapes_fresh(worker):
	JobTimeoutException = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException

	def timeout():
		raise JobTimeoutException("fake interrupt")

	worker.send_hook = timeout
	with pytest.raises(JobTimeoutException) as caught:
		worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert caught.value.__context__ is None
	assert worker.journal.run["state"] == "interrupted"
	assert worker.journal.run["uncertain"] == 1
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert len(worker.calls) == 1


def test_interrupt_after_a_billed_response_retains_known_partial_usage(worker):
	JobTimeoutException = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException

	def timeout():
		exc = JobTimeoutException("fake interrupted reask")
		exc.usage = {"total_tokens": 7}
		exc.usage_complete = False
		raise exc

	worker.send_hook = timeout
	with pytest.raises(JobTimeoutException):
		worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert worker.journal.run["tokens_reported"] == 7
	assert worker.journal.run["usage_incomplete"] == 1 and worker.journal.run["uncertain"] == 1


@pytest.mark.parametrize("committed", [False, True])
def test_settlement_retry_never_repeats_provider_even_when_commit_ack_is_lost(worker, monkeypatch, committed):
	worker.count = 1
	transaction = worker.jobs._transaction
	failed = []

	def flaky(fn):
		before = worker.journal.db.rows[worker.journal.mod.RUN]["fake-run"]["completed"]
		if committed:
			result = transaction(fn)
			if (
				not before
				and worker.journal.db.rows[worker.journal.mod.RUN]["fake-run"]["completed"]
				and not failed
			):
				failed.append(True)
				raise OSError("fake commit acknowledgement lost")
			return result
		with worker.journal.db.transaction():
			result = fn()
			if (
				not before
				and worker.journal.db.rows[worker.journal.mod.RUN]["fake-run"]["completed"]
				and not failed
			):
				failed.append(True)
				raise OSError("fake commit failed")
			return result

	monkeypatch.setattr(worker.jobs, "_transaction", flaky)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert len(worker.calls) == 1 and len(failed) == 1
	run = worker.journal.db.rows[worker.journal.mod.RUN]["fake-run"]
	assert run["tokens_reported"] == 7 and run["completed"] == 1 and run["state"] == "complete"
	assert len(worker.journal.db.rows["answers"]) == 1


def test_worker_uses_the_capture_suppression_flag_and_restores_it(worker):
	seen = []
	worker.send_hook = lambda: seen.append(getattr(worker.jobs.frappe.local, "optimus_analyzing", False))
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert seen and all(seen)
	assert not getattr(worker.jobs.frappe.local, "optimus_analyzing", False)


def test_slow_input_preparation_cannot_spend_an_outdated_provider_budget(worker, monkeypatch):
	next_item = worker.jobs._next_item
	worker.journal.run["deadline"] = 200

	def slow(*a):
		item = next_item(*a)
		worker.clock[0] = 190
		return item

	monkeypatch.setattr(worker.jobs, "_next_item", slow)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert not worker.calls and worker.journal.run["end_reason"] == "deadline"


def test_unpersistable_answer_retains_usage_as_a_known_failed_outcome(worker):
	worker.count = 1

	def fail():
		raise ValueError("fake answer cannot be stored")

	worker.persist_hook = fail
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	run = worker.journal.db.rows[worker.journal.mod.RUN]["fake-run"]
	assert run["failed"] == 1 and run["tokens_reported"] == 7 and run["completed"] == 0
	assert run["usage_incomplete"] == 0


@pytest.mark.parametrize("break_at", ["rollback", "abandon", "log_commit"])
def test_cleanup_failure_does_not_replace_a_provider_interrupt(worker, monkeypatch, break_at):
	JobTimeoutException = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException

	def interrupted():
		raise JobTimeoutException("fake worker timeout")

	def disconnected(*a, **kw):
		raise ConnectionError("fake SQL outage during cleanup")

	worker.send_hook = interrupted
	if break_at == "rollback":
		monkeypatch.setattr(
			worker.jobs,
			"frappe",
			SimpleNamespace(
				db=SimpleNamespace(rollback=disconnected),
				local=SimpleNamespace(),
			),
		)
	elif break_at == "abandon":
		monkeypatch.setattr(worker.journal.mod, "abandon", disconnected)
	else:
		original = worker.jobs._transaction

		def transaction(operation):
			if worker.journal.run["state"] == "interrupted":
				return disconnected()
			return original(operation)

		monkeypatch.setattr(worker.jobs, "_transaction", transaction)
	with pytest.raises(JobTimeoutException) as error:
		worker.jobs.run_ai_refresh_slice("fake-run")
	assert error.value.__context__ is None and error.value.__cause__ is None
	assert len(worker.calls) == 1
	assert worker.jobs.frappe.local.optimus_analyzing is False
