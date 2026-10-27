"""Exercise worker delivery against the transactional journal, not a mock ledger."""


from importlib import import_module
from types import SimpleNamespace

import pytest

from optimus import ai_fix
from optimus.tests.fixtures.ai_refresh_journal import journal as _journal_fixture  # noqa: F401


@pytest.fixture(name="worker")
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
	render_pending = jobs._render_pending
	monkeypatch.setattr(jobs, "_render_pending", lambda *a: None)
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
		render_pending=render_pending,
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


@pytest.fixture(name="rows")
def rows(worker, monkeypatch):
	"""Every Error Log write as (title, exception, context)."""
	logged = []
	monkeypatch.setattr(
		ai_fix, "log_ai_failure", lambda title, exc=None, **kw: logged.append((title, exc, kw))
	)
	return logged


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


def _signal_rows(worker, title):
	return [context for t, context in worker.rows if t == title]


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
