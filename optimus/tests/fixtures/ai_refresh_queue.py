"""Queue loss delays a SQL intent; it cannot erase or execute it inline."""


from copy import deepcopy
from importlib import import_module
from types import SimpleNamespace

import pytest

from optimus import ai_fix
from optimus.tests.fixtures.ai_refresh_journal import journal as _journal_fixture  # noqa: F401


@pytest.fixture(name="queue")
def queue(journal, monkeypatch):
	jobs = import_module("optimus.ai_jobs")
	monkeypatch.setattr("optimus.journal_schema.ready", lambda *a: True)
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
	monkeypatch.setattr(
		ai_fix,
		"log_ai_failure",
		lambda title, exc=None, **kw: logs.append(type(exc).__name__) if exc is not None else None,
	)
	# The selection counts and the Refresh flag have their own tests (test_ai_refresh_contracts).
	monkeypatch.setattr(jobs, "_selection_counts", lambda *a, **kw: (0, 0))
	return SimpleNamespace(jobs=jobs, journal=journal, sent=sent, logs=logs, callbacks=callbacks)


class _QueryTimeoutError(Exception):
	"""``frappe.QueryTimeoutError(e)`` around a lock wait timeout."""


class _QueryDeadlockError(Exception):
	"""``frappe.QueryDeadlockError(e)`` around a deadlock."""


class _DriverError(Exception):
	"""PyMySQL's shape: ``args == (code, message)``."""


def _lock_wait():
	return _QueryTimeoutError(_DriverError(1205, "fake lock wait timeout"))


@pytest.fixture(name="admitting")
def admitting(queue, monkeypatch):
	"""``start_refresh`` with a listening worker, an enabled provider and an allowed user."""
	monkeypatch.setattr(queue.jobs, "_workers_listening", lambda docname=None: True)
	monkeypatch.setattr(queue.jobs, "_check_run", lambda run: None)
	monkeypatch.setattr(ai_fix, "is_available", lambda **kw: True)
	monkeypatch.setattr(queue.jobs.time, "sleep", lambda seconds: None)
	return queue


@pytest.fixture(name="rows")
def rows(queue, monkeypatch):
	"""Every Error Log write as (title, exception, context)."""
	logged = []
	monkeypatch.setattr(
		ai_fix, "log_ai_failure", lambda title, exc=None, **kw: logged.append((title, exc, kw))
	)
	return logged


def _start(queue):
	return queue.jobs.start_refresh(
		docname="fake-session-doc", session_uuid="fake-session", requested_by="fake-owner"
	)


def _crash(queue, lease_until=30):
	"""The fixture run, claimed and mid-call at 10-11, by a worker that died."""
	journal = queue.journal
	with journal.db.transaction():
		journal.mod.claim(
			"fake-run", docname="fake-session-doc", slice_no=0, worker_token="dead", now=10, lease_seconds=20
		)
		attempt = journal.mod.begin_attempt(
			"fake-run",
			docname="fake-session-doc",
			worker_token="dead",
			kind="fix",
			target="fake-finding",
			input_hash="fake",
			now=11,
		)
	journal.run["lease_until"] = lease_until
	return attempt


def _three_runs(queue):
	"""poison and victim: expired leases; lost: a committed intent that never reached RQ."""
	journal = queue.journal
	journal.db.rows[journal.mod.RUN].clear()
	for name, session, values in (
		("poison", "sess-p", {"state": "running", "worker_token": "w0", "lease_until": 10}),
		("victim", "sess-v", {"state": "running", "worker_token": "w1", "lease_until": 20}),
		("lost", "sess-q", {"state": "queued", "lease_until": 1800, "dispatch_pending": 1}),
	):
		journal.db.rows[journal.mod.RUN][name] = {
			**journal.run,
			"name": name,
			"run_id": name,
			"session_name": session,
			"active_session": session,
			"dispatched_at_epoch": 0,
			**values,
		}
		journal.db.rows["Optimus Session"][session] = {
			"name": session,
			"session_uuid": "fake-" + session,
			"status": "Ready",
			"ai_tokens_spent": 0,
			"ai_refresh_count": 0,
		}
	return journal.db.rows[journal.mod.RUN]


def _cancel(queue, docname="fake-session-doc", run_id="fake-run"):
	return queue.jobs.cancel_refresh(run_id, docname=docname, requested_by="fake-owner")


_SHOW = (("SHOW lock_timeout", ()), True)  # the first statement: it takes no snapshot either


_SET = (("SET LOCAL lock_timeout = %s", ("30s",)), False)


_HTTP_BOUND = [_SHOW, _SET]


def _statements(queue):
	"""The statements sent with ``frappe.db.sql``, each with whether it opened its transaction."""
	return [call[1:] for call in queue.journal.db.calls if call[0] == "SQL"]


def _bound_then_lock(queue):
	"""True when the last statement of every lock bound is followed at once by the session
	row's lock."""
	calls = queue.journal.db.calls
	return all(
		calls[i + 1] == ("Optimus Session", "fake-session-doc", True)
		for i, call in enumerate(calls)
		if call[0] == "SQL" and calls[i + 1][0] != "SQL"
	)


def _pending(queue, dispatched_at):
	queue.journal.run.update(state="queued", dispatch_pending=1, dispatched_at_epoch=dispatched_at)


@pytest.fixture(name="ended_rows")
def ended_rows(queue, monkeypatch):
	"""Every Error Log write as (title, context), with whether a journal transaction was open."""
	logged = []

	def log(title, exc=None, **context):
		logged.append((title, context, queue.journal.db.active))

	monkeypatch.setattr(ai_fix, "log_ai_failure", log)
	return logged


def _titled(logged, title):
	return [context for t, context, *_ in logged if t == title]
