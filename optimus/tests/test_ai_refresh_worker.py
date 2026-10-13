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
	calls, written, queued, logs, rows = [], [], [], [], []

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
	monkeypatch.setattr(jobs, "_dispatch_pending", lambda run_id, docname: queued.append(run_id))

	def log_row(title, exc=None, **context):
		rows.append((title, context))
		if exc is not None:
			logs.append(type(exc).__name__)

	monkeypatch.setattr(ai_fix, "log_ai_failure", log_row)
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
		rows=rows,
		count=5,
		tokens=7,
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
				return {
					"suggestion": "fake answer",
					"tokens": {"total_tokens": env.tokens},
					"usage_complete": True,
				}

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
	# Q1: a run's ending counts nothing; the admission counted the refresh.
	assert worker.journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_refresh_count"] == 0


@pytest.mark.parametrize(
	"kind,expected",
	[
		("auth", 1),
		("quota", 1),
		("not_found", 1),
		("config", 1),
		("rate_limited", 3),
		("timeout", 3),
		("server", 3),
	],
)
def test_provider_failure_limits_survive_the_real_journal(worker, kind, expected):
	def fail():
		raise ai_fix.AiFixError("fake provider error", kind=kind)

	worker.send_hook = fail
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert len(worker.calls) == expected and not worker.written
	assert worker.journal.run["end_reason"] == "breaker"
	assert worker.journal.run["breaker_kind"] == kind
	assert worker.journal.run["usage_incomplete"] == expected


def test_cancel_mid_call_keeps_late_usage_without_writing_answer(worker):
	def cancel():
		with worker.journal.db.transaction():
			worker.journal.mod.cancel("fake-run", docname="fake-session-doc", now=21)

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


def test_a_worker_on_postgres_sets_no_lock_bound(worker):
	"""The HTTP lock bound is not the worker's: its claim, sends, settlements and ending keep
	the server's setting and retry a lock wait."""
	statements = []
	worker.journal.db.db_type = "postgres"
	worker.jobs.frappe.db.db_type = "postgres"
	worker.jobs.frappe.db.sql = lambda query, values=(): statements.append(query)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert worker.journal.run["state"] == "complete" and len(worker.calls) == 5
	assert statements == [] and not [c for c in worker.journal.db.calls if c[0] == "SQL"]


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


class _Conflict(Exception):
	"""What Frappe raises when the database ended the whole transaction:
	``QueryDeadlockError(e)`` around MariaDB 1213 or 1020."""


class _DriverError(Exception):
	"""PyMySQL's error shape: ``args == (code, message)``."""


def _deadlock():
	return _Conflict(_DriverError(1213, "fake deadlock"))


def _settle_failing(worker, monkeypatch, *, failures, committed, error=_deadlock):
	"""Fail the transaction that would complete the first answer ``failures`` times: before
	the COMMIT (rolled back) or after it (``committed``: an after_commit error)."""
	transaction = worker.jobs._transaction
	failed = []

	def flaky(fn):
		run = worker.journal.db.rows[worker.journal.mod.RUN]["fake-run"]
		before = run["completed"]
		if committed:
			result = transaction(fn)
			if not before and run["completed"] and len(failed) < failures:
				failed.append(True)
				raise error()
			return result
		with worker.journal.db.transaction():
			result = fn()
			if not before and run["completed"] and len(failed) < failures:
				failed.append(True)
				raise error()
			return result

	monkeypatch.setattr(worker.jobs, "_transaction", flaky)
	return failed


@pytest.fixture
def rows(worker, monkeypatch):
	"""Every Error Log write as (title, exception, context)."""
	logged = []
	monkeypatch.setattr(
		ai_fix, "log_ai_failure", lambda title, exc=None, **kw: logged.append((title, exc, kw))
	)
	return logged


def test_settlement_retries_a_whole_transaction_rollback_without_repeating_the_provider(
	worker, monkeypatch, rows
):
	worker.count = 1
	failed = _settle_failing(worker, monkeypatch, failures=2, committed=False)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert len(worker.calls) == 1 and len(failed) == 2
	run = worker.journal.db.rows[worker.journal.mod.RUN]["fake-run"]
	assert run["tokens_reported"] == 7 and run["completed"] == 1 and run["state"] == "complete"
	assert worker.journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_tokens_spent"] == 7
	assert len(worker.journal.db.rows["answers"]) == 1
	assert not rows


