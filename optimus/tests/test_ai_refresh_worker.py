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
	monkeypatch.setattr(jobs, "_dispatch_pending", lambda run_id, docname: queued.append(run_id))
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
		# A failure after the request was sent, as the call boundary reports it: usage
		# unknown (an error raised before any call is a known zero, C3).
		raise ai_fix.AiFixError("fake provider error", kind=kind, usage_complete=False)

	worker.send_hook = fail
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert len(worker.calls) == expected and not worker.written
	assert worker.journal.run["end_reason"] == "breaker"
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
	[(title, exc, context)] = [row for row in rows if row[0] == "optimus AI refresh worker"]
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
	[(title, exc, context)] = [row for row in rows if row[0] == "optimus AI refresh worker"]
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
			return ai_fix.suggest_fix(
				{"finding_type": "N+1 Query"},
				timeout=timeout,
				session_uuid="fake-session",
				docname="fake-session-doc",
			)

		return worker.jobs.PreparedItem(
			item.kind, item.target, item.input_hash, send, item.valid, item.persist
		)

	monkeypatch.setattr(worker.jobs, "_next_item", next_item)
	worker.count = 2
	worker.jobs.run_ai_refresh_slice("fake-run", 0)
	assert not sent and worker.journal.run["attempted"] == 2
	assert worker.journal.run["usage_incomplete"] == 0
	assert all(a["usage_complete"] == 1 for a in worker.journal.db.rows[worker.journal.mod.ATTEMPT].values())