def test_settlement_gives_up_after_the_third_conflict_and_keeps_the_known_usage(worker, monkeypatch, rows):
	worker.count = 1
	failed = _settle_failing(worker, monkeypatch, failures=3, committed=False)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert len(worker.calls) == 1 and len(failed) == 3
	run = worker.journal.db.rows[worker.journal.mod.RUN]["fake-run"]
	assert run["failed"] == 1 and run["completed"] == 0 and run["tokens_reported"] == 7
	assert "answers" not in worker.journal.db.rows
	[(title, exc, context)] = rows
	assert type(exc) is _Conflict and context["tries"] == 3 and context["tokens"] == 7
	assert context["outcome"] == "not committed after try 3; recorded by fallback"
	assert context["run_id"] == "fake-run" and context["docname"] == "fake-session-doc"


def test_an_error_after_the_commit_is_read_back_as_a_saved_outcome(worker, monkeypatch, rows):
	worker.count = 1
	failed = _settle_failing(
		worker, monkeypatch, failures=1, committed=True, error=lambda: OSError("fake after_commit failure")
	)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert len(worker.calls) == 1 and len(failed) == 1
	run = worker.journal.db.rows[worker.journal.mod.RUN]["fake-run"]
	assert run["tokens_reported"] == 7 and run["completed"] == 1 and run["state"] == "complete"
	assert len(worker.journal.db.rows["answers"]) == 1
	[(title, exc, context)] = rows
	assert type(exc) is OSError and context["outcome"].startswith("committed")


def test_an_error_before_the_commit_that_is_no_conflict_is_not_retried(worker, monkeypatch, rows):
	"""#72's rule: only a rolled-back conflict is tried again; the answer is then not saved,
	its known usage is, and the finding counts as failed."""
	worker.count = 1
	failed = _settle_failing(
		worker, monkeypatch, failures=1, committed=False, error=lambda: OSError("fake commit failure")
	)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert len(worker.calls) == 1 and len(failed) == 1
	run = worker.journal.db.rows[worker.journal.mod.RUN]["fake-run"]
	assert run["failed"] == 1 and run["completed"] == 0 and run["tokens_reported"] == 7
	assert worker.journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_tokens_spent"] == 7
	assert "answers" not in worker.journal.db.rows
	[(title, exc, context)] = rows
	assert type(exc) is OSError and context["tries"] == 1 and context["tokens"] == 7
	assert context["outcome"] == "not committed after try 1; recorded by fallback"


@pytest.mark.parametrize("recovers", [True, False])
def test_every_counter_row_the_counter_logged_gets_an_outcome(worker, monkeypatch, rows, recovers):
	"""The session counter logs its own row (``savepoint_rollback=failed``) and raises when the
	whole transaction is gone; that row must not read as lost tokens once a retry commits, and
	when the answer's write gives up and the failed-outcome write then records the tokens,
	every row says so, so a reconciler never adds them a second time."""
	worker.count = 1
	failures = 1 if recovers else 3
	errors = []
	db = worker.journal.db

	def spend(name, tokens):
		if len(errors) < failures:
			exc = _deadlock()
			setattr(exc, ai_fix._LOGGED_ATTR, True)
			errors.append(exc)
			raise exc
		db.increment(name, "ai_tokens_spent", tokens)

	monkeypatch.setattr("optimus.analyze._add_ai_spend", spend)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert len(worker.calls) == 1
	outcomes = [context.get("outcome") for title, exc, context in rows if exc in errors]
	if recovers:
		assert outcomes == ["committed on try 2"]
		assert db.rows[worker.journal.mod.RUN]["fake-run"]["completed"] == 1
	else:
		assert outcomes == ["not committed after try 3; recorded by fallback"] * 3
		assert [context["tokens"] for title, exc, context in rows if exc is errors[-1]] == [7]
		assert db.rows[worker.journal.mod.RUN]["fake-run"]["failed"] == 1
	assert db.rows["Optimus Session"]["fake-session-doc"]["ai_tokens_spent"] == 7


def test_a_fallback_that_finds_the_outcome_already_saved_attributes_it_to_the_first_write(
	worker, monkeypatch, rows
):
	"""The first write committed but its COMMIT raised and the read-back failed: the
	failed-outcome write then finds the attempt settled (a no-op), so the rows say the
	outcome is saved, never that the fallback recorded the tokens."""
	worker.count = 1
	failed = _settle_failing(
		worker, monkeypatch, failures=1, committed=True, error=lambda: OSError("fake after_commit failure")
	)
	real = worker.journal.mod.attempt_settled
	reads = []

	def first_read_fails(attempt_id):
		reads.append(attempt_id)
		if len(reads) == 1:
			raise ConnectionError("fake lost connection")
		return real(attempt_id)

	monkeypatch.setattr(worker.journal.mod, "attempt_settled", first_read_fails)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert len(worker.calls) == 1 and len(failed) == 1 and len(reads) == 2
	run = worker.journal.db.rows[worker.journal.mod.RUN]["fake-run"]
	assert run["completed"] == 1 and run["failed"] == 0 and run["tokens_reported"] == 7
	assert worker.journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_tokens_spent"] == 7
	[(title, exc, context)] = rows
	assert type(exc) is OSError and context["outcome"].startswith("recorded")


def _first_write_fails(worker, monkeypatch):
	"""Every item's first settlement write fails with an ordinary error (not a conflict, so it
	is not tried again); the fallback write, a fresh transaction, goes through. Returns the
	``(outcome, error_kind)`` each fallback wrote."""
	real = worker.journal.mod.settle_attempt
	seen, written = set(), []

	def settle(run_id, attempt_id, **kw):
		if attempt_id not in seen:
			seen.add(attempt_id)
			raise OSError("fake: the first settlement write failed")
		written.append((kw["outcome"], kw["error_kind"]))
		return real(run_id, attempt_id, **kw)

	monkeypatch.setattr(worker.journal.mod, "settle_attempt", settle)
	return written


def _attempt_outcomes(worker):
	return [
		(a["state"], a["error_kind"]) for a in worker.journal.db.rows[worker.journal.mod.ATTEMPT].values()
	]


def test_a_fallback_keeps_an_uncertain_call_uncertain(worker, monkeypatch):
	"""The job timed out during the provider call, so the call's outcome is unknown. When the
	first write of that outcome failed, the fallback recorded a definite failure, which a
	resume or a new refresh would then send again."""
	JobTimeoutException = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	worker.count = 1

	def timeout():
		raise JobTimeoutException("fake: the job timeout fired during the provider call")

	worker.send_hook = timeout
	written = _first_write_fails(worker, monkeypatch)
	with pytest.raises(JobTimeoutException):
		worker.jobs.run_ai_refresh_slice("fake-run", 0)
	run = worker.journal.db.rows[worker.journal.mod.RUN]["fake-run"]
	assert written == [("uncertain", "timeout")]
	assert _attempt_outcomes(worker) == [("uncertain", "timeout")]
	assert run["uncertain"] == 1 and run["failed"] == 0 and run["usage_incomplete"] == 1


@pytest.mark.parametrize(
	"kind,state", [("context", "failed"), ("refused", "failed"), ("not_eligible", "skipped")]
)
def test_a_fallback_keeps_the_kind_of_a_per_prompt_failure_or_a_skip(worker, monkeypatch, kind, state):
	"""Q2: a per-prompt failure is recorded under its own kind and never feeds the three-in-a-
	row stop, and a skip stays a skip. The fallback relabelled each as an ``internal``
	failure, so three of them stopped the run."""
	worker.count = 3

	def fail():
		raise ai_fix.AiFixError("fake provider error", kind=kind)

	worker.send_hook = fail
	written = _first_write_fails(worker, monkeypatch)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	run = worker.journal.db.rows[worker.journal.mod.RUN]["fake-run"]
	assert written == [(state, kind)] * 3 and _attempt_outcomes(worker) == [(state, kind)] * 3
	assert len(worker.calls) == 3 and run["end_reason"] == "complete" and not run.get("breaker_kind")
	assert run["consecutive_failures"] == 0


@pytest.mark.parametrize("kind", sorted(ai_fix.AI_FATAL_KINDS))
def test_a_fallback_keeps_a_fatal_kind_stopping_the_run_at_once(worker, monkeypatch, kind):
	worker.count = 5

	def fail():
		raise ai_fix.AiFixError("fake provider error", kind=kind)

	worker.send_hook = fail
	written = _first_write_fails(worker, monkeypatch)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	run = worker.journal.db.rows[worker.journal.mod.RUN]["fake-run"]
	assert written == [("failed", kind)] and len(worker.calls) == 1
	assert run["end_reason"] == "breaker" and run["breaker_kind"] == kind


def test_a_fallback_after_a_successful_reply_records_a_failure_without_the_answer(worker, monkeypatch):
	worker.count = 1
	written = _first_write_fails(worker, monkeypatch)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	run = worker.journal.db.rows[worker.journal.mod.RUN]["fake-run"]
	assert written == [("failed", "internal")] and _attempt_outcomes(worker) == [("failed", "internal")]
	assert not worker.written and run["completed"] == 0 and run["tokens_reported"] == 7


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


def test_a_run_whose_item_never_begins_stops_instead_of_respinning(worker):
	"""O5: each slice selected the item again, could not begin it and yielded; with the
	continuation now dispatched at once that was a hot loop until the deadline."""
	worker.valid = False
	for slice_no in range(3):
		worker.jobs.run_ai_refresh_slice("fake-run", slice_no)
	run = worker.journal.run
	assert run["state"] == "stopped" and run["end_reason"] == "no_progress"
	assert run["active_session"] is None and run["attempted"] == 0
	assert not worker.calls and worker.queued == ["fake-run", "fake-run"]


def test_switching_ai_off_mid_run_stops_it_without_counting_a_refresh(worker, monkeypatch):
	"""E7 / C4: the run ended complete and bumped the session's refresh count."""
	worker.count = 3
	select = worker.jobs._next_item

	def next_item(run, memo):
		if worker.calls:
			raise worker.jobs.StopRefresh("disabled")
		return select(run, memo)

	monkeypatch.setattr(worker.jobs, "_next_item", next_item)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	run = worker.journal.run
	assert run["state"] == "stopped" and run["end_reason"] == "disabled" and run["completed"] == 1
	assert worker.journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_refresh_count"] == 0


def test_when_both_settlement_writes_fail_the_final_row_carries_tokens_and_outcome(worker, monkeypatch, rows):
	"""R4 / C3: the error escapes to the worker cleanup, which makes the attempt uncertain;
	that last row says what the reported tokens became."""
	worker.count = 1

	def outage(*a, **kw):
		raise OSError("fake storage outage")

	monkeypatch.setattr(worker.journal.mod, "settle_attempt", outage)
	with pytest.raises(worker.jobs.RefreshSliceFailed):
		worker.jobs.run_ai_refresh_slice("fake-run", 0)
	[(title, exc, context)] = [row for row in rows if row[0] == "optimus ai refresh worker"]
	assert type(exc) is OSError and context["tokens"] == 7
	assert context["outcome"] == "not committed after try 1; the attempt is now uncertain"
	run = worker.journal.db.rows[worker.journal.mod.RUN]["fake-run"]
	assert run["state"] == "interrupted" and run["uncertain"] == 1 and run["tokens_reported"] == 0


def test_when_the_cleanup_also_fails_the_final_row_leaves_the_attempt_to_recovery(worker, monkeypatch, rows):
	worker.count = 1

	def outage(*a, **kw):
		raise OSError("fake storage outage")

	monkeypatch.setattr(worker.journal.mod, "settle_attempt", outage)
	monkeypatch.setattr(worker.journal.mod, "abandon", outage)
	with pytest.raises(worker.jobs.RefreshSliceFailed):
		worker.jobs.run_ai_refresh_slice("fake-run", 0)
	[(title, exc, context)] = [row for row in rows if row[0] == "optimus ai refresh worker"]
	assert context["tokens"] == 7 and context["outcome"] == "not committed after try 1"


def test_a_begin_that_used_up_the_budget_sends_nothing(worker, monkeypatch):
	"""M35: the deadline is checked again after the send intent commits; a call started
	below the reserve could not settle before the deadline fences it."""
	worker.count = 1
	worker.journal.run["deadline"] = 60
	begin = worker.journal.mod.begin_attempt

	def slow(*a, **kw):
		out = begin(*a, **kw)
		worker.clock[0] = 47  # 60 - 47 - 5 = 8 seconds left, under min(15, 10)
		return out

	monkeypatch.setattr(worker.journal.mod, "begin_attempt", slow)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert not worker.calls and worker.journal.run["end_reason"] == "deadline"
	[attempt] = worker.journal.db.rows[worker.journal.mod.ATTEMPT].values()
	assert attempt["state"] == "skipped" and attempt["error_kind"] == "deadline"
	assert attempt["settled"] == 1 and attempt["usage_complete"] == 1
	assert worker.journal.run["usage_incomplete"] == 0


def test_a_refusal_before_any_http_call_is_a_known_zero_in_the_run(worker, monkeypatch):
	"""C3: a prompt refused before sending counted as incomplete usage, which inflated the
	release-monitoring signal for usage the provider never billed."""

	def refuse(*a, **kw):
		raise ai_fix.AiFixError("fake: the prompt does not fit", kind="context")

	sent = []
	for name, value in {
		"is_finding_type_excluded": lambda finding_type: False,
		"llm_gate_note": lambda finding: None,
		"_provider_config": lambda: {"model": "m", "base_url": "https://fake.invalid", "name": "p"},
		"_require_configured": lambda provider: None,
		"_context_tokens": lambda provider: 1000,
		"_output_tokens": lambda provider: 100,
		"_resolve_display_threshold_ms": lambda: 1000.0,
		"_build_fix_request": lambda *a, **kw: ("system", [], []),
		"_is_hosted_endpoint": lambda url: False,
		"_check_context_fits": refuse,
		"_complete_with_guardrails": lambda *a, **kw: sent.append(True),
	}.items():
		monkeypatch.setattr(ai_fix, name, value)
	select = worker.jobs._next_item

	def next_item(run, memo):
		item = select(run, memo)
		if item is None:
			return None

		def send(timeout):
			return worker.jobs._suggest_fix({"finding_type": "N+1 Query"}, timeout=timeout, run=run)

		return worker.jobs.PreparedItem(
			item.kind, item.target, item.input_hash, send, item.valid, item.persist
		)

	monkeypatch.setattr(worker.jobs, "_next_item", next_item)
	worker.count = 2
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert not sent and worker.journal.run["attempted"] == 2
	assert worker.journal.run["usage_incomplete"] == 0
	# recorded under its own kind (not internal), and never feeding the streak
	assert {a["error_kind"] for a in worker.journal.db.rows[worker.journal.mod.ATTEMPT].values()} == {
		"context"
	}
	assert worker.journal.run["consecutive_failures"] == 0 and not worker.journal.run.get("breaker_kind")
	assert all(a["usage_complete"] == 1 for a in worker.journal.db.rows[worker.journal.mod.ATTEMPT].values())


def test_a_failure_from_outside_the_usage_boundary_is_unknown_usage(worker, monkeypatch):
	"""F2 (probe U2): the HTTP layer's own timeout failure reaching the engine without a call
	count is a sent call of unknown usage, never a known zero of 0 tokens."""
	import frappe

	monkeypatch.setattr(frappe, "_", lambda text, *a, **kw: text)

	def timed_out():
		raise ai_fix._timeout_failure(10)

	worker.count = 1
	worker.send_hook = timed_out
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	[attempt] = worker.journal.db.rows[worker.journal.mod.ATTEMPT].values()
	assert attempt["usage_complete"] == 0 and attempt["tokens_reported"] == 0
	assert worker.journal.run["usage_incomplete"] == 1


def test_a_slice_that_answered_an_item_is_not_idle_when_the_next_cannot_begin(worker):
	"""F6 (mutant Q1): only a slice that began no item adds to the idle streak; a healthy run
	whose next input changed must not drift towards no_progress."""
	worker.count = 2
	worker.send_hook = lambda: setattr(worker, "valid", False)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	run = worker.journal.run
	assert len(worker.calls) == 1 and run["completed"] == 1
	assert run["state"] == "queued" and run["slice_no"] == 1 and run["idle_slices"] == 0


# --- release-blocking signals (O4): one Error Log row each, at the moment it happens -----------


def _signal_rows(worker, title):
	return [context for t, context in worker.rows if t == title]


def test_a_normal_run_writes_no_signal_row(worker):
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert not [t for t, _ in worker.rows if t.startswith("optimus ai refresh d")]


def test_an_answer_rejected_by_its_input_check_is_a_discarded_row(worker):
	worker.count = 1
	worker.persist_hook = lambda: False
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	[row] = _signal_rows(worker, "optimus ai refresh discarded")
	attempt = next(iter(worker.journal.db.rows[worker.journal.mod.ATTEMPT]))
	assert row == {
		"docname": "fake-session-doc",
		"run_id": "fake-run",
		"signal": "discarded",
		"reason": "input_changed",
		"attempt": attempt,
		"kind": "fix",
	}


def test_an_answer_from_a_fenced_worker_is_a_discarded_row(worker):
	def cancel():
		with worker.journal.db.transaction():
			worker.journal.mod.cancel("fake-run", docname="fake-session-doc", now=21)

	worker.send_hook = cancel
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	[row] = _signal_rows(worker, "optimus ai refresh discarded")
	assert row["reason"] == "fenced" and row["kind"] == "fix" and row["run_id"] == "fake-run"
	assert worker.journal.run["tokens_reported"] == 7  # the usage is still kept


def test_a_failed_call_is_not_a_discarded_answer(worker):
	def fail():
		raise ai_fix.AiFixError("fake", kind="server")

	worker.send_hook = fail
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert not _signal_rows(worker, "optimus ai refresh discarded")


def test_a_delivery_of_a_slice_already_claimed_is_a_duplicate_blocked_row(worker):
	worker.send_hook = lambda: worker.clock.__setitem__(0, worker.clock[0] + 31)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert not _signal_rows(worker, "optimus ai refresh duplicate blocked")
	worker.jobs.run_ai_refresh_slice("fake-run", 0)  # RQ delivers slice 0 again
	[row] = _signal_rows(worker, "optimus ai refresh duplicate blocked")
	assert row == {
		"docname": "fake-session-doc",
		"run_id": "fake-run",
		"signal": "duplicate_blocked",
		"reason": "already_claimed",
		"slice_no": 0,
	}
	assert len(worker.calls) == 1


def test_a_second_send_of_an_attempted_item_is_a_duplicate_blocked_row(worker, monkeypatch):
	"""The unique attempt key stops a second call for the same item of the same run."""
	worker.count = 1
	worker.jobs.run_ai_refresh_slice("fake-run", 0)  # finding-0 attempted and settled
	assert len(worker.calls) == 1 and worker.journal.run["state"] == "complete"
	run = worker.journal.run
	run.update(state="running", worker_token="worker-x", lease_until=999, active_session="fake-session-doc")
	original = worker.jobs._next_item

	def again(run, memo):
		return original(run, memo) or worker.jobs.PreparedItem(
			"fix", "finding-0", "fake-hash", lambda t: 1 / 0, lambda: True, lambda r: True
		)

	monkeypatch.setattr(worker.jobs, "_next_item", again)
	worker.rows.clear()
	run.update(state="queued", slice_no=1, lease_until=1800, dispatch_pending=1)
	worker.jobs.run_ai_refresh_slice("fake-run", 1)
	[row] = _signal_rows(worker, "optimus ai refresh duplicate blocked")
	assert row["reason"] == "already_attempted" and row["kind"] == "fix" and row["run_id"] == "fake-run"
	assert len(worker.calls) == 1  # no second provider call


def test_a_retried_transaction_leaves_one_signal_row_not_two(worker, monkeypatch):
	"""A try that is rolled back and repeated must not log what its first run recorded."""
	worker.send_hook = lambda: worker.clock.__setitem__(0, worker.clock[0] + 31)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	real = worker.journal.mod.claim
	tries = []

	def claim_twice(*a, signals=None, **kw):
		tries.append(True)
		result = real(*a, signals=signals, **kw)
		if len(tries) == 1:
			raise _Conflict(_DriverError(1213, "fake deadlock"))  # rolled back after it recorded
		return result

	monkeypatch.setattr(worker.journal.mod, "claim", claim_twice)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)  # a replayed delivery of slice 0
	assert len(tries) == 2
	assert len(_signal_rows(worker, "optimus ai refresh duplicate blocked")) == 1


def _fail_answers_after_writing(worker, monkeypatch, failures):
	"""Make the answer's settlement fail (rolled back, after it recorded its signal) ``failures``
	times; the failed-outcome fallback and every other call go through."""
	real = worker.journal.mod.settle_attempt
	seen = []

	def flaky(*a, **kw):
		result = real(*a, **kw)
		if kw.get("outcome") == "succeeded" and len(seen) < failures:
			seen.append(True)
			raise _Conflict(_DriverError(1213, "fake deadlock"))
		return result

	monkeypatch.setattr(worker.journal.mod, "settle_attempt", flaky)
	return seen


def test_a_retried_settlement_leaves_one_discarded_row_not_two(worker, monkeypatch):
	worker.count = 1
	worker.persist_hook = lambda: False
	seen = _fail_answers_after_writing(worker, monkeypatch, 1)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert len(seen) == 1
	assert len(_signal_rows(worker, "optimus ai refresh discarded")) == 1


def test_a_fallback_after_an_uncommitted_discard_logs_no_phantom_row(worker, monkeypatch):
	"""The answer's write recorded a discard but never committed; the fallback records a failed
	outcome. Nothing was discarded, so no row."""
	worker.count = 1
	worker.persist_hook = lambda: False
	seen = _fail_answers_after_writing(worker, monkeypatch, 3)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert len(seen) == 3
	assert not _signal_rows(worker, "optimus ai refresh discarded")


def test_a_retried_begin_leaves_one_duplicate_blocked_row_not_two(worker, monkeypatch):
	worker.count = 1
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	run = worker.journal.run
	original = worker.jobs._next_item

	def again(run, memo):
		return original(run, memo) or worker.jobs.PreparedItem(
			"fix", "finding-0", "fake-hash", lambda t: 1 / 0, lambda: True, lambda r: True
		)

	monkeypatch.setattr(worker.jobs, "_next_item", again)
	real = worker.journal.mod.begin_attempt
	tries = []

	def flaky(*a, **kw):
		result = real(*a, **kw)
		tries.append(True)
		if len(tries) == 1:
			raise _Conflict(_DriverError(1213, "fake deadlock"))
		return result

	monkeypatch.setattr(worker.journal.mod, "begin_attempt", flaky)
	worker.rows.clear()
	run.update(
		state="queued", slice_no=1, lease_until=1800, dispatch_pending=1, active_session="fake-session-doc"
	)
	worker.jobs.run_ai_refresh_slice("fake-run", 1)
	assert len(tries) == 2
	assert len(_signal_rows(worker, "optimus ai refresh duplicate blocked")) == 1


def test_a_count_above_the_int_range_is_logged_before_it_is_clamped(worker):
	worker.count = 1
	worker.tokens = 2**31 + 5
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	[row] = [
		c for t, c in worker.rows if t == "optimus ai refresh item" and "clamped" in c.get("outcome", "")
	]
	assert row["tokens"] == 2**31 + 5 and row["docname"] == "fake-session-doc" and row["run_id"] == "fake-run"
	assert row["item"] == "fix" and row["attempt"]
	assert worker.journal.run["tokens_reported"] == 2**31 + 5  # the run keeps the full count


def test_an_ordinary_count_logs_no_clamp_row(worker):
	worker.count = 1
	worker.tokens = 2**31 - 1
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert not [c for t, c in worker.rows if "clamped" in c.get("outcome", "")]


# --- one row when a call becomes uncertain or a run ends abnormally -------------------------


def _titled(worker, title):
	return [context for t, context in worker.rows if t == title]


def _no_open_transaction_while_logging(worker, monkeypatch):
	"""Each row is written after the transaction that decided it ended."""
	log = ai_fix.log_ai_failure
	open_at = []

	def checked(title, exc=None, **context):
		open_at.append(worker.journal.db.active)
		return log(title, exc, **context)

	monkeypatch.setattr(ai_fix, "log_ai_failure", checked)
	return open_at


def test_a_worker_failure_after_the_send_writes_one_uncertain_row_and_one_ended_row(worker, monkeypatch):
	worker.count = 1
	open_at = _no_open_transaction_while_logging(worker, monkeypatch)

	def outage(*a, **kw):
		raise OSError("fake storage outage")

	monkeypatch.setattr(worker.journal.mod, "settle_attempt", outage)
	with pytest.raises(worker.jobs.RefreshSliceFailed):
		worker.jobs.run_ai_refresh_slice("fake-run", 0)
	[attempt] = worker.journal.db.rows[worker.journal.mod.ATTEMPT].values()
	assert _titled(worker, "optimus ai refresh uncertain") == [
		{
			"docname": "fake-session-doc",
			"run_id": "fake-run",
			"attempt": attempt["name"],
			"reason": "worker_interrupted",
		}
	]
	assert _titled(worker, "optimus ai refresh ended") == [
		{
			"docname": "fake-session-doc",
			"run_id": "fake-run",
			"state": "interrupted",
			"reason": "worker_interrupted",
		}
	]
	assert open_at and not any(open_at)


def test_a_call_the_job_timeout_cut_writes_one_uncertain_row(worker, monkeypatch):
	"""The worker settles the call as uncertain itself before the timeout stops the job; the
	cleanup then finds no open call, so there is one uncertain row, not two."""
	JobTimeoutException = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	worker.count = 1
	open_at = _no_open_transaction_while_logging(worker, monkeypatch)

	def timeout():
		raise JobTimeoutException("fake: the job timeout fired during the provider call")

	worker.send_hook = timeout
	with pytest.raises(JobTimeoutException):
		worker.jobs.run_ai_refresh_slice("fake-run", 0)
	[attempt] = worker.journal.db.rows[worker.journal.mod.ATTEMPT].values()
	assert [(c["attempt"], c["reason"]) for c in _titled(worker, "optimus ai refresh uncertain")] == [
		(attempt["name"], "timeout")
	]
	assert [c["reason"] for c in _titled(worker, "optimus ai refresh ended")] == ["worker_interrupted"]
	assert open_at and not any(open_at)


@pytest.mark.parametrize("reason", ["disabled", "history_limit", "phase2", "permission"])
def test_a_run_that_stops_for_a_reason_an_operator_must_see_writes_one_ended_row(worker, monkeypatch, reason):
	worker.count = 3
	select = worker.jobs._next_item
	open_at = _no_open_transaction_while_logging(worker, monkeypatch)

	def next_item(run, memo):
		if worker.calls:
			raise worker.jobs.StopRefresh(reason)
		return select(run, memo)

	monkeypatch.setattr(worker.jobs, "_next_item", next_item)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert worker.journal.run["end_reason"] == reason
	assert _titled(worker, "optimus ai refresh ended") == [
		{"docname": "fake-session-doc", "run_id": "fake-run", "state": "stopped", "reason": reason}
	]
	assert not _titled(worker, "optimus ai refresh uncertain") and not any(open_at)


def test_a_run_that_stops_without_progress_writes_one_ended_row(worker):
	worker.valid = False
	for slice_no in range(3):
		worker.jobs.run_ai_refresh_slice("fake-run", slice_no)
	assert worker.journal.run["end_reason"] == "no_progress"
	assert _titled(worker, "optimus ai refresh ended") == [
		{"docname": "fake-session-doc", "run_id": "fake-run", "state": "stopped", "reason": "no_progress"}
	]


@pytest.mark.parametrize("reason", ["complete", "deadline", "breaker", "not_ready"])
def test_a_run_that_ends_normally_writes_no_ended_row(worker, monkeypatch, reason):
	worker.count = 1
	if reason == "deadline":
		worker.journal.run["deadline"] = 29
	elif reason == "breaker":

		def fail():
			raise ai_fix.AiFixError("fake provider error", kind="auth")

		worker.send_hook = fail
	elif reason == "not_ready":

		def next_item(run, memo):
			raise worker.jobs.StopRefresh("not_ready")

		monkeypatch.setattr(worker.jobs, "_next_item", next_item)
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert worker.journal.run["end_reason"] == reason
	assert not _titled(worker, "optimus ai refresh ended")
	assert not _titled(worker, "optimus ai refresh uncertain")
