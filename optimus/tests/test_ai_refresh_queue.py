"""Queue loss delays a SQL intent; it cannot erase or execute it inline."""

from copy import deepcopy
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
	monkeypatch.setattr(
		ai_fix,
		"log_ai_failure",
		lambda title, exc=None, **kw: logs.append(type(exc).__name__) if exc is not None else None,
	)
	# The selection counts and the Refresh flag have their own tests (test_ai_refresh_contracts).
	monkeypatch.setattr(jobs, "_selection_counts", lambda *a, **kw: (0, 0))
	monkeypatch.setattr(jobs, "_refresh_flag_held", lambda session_uuid: False)
	return SimpleNamespace(jobs=jobs, journal=journal, sent=sent, logs=logs, callbacks=callbacks)


def test_dispatch_runs_only_after_commit_and_bypasses_capture(queue):
	assert queue.jobs._dispatch_pending("fake-run", "fake-session-doc") is True
	method, kw = queue.sent[0]
	assert method == "optimus.ai_jobs.run_ai_refresh_slice"
	assert kw["run_id"] == "fake-run" and kw["slice_no"] == 0
	assert kw["deduplicate"] is True and kw["timeout"] == 480
	assert queue.journal.run["dispatch_pending"] == 1
	assert queue.jobs._dispatch_pending("fake-run", "fake-session-doc") is False
	assert len(queue.sent) == 1


def test_redis_error_keeps_intent_and_reports_failure(queue, monkeypatch):
	def fail(*a, **kw):
		raise ConnectionError("fake Redis down")

	monkeypatch.setattr(queue.jobs.frappe.enqueue, "__wrapped__", fail)
	assert queue.jobs._dispatch_pending("fake-run", "fake-session-doc") is False
	assert queue.journal.run["state"] == "queued" and queue.journal.run["dispatch_pending"] == 1
	assert queue.logs == ["ConnectionError"] and not queue.sent


def test_worker_claim_is_the_only_delivery_acknowledgement(queue):
	queue.jobs._dispatch_pending("fake-run", "fake-session-doc")
	with queue.journal.db.transaction():
		queue.journal.mod.claim(
			"fake-run",
			docname="fake-session-doc",
			slice_no=0,
			worker_token="worker",
			now=51,
			lease_seconds=300,
		)
	assert queue.journal.run["dispatch_pending"] == 0
	assert queue.jobs._dispatch_pending("fake-run", "fake-session-doc") is False


def test_after_commit_enqueue_failure_also_preserves_intent(queue, monkeypatch):
	def enqueue(*a, **kw):
		def fail():
			raise ConnectionError("fake after-commit queue failure")

		queue.callbacks.append(fail)

	monkeypatch.setattr(queue.jobs.frappe.enqueue, "__wrapped__", enqueue)
	assert queue.jobs._dispatch_pending("fake-run", "fake-session-doc") is False
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
		queue.journal.mod.claim(
			"fake-run",
			docname="fake-session-doc",
			slice_no=0,
			worker_token="worker",
			now=10,
			lease_seconds=20,
		)
		attempt = queue.journal.mod.begin_attempt(
			"fake-run",
			docname="fake-session-doc",
			worker_token="worker",
			kind="fix",
			target="fake-finding",
			input_hash="fake",
			now=11,
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
	monkeypatch.setattr(queue.jobs, "_workers_listening", lambda docname=None: False)
	monkeypatch.setattr(queue.jobs, "_check_run", lambda run: None)
	monkeypatch.setattr(ai_fix, "is_available", lambda: True)
	out = queue.jobs.start_refresh(
		docname="fake-session-doc", session_uuid="fake-session", requested_by="fake-owner"
	)
	assert out == {"status": "refused", "reason": "no_worker"}
	assert not queue.journal.db.rows[queue.journal.mod.RUN] and not queue.sent


@pytest.mark.parametrize("redis", ["up", "down"])
def test_existing_refresh_is_visible_even_when_provider_or_queue_is_unavailable(queue, monkeypatch, redis):
	"""Attaching never needs a worker or Redis, and never changes its answer. The fixture run
	is a committed intent that never reached RQ: the click re-sends it, best effort (F1)."""
	monkeypatch.setattr(queue.jobs, "_check_run", lambda run: None)
	monkeypatch.setattr(ai_fix, "is_available", lambda: False)

	def unavailable():
		raise AssertionError("existing progress must not require Redis")

	monkeypatch.setattr(queue.jobs, "_workers_listening", unavailable)
	if redis == "down":

		def fail(*a, **kw):
			raise ConnectionError("fake Redis down")

		monkeypatch.setattr(queue.jobs.frappe.enqueue, "__wrapped__", fail)
	out = queue.jobs.start_refresh(
		docname="fake-session-doc", session_uuid="fake-session", requested_by="fake-owner"
	)
	assert out["status"] == "already_running" and out["refresh"]["run_id"] == "fake-run"
	assert len(queue.journal.db.rows[queue.journal.mod.RUN]) == 1
	assert [kw["slice_no"] for _, kw in queue.sent] == ([0] if redis == "up" else [])
	assert queue.logs == ([] if redis == "up" else ["ConnectionError"])
	assert queue.journal.run["dispatch_pending"] == 1


def test_start_commits_reservation_before_enqueuing(queue, monkeypatch):
	queue.journal.db.rows[queue.journal.mod.RUN] = {}
	monkeypatch.setattr(queue.jobs, "_workers_listening", lambda docname=None: True)
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
	monkeypatch.setattr(queue.jobs, "_workers_listening", lambda docname=None: True)
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


@pytest.mark.parametrize("still_active", [True, False])
def test_a_reservation_the_admission_could_not_see_is_read_again(queue, monkeypatch, still_active):
	"""The unique active_session key refused the insert but this transaction's snapshot could
	not see that run (Postgres): it is read again, fresh, or reported busy once it ended."""
	monkeypatch.setattr(queue.jobs, "_workers_listening", lambda docname=None: True)
	monkeypatch.setattr(queue.jobs, "_check_run", lambda run: None)
	monkeypatch.setattr(ai_fix, "is_available", lambda: True)
	monkeypatch.setattr(queue.journal.mod, "admit", lambda **kw: {"status": "already_running", "run": None})
	reads = []
	current = queue.journal.db.get_value

	def get_value(table, name, *a, **kw):
		reads.append(table)
		if len(reads) == 1 or not still_active:
			return None
		return current(table, name, *a, **kw)

	monkeypatch.setattr(queue.journal.db, "get_value", get_value)
	out = queue.jobs.start_refresh(
		docname="fake-session-doc", session_uuid="fake-session", requested_by="fake-owner"
	)
	if still_active:
		assert out["status"] == "already_running" and out["refresh"]["run_id"] == "fake-run"
	else:
		assert out == {"status": "refused", "reason": "busy"}
	assert not queue.sent


def test_an_http_admission_waits_for_its_session_row_once_then_answers_busy(queue, monkeypatch):
	"""F-5: a held session row costs the HTTP caller one lock wait timeout, then busy; no
	second attempt waits again (a worker transition still retries lock waits)."""

	class QueryTimeoutError(Exception):
		pass

	class DriverError(Exception):
		pass

	queue.journal.db.rows[queue.journal.mod.RUN] = {}
	monkeypatch.setattr(queue.jobs, "_workers_listening", lambda docname=None: True)
	monkeypatch.setattr(queue.jobs, "_check_run", lambda run: None)
	monkeypatch.setattr(ai_fix, "is_available", lambda: True)
	monkeypatch.setattr(queue.jobs.time, "sleep", lambda seconds: None)
	current = queue.journal.db.get_value
	waits = []

	def held(table, name, *a, for_update=False, **kw):
		if table == "Optimus Session" and for_update:
			waits.append(name)
			raise QueryTimeoutError(DriverError(1205, "fake lock wait timeout"))
		return current(table, name, *a, for_update=for_update, **kw)

	monkeypatch.setattr(queue.journal.db, "get_value", held)
	out = queue.jobs.start_refresh(
		docname="fake-session-doc", session_uuid="fake-session", requested_by="fake-owner"
	)
	assert out == {"status": "refused", "reason": "busy"}
	assert waits == ["fake-session-doc"]
	assert not queue.journal.db.rows[queue.journal.mod.RUN] and not queue.sent


def test_an_http_admission_never_retries_a_lock_wait_timeout_anywhere(queue, monkeypatch):
	"""F-5: a lock wait timeout outside the session and mutex locks (here the reservation
	INSERT) is raised after one attempt instead of waiting two more timeouts."""

	class QueryTimeoutError(Exception):
		pass

	class DriverError(Exception):
		pass

	queue.journal.db.rows[queue.journal.mod.RUN] = {}
	monkeypatch.setattr(queue.jobs, "_workers_listening", lambda docname=None: True)
	monkeypatch.setattr(queue.jobs, "_check_run", lambda run: None)
	monkeypatch.setattr(ai_fix, "is_available", lambda: True)
	monkeypatch.setattr(queue.jobs.time, "sleep", lambda seconds: None)
	inserts = []

	def waited(values):
		inserts.append(values["name"])
		raise QueryTimeoutError(DriverError(1205, "fake lock wait timeout"))

	monkeypatch.setattr(queue.journal.db, "insert", waited)
	with pytest.raises(QueryTimeoutError):
		queue.jobs.start_refresh(
			docname="fake-session-doc", session_uuid="fake-session", requested_by="fake-owner"
		)
	assert len(inserts) == 1 and not queue.sent


# F2: recovery without the scheduler, the sweep, cancellation and settings.


class _QueryTimeoutError(Exception):
	"""``frappe.QueryTimeoutError(e)`` around a lock wait timeout."""


class _QueryDeadlockError(Exception):
	"""``frappe.QueryDeadlockError(e)`` around a deadlock."""


class _DriverError(Exception):
	"""PyMySQL's shape: ``args == (code, message)``."""


def _lock_wait():
	return _QueryTimeoutError(_DriverError(1205, "fake lock wait timeout"))


@pytest.fixture
def admitting(queue, monkeypatch):
	"""``start_refresh`` with a listening worker, an enabled provider and an allowed user."""
	monkeypatch.setattr(queue.jobs, "_workers_listening", lambda docname=None: True)
	monkeypatch.setattr(queue.jobs, "_check_run", lambda run: None)
	monkeypatch.setattr(ai_fix, "is_available", lambda **kw: True)
	monkeypatch.setattr(queue.jobs.time, "sleep", lambda seconds: None)
	return queue


@pytest.fixture
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


def test_a_refresh_click_recovers_a_crashed_run_without_the_scheduler(admitting, monkeypatch, rows):
	"""A2: optimus.local runs with pause_scheduler; a worker killed mid-call left its run
	running and the session reserved, so every later Refresh answered already_running. The
	click itself now takes the expired run over and starts the new one."""
	queue = admitting

	def never():
		raise AssertionError("recovery must not wait for the scheduler")

	monkeypatch.setattr(queue.jobs, "recover_pending", never)
	attempt = _crash(queue)
	out = _start(queue)
	assert out["status"] == "queued" and out["refresh"]["run_id"] != "fake-run"
	assert len(queue.sent) == 1 and queue.sent[0][1]["run_id"] == out["refresh"]["run_id"]
	old = queue.journal.db.rows[queue.journal.mod.RUN]["fake-run"]
	assert old["state"] == "interrupted" and old["end_reason"] == "lease_expired"
	assert queue.journal.db.rows[queue.journal.mod.ATTEMPT][attempt["name"]]["state"] == "uncertain"
	assert [(title, exc, context.get("attempt")) for title, exc, context in rows] == [
		("optimus ai refresh uncertain", None, attempt["name"]),
		("optimus ai refresh ended", None, None),
	]


def test_a_click_with_ai_switched_off_is_refused_and_starts_nothing(admitting, monkeypatch, rows):
	"""With no live run to attach to, a provider that is off refuses the click before any
	setting, queue or journal is touched: nothing is reserved, counted or sent."""
	queue = admitting
	queue.journal.db.rows[queue.journal.mod.RUN] = {}
	monkeypatch.setattr(ai_fix, "is_available", lambda **kw: False)
	assert _start(queue) == {"status": "refused", "reason": "disabled"}
	assert queue.journal.db.rows[queue.journal.mod.RUN] == {} and not queue.sent and not rows
	assert queue.journal.db.rows["Optimus Session"]["fake-session-doc"]["ai_refresh_count"] == 0


def test_refresh_state_shows_the_sessions_run_and_nothing_for_another_session(queue):
	"""``refresh_state`` is read only: the progress of the session's run, counts only, or None
	when the session has none."""
	out = queue.jobs.refresh_state("fake-session-doc")
	assert out == queue.jobs.public_state(queue.journal.run)
	assert out["run_id"] == "fake-run" and out["state"] == "queued"
	assert queue.jobs.refresh_state("other-session-doc") is None
	assert not queue.sent and not queue.journal.db.locks


def test_a_click_attaches_to_a_live_run(admitting):
	queue = admitting
	_crash(queue, lease_until=500)
	out = _start(queue)
	assert out["status"] == "already_running" and out["refresh"]["run_id"] == "fake-run"
	assert out["refresh"]["state"] == "running" and out["refresh"]["stalled"] is False
	assert not queue.sent


def test_a_takeover_of_an_inconsistent_run_writes_one_row_naming_it(admitting, rows):
	queue = admitting
	attempt = _crash(queue)
	del queue.journal.db.rows[queue.journal.mod.ATTEMPT][attempt["name"]]
	assert _start(queue)["status"] == "queued"
	[(title, exc, context)] = [row for row in rows if row[0] == "optimus ai refresh release"]
	assert title == "optimus ai refresh release" and context["run_id"] == "fake-run"
	assert context["docname"] == "fake-session-doc" and "attempt" in str(exc)
	# no attempt row was there to make uncertain: the release row says the call is
	assert [row[0] for row in rows] == ["optimus ai refresh release", "optimus ai refresh ended"]


@pytest.mark.parametrize(
	"values,reason",
	[({"lease_until": 40}, "lease_expired"), ({"lease_until": 900, "deadline": 45}, "deadline")],
)
def test_progress_shows_a_run_that_can_no_longer_progress_as_interrupted(queue, values, reason):
	"""A2: derived from the lease and the deadline, before any recovery has written it."""
	run = {**queue.journal.run, "state": "running", **values}
	out = queue.jobs.public_state(run)
	assert out["state"] == "interrupted" and out["stalled"] is True and out["end_reason"] == reason
	live = queue.jobs.public_state({**queue.journal.run, "state": "running", "lease_until": 900})
	assert live["state"] == "running" and live["stalled"] is False and live["end_reason"] == ""
	ended = queue.jobs.public_state({**queue.journal.run, "state": "complete", "lease_until": 0})
	assert ended["state"] == "complete" and ended["stalled"] is False


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


@pytest.mark.parametrize("variant", ["lock_wait", "inconsistent", "unexpected"])
def test_one_bad_row_never_stops_the_recovery_sweep(queue, monkeypatch, rows, variant):
	"""O1 / E1 / R1: the sweep raised at its first bad row (a corrupt run, or a session row a
	dead transaction held past a lock wait timeout) on every tick, so no lease, deadline or
	lost dispatch was recovered anywhere on the site. Each row is now on its own."""
	monkeypatch.setattr(queue.jobs.frappe, "get_all", queue.journal.db.get_all, raising=False)
	monkeypatch.setattr(queue.jobs.time, "sleep", lambda seconds: None)
	runs = _three_runs(queue)
	events = []
	real = queue.journal.mod.interrupt

	def interrupt(run_id, **kw):
		events.append(run_id)
		if run_id == "poison" and variant == "lock_wait":
			raise _lock_wait()
		if run_id == "poison" and variant == "unexpected":
			raise RuntimeError("fake storage failure")
		return real(run_id, **kw)

	monkeypatch.setattr(queue.journal.mod, "interrupt", interrupt)
	dispatch = queue.journal.mod.prepare_dispatch
	monkeypatch.setattr(
		queue.journal.mod,
		"prepare_dispatch",
		lambda run_id, **kw: (events.append(run_id), dispatch(run_id, **kw))[1],
	)
	logged = ai_fix.log_ai_failure
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **kw: (events.append("log"), logged(*a, **kw)))
	if variant == "inconsistent":
		runs["poison"]["active_attempt"] = "attempt-row-missing"
	counts = queue.jobs.recover_pending()
	runs = queue.journal.db.rows[queue.journal.mod.RUN]  # a rolled-back row restores the rows
	assert runs["victim"]["state"] == "interrupted" and runs["victim"]["active_session"] is None
	assert [kw["run_id"] for _, kw in queue.sent] == ["lost"]
	ended = sorted(context["run_id"] for title, _, context in rows if title == "optimus ai refresh ended")
	others = [row for row in rows if row[0] != "optimus ai refresh ended"]
	if variant == "lock_wait":
		assert runs["poison"]["state"] == "running" and not others and ended == ["victim"]
		assert counts == {"interrupted": 1, "dispatched": 1, "busy": 1, "failed": 0}
		assert events.count("poison") == 1  # NOWAIT, never retried
	elif variant == "inconsistent":
		assert runs["poison"]["state"] == "interrupted" and runs["poison"]["active_session"] is None
		assert counts == {"interrupted": 2, "dispatched": 1, "busy": 0, "failed": 0}
		[(title, exc, context)] = others
		assert context["run_id"] == "poison" and "attempt" in str(exc) and ended == ["poison", "victim"]
	else:
		assert runs["poison"]["state"] == "running"
		assert counts == {"interrupted": 1, "dispatched": 1, "busy": 0, "failed": 1}
		[(title, exc, context)] = others
		assert title == "optimus ai refresh recovery" and type(exc) is RuntimeError and ended == ["victim"]
		assert context["run_id"] == "poison" and context["docname"] == "sess-p"
		assert events.index("log") > max(i for i, e in enumerate(events) if e != "log")


def test_the_sweep_takes_every_session_row_with_nowait(queue, monkeypatch):
	monkeypatch.setattr(queue.jobs.frappe, "get_all", queue.journal.db.get_all, raising=False)
	_three_runs(queue)
	current = queue.journal.db.get_value
	waits = set()

	def spy(table, name, *a, for_update=False, wait=True, **kw):
		if table == "Optimus Session" and for_update:
			waits.add(wait)
		return current(table, name, *a, for_update=for_update, **kw)

	monkeypatch.setattr(queue.journal.db, "get_value", spy)
	assert queue.jobs.recover_pending()["interrupted"] == 2
	assert waits == {False}


def test_a_failed_candidate_read_never_stops_the_other_passes(queue, monkeypatch, rows):
	runs = _three_runs(queue)

	def get_all(table, *, filters, **kw):
		if "lease_until" in filters:
			raise RuntimeError("fake read failure")
		return queue.journal.db.get_all(table, filters=filters, **kw)

	monkeypatch.setattr(queue.jobs.frappe, "get_all", get_all, raising=False)
	runs["victim"]["deadline"] = 30
	counts = queue.jobs.recover_pending()
	runs = queue.journal.db.rows[queue.journal.mod.RUN]
	assert runs["victim"]["state"] == "interrupted" and len(queue.sent) == 1
	assert counts["failed"] == 1 and [(title, type(exc)) for title, exc, _ in rows] == [
		("optimus ai refresh ended", type(None)),
		("optimus ai refresh recovery", RuntimeError),
	]


def test_an_http_admission_leaves_a_busy_dispatch_pending_without_waiting(admitting, monkeypatch, rows):
	"""C1 / F4: the admission already waited for the session row; its dispatch takes the row
	with NOWAIT, so one click waits one lock timeout at most. A busy row leaves the committed
	intent pending for the session's next click or a takeover, without failing the response."""
	queue = admitting
	queue.journal.db.rows[queue.journal.mod.RUN] = {}
	current = queue.journal.db.get_value
	locks = []

	def held(table, name, *a, for_update=False, wait=True, **kw):
		if table == "Optimus Session" and for_update:
			locks.append(wait)
			if len(locks) > 1:  # the admission got it; the dispatch does not
				raise _lock_wait()
		return current(table, name, *a, for_update=for_update, **kw)

	monkeypatch.setattr(queue.journal.db, "get_value", held)
	out = _start(queue)
	assert out["status"] == "queued" and out["refresh"]["state"] == "queued"
	assert locks == [True, False] and not queue.sent and not rows
	run = next(iter(queue.journal.db.rows[queue.journal.mod.RUN].values()))
	assert run["dispatch_pending"] == 1


def _cancel(queue, docname="fake-session-doc", run_id="fake-run"):
	return queue.jobs.cancel_refresh(run_id, docname=docname, requested_by="fake-owner")


def test_cancel_answers_with_the_cancelled_progress(queue, rows):
	out = _cancel(queue)
	assert out["status"] == "cancelled" and out["refresh"]["state"] == "cancelled"
	assert queue.journal.run["cancelled_by"] == "fake-owner" and not rows


def test_cancel_of_another_sessions_run_changes_nothing_and_raises_nothing(queue, rows):
	"""S1 / C2: the caller authorized one session; a run id of another session is refused
	under the session lock, exactly like a run that does not exist."""
	queue.journal.db.rows["Optimus Session"]["other-session-doc"] = {
		**queue.journal.db.rows["Optimus Session"]["fake-session-doc"],
		"name": "other-session-doc",
	}
	before = deepcopy(queue.journal.db.rows)
	assert _cancel(queue, docname="other-session-doc") == {"status": "refused", "reason": "not_running"}
	assert _cancel(queue, run_id="missing-run") == {"status": "refused", "reason": "not_running"}
	assert queue.journal.db.rows == before and not rows


@pytest.mark.parametrize(
	"run_id",
	[{"state": "queued"}, {"active_session": "fake-session-doc"}, None, "", "x" * 141],
	ids=["filter", "reservation-filter", "none", "empty", "141-characters"],
)
def test_cancel_with_a_run_id_that_is_not_a_short_string_is_not_running(queue, rows, run_id):
	"""A filter in place of the run id cancelled the session's own run without its id, through
	a locking read by filter. It names no run, like a run that does not exist."""
	before = deepcopy(queue.journal.db.rows)
	assert _cancel(queue, run_id=run_id) == {"status": "refused", "reason": "not_running"}
	assert queue.journal.db.rows == before and not rows


def test_cancel_waits_for_a_held_session_row_once_then_answers_busy(queue, monkeypatch, rows):
	"""C1: an HTTP cancel waited up to three lock timeouts and then raised."""
	monkeypatch.setattr(queue.jobs.time, "sleep", lambda seconds: None)
	current = queue.journal.db.get_value
	waits = []

	def held(table, name, *a, for_update=False, **kw):
		if table == "Optimus Session" and for_update:
			waits.append(name)
			raise _lock_wait()
		return current(table, name, *a, for_update=for_update, **kw)

	monkeypatch.setattr(queue.journal.db, "get_value", held)
	assert _cancel(queue) == {"status": "refused", "reason": "busy"}
	assert waits == ["fake-session-doc"] and queue.journal.run["state"] == "queued" and not rows


def test_a_cancel_that_fails_for_another_reason_raises_instead_of_answering_busy(queue, monkeypatch, rows):
	"""Only a lock that was not granted is ``busy``: any other failure of the cancellation is
	raised to the caller, and the run is left as it was."""
	monkeypatch.setattr(queue.jobs.time, "sleep", lambda seconds: None)

	def fail(*a, **kw):
		raise RuntimeError("fake storage failure")

	monkeypatch.setattr(queue.journal.mod, "cancel", fail)
	with pytest.raises(RuntimeError, match="fake storage failure"):
		_cancel(queue)
	assert queue.journal.run["state"] == "queued" and not rows


def test_cancel_still_retries_a_deadlock(queue, monkeypatch):
	monkeypatch.setattr(queue.jobs.time, "sleep", lambda seconds: None)
	real = queue.journal.mod.cancel
	calls = []

	def deadlocked_once(*a, **kw):
		calls.append(True)
		if len(calls) == 1:
			raise _QueryDeadlockError(_DriverError(1213, "fake deadlock"))
		return real(*a, **kw)

	monkeypatch.setattr(queue.journal.mod, "cancel", deadlocked_once)
	assert _cancel(queue)["status"] == "cancelled" and len(calls) == 2


def test_cancel_force_releases_a_corrupt_run_with_one_row(queue, rows):
	"""E2: a corrupt run could not be cancelled, so it held its session and a site-cap slot
	until someone repaired the journal by hand."""
	queue.journal.run.update(
		state="running", worker_token="x", lease_until=999, active_session="SOMETHING-ELSE"
	)
	out = _cancel(queue)
	assert out["status"] == "cancelled" and queue.journal.run["active_session"] is None
	[(title, exc, context)] = rows
	assert title == "optimus ai refresh release" and context["run_id"] == "fake-run"
	assert "reservation" in str(exc) and context["docname"] == "fake-session-doc"


# --- the lock wait of an HTTP request on Postgres ------------------------------------------------

_HTTP_BOUND = (("SET LOCAL lock_timeout = %s", ("30s",)), True)


def _statements(queue):
	"""The statements sent with ``frappe.db.sql``, each with whether it opened its transaction."""
	return [call[1:] for call in queue.journal.db.calls if call[0] == "SQL"]


def _bound_then_lock(queue):
	"""True when every lock bound is followed at once by the session row's lock."""
	calls = queue.journal.db.calls
	return all(
		calls[i + 1] == ("Optimus Session", "fake-session-doc", True)
		for i, call in enumerate(calls)
		if call[0] == "SQL"
	)


@pytest.mark.parametrize("path", ["click", "cancel"])
def test_on_postgres_an_http_transaction_bounds_its_lock_waits_first(admitting, path):
	"""Postgres waits for a row lock for as long as it takes unless ``lock_timeout`` is set, and
	Frappe sets none: a click or a cancel on a session row a dead transaction holds hung until the
	web server killed it, and its backend went on waiting. The admission and the cancellation set
	the bound for their own transaction, as its first statement, before the session lock."""
	queue = admitting
	queue.journal.db.db_type = "postgres"
	if path == "click":
		queue.journal.db.rows[queue.journal.mod.RUN] = {}
		assert _start(queue)["status"] == "queued"
	else:
		assert _cancel(queue)["status"] == "cancelled"
	assert _statements(queue) == [_HTTP_BOUND]
	assert _bound_then_lock(queue)


def test_each_try_of_an_http_admission_sets_its_bound_again(admitting, monkeypatch):
	"""``SET LOCAL`` ends with its transaction: a try that is rolled back and repeated sets it
	again, first."""
	queue = admitting
	queue.journal.db.rows[queue.journal.mod.RUN] = {}
	queue.journal.db.db_type = "postgres"
	admit = queue.journal.mod.admit
	tries = []

	def deadlocked_once(**kw):
		tries.append(True)
		if len(tries) == 1:
			raise _QueryDeadlockError(_DriverError(1213, "fake deadlock"))
		return admit(**kw)

	monkeypatch.setattr(queue.journal.mod, "admit", deadlocked_once)
	assert _start(queue)["status"] == "queued"
	assert len(tries) == 2 and _statements(queue) == [_HTTP_BOUND, _HTTP_BOUND]


def test_on_mariadb_an_http_transaction_keeps_the_servers_lock_wait(admitting):
	"""MariaDB's wait is the server's ``innodb_lock_wait_timeout``: a session-level SET would
	outlive the transaction on the pooled connection, so the engine sends none."""
	queue = admitting
	queue.journal.db.rows[queue.journal.mod.RUN] = {}
	out = _start(queue)
	assert out["status"] == "queued"
	assert _cancel(queue, run_id=out["refresh"]["run_id"])["status"] == "cancelled"
	assert _statements(queue) == []


def test_no_worker_or_sweep_transaction_on_postgres_sets_a_lock_bound(queue, monkeypatch):
	"""Only the two HTTP transactions are bounded: the sweep, a dispatch and every worker
	transition keep the server's setting and retry a lock wait."""
	queue.journal.db.db_type = "postgres"
	monkeypatch.setattr(queue.jobs.frappe, "get_all", queue.journal.db.get_all, raising=False)
	assert queue.jobs._dispatch_pending("fake-run", "fake-session-doc") is True
	_crash(queue)
	assert queue.jobs.recover_pending()["interrupted"] == 1
	assert _statements(queue) == []


@pytest.mark.parametrize(
	"name,value",
	[
		("optimus_ai_slice_seconds", "120"),
		("optimus_ai_refresh_max_seconds", "900"),
		("optimus_ai_max_active_refreshes", " 3 "),
	],
)
def test_settings_written_by_set_config_without_p_are_read_as_numbers(admitting, name, value):
	"""D5: ``bench set-config`` without ``-p`` stores the string "120"; every admission then
	raised (or, for the slice length, every dispatch failed)."""
	queue = admitting
	queue.journal.db.rows[queue.journal.mod.RUN] = {}
	queue.jobs.frappe.conf[name] = value
	out = _start(queue)
	assert out["status"] == "queued" and len(queue.sent) == 1 and not queue.logs
	if name == "optimus_ai_refresh_max_seconds":
		assert next(iter(queue.journal.db.rows[queue.journal.mod.RUN].values()))["deadline"] == 950


@pytest.mark.parametrize(
	"name,value",
	[
		("optimus_ai_slice_seconds", "2 minutes"),
		("optimus_ai_slice_seconds", 5),
		("optimus_ai_slice_seconds", True),
		("optimus_ai_refresh_max_seconds", 1.5),
		("optimus_ai_max_active_refreshes", "0"),
	],
)
def test_an_invalid_setting_refuses_before_admission_and_names_the_fix(admitting, name, value):
	"""R5: an invalid slice length was admitted, reserved the session and then failed every
	dispatch until its queued lease ran out."""
	queue = admitting
	queue.journal.db.rows[queue.journal.mod.RUN] = {}
	queue.jobs.frappe.conf[name] = value
	with pytest.raises(ValueError) as caught:
		_start(queue)
	assert name in str(caught.value) and "set-config -p" in str(caught.value)
	assert not queue.journal.db.rows[queue.journal.mod.RUN] and not queue.sent


@pytest.mark.parametrize("reason", ["not_ready", "permission", "phase2"])
@pytest.mark.parametrize("existing", [False, True], ids=["no-run", "live-run"])
def test_a_session_or_requester_that_cannot_refresh_is_a_refusal(
	admitting, monkeypatch, rows, reason, existing
):
	"""A session that is not Ready, a requester without access or a Phase 2 run in progress is
	answered like every other refusal, not raised: nothing is admitted, counted, sent or
	logged, and a live run's progress is not shown to a requester who may not see it."""
	queue = admitting
	if not existing:
		queue.journal.db.rows[queue.journal.mod.RUN] = {}
	before = deepcopy(queue.journal.db.rows)

	def stop(run, **kw):
		raise queue.jobs.StopRefresh(reason)

	monkeypatch.setattr(queue.jobs, "_check_run", stop)
	assert _start(queue) == {"status": "refused", "reason": reason}
	assert queue.journal.db.rows == before and not queue.sent and not rows


def test_an_invalid_queue_setting_is_rejected_by_name_before_admission(admitting, monkeypatch):
	"""F5 (probe U1): an invalid queue name was a queue_unavailable refusal whose log row did
	not name the setting; it is rejected like the other settings."""
	queue = admitting
	queue.journal.db.rows[queue.journal.mod.RUN] = {}
	queue.jobs.frappe.conf["optimus_ai_queue"] = "Bad Queue!"
	with pytest.raises(ValueError) as caught:
		_start(queue)
	assert "optimus_ai_queue" in str(caught.value) and "set-config" in str(caught.value)
	assert not queue.journal.db.rows[queue.journal.mod.RUN] and not queue.sent


def _pending(queue, dispatched_at):
	queue.journal.run.update(state="queued", dispatch_pending=1, dispatched_at_epoch=dispatched_at)


def test_a_click_resends_a_committed_intent_that_never_reached_a_worker(admitting, monkeypatch, rows):
	"""F1: a lost dispatch (a busy session row, a Redis blip) left the run queued with no job
	for the whole 1800 s queued lease, since the sweep is not scheduled. The next click
	re-sends it: NOWAIT, best effort; the claim stays the fence and the job id deduplicates."""
	queue = admitting
	_pending(queue, dispatched_at=10)  # 40 s ago: past the 30 s throttle
	current = queue.journal.db.get_value
	waits = []

	def spy(table, name, *a, for_update=False, wait=True, **kw):
		if table == "Optimus Session" and for_update:
			waits.append(wait)
		return current(table, name, *a, for_update=for_update, **kw)

	monkeypatch.setattr(queue.journal.db, "get_value", spy)

	def never():
		raise AssertionError("attaching never asks the workers")

	monkeypatch.setattr(queue.jobs, "_workers_listening", never)
	out = _start(queue)
	assert out["status"] == "already_running" and out["refresh"]["run_id"] == "fake-run"
	assert [kw["job_id"] for _, kw in queue.sent] == ["optimus-ai-fake-run-0"]
	assert waits == [False] and not rows
	assert queue.journal.run["dispatched_at_epoch"] == 50 and queue.journal.run["dispatch_pending"] == 1


@pytest.mark.parametrize("dispatched_at", [30, 50])
def test_a_click_within_the_throttle_takes_no_lock(admitting, monkeypatch, dispatched_at):
	"""The gate is the plain read: a polling click inside the 30 s throttle sends nothing and
	locks nothing."""
	queue = admitting
	_pending(queue, dispatched_at=dispatched_at)
	locks = []
	current = queue.journal.db.get_value

	def spy(table, name, *a, for_update=False, **kw):
		if for_update:
			locks.append(table)
		return current(table, name, *a, for_update=for_update, **kw)

	monkeypatch.setattr(queue.journal.db, "get_value", spy)
	assert _start(queue)["status"] == "already_running"
	assert not queue.sent and not locks


@pytest.mark.parametrize("trouble", ["busy", "unexpected", "raised"])
def test_a_resend_that_fails_never_changes_the_attach_answer(admitting, monkeypatch, rows, trouble):
	queue = admitting
	_pending(queue, dispatched_at=0)

	def refuse(*a, **kw):
		raise _lock_wait() if trouble == "busy" else RuntimeError("fake storage failure")

	if trouble == "raised":  # even past the dispatch's own failure handling
		monkeypatch.setattr(queue.jobs, "_dispatch_pending", refuse)
	else:
		monkeypatch.setattr(queue.journal.mod, "prepare_dispatch", refuse)
	out = _start(queue)
	assert out["status"] == "already_running" and out["refresh"]["run_id"] == "fake-run"
	assert not queue.sent
	assert [type(exc) for _, exc, _ in rows] == ([RuntimeError] if trouble == "unexpected" else [])


def test_a_forced_release_in_a_refused_admission_still_writes_its_row(admitting, rows):
	"""F3 (probe D1): the takeover of an inconsistent expired run commits even when the
	admission is then refused, so its one recovery row must be written too."""
	queue = admitting
	attempt = _crash(queue)
	del queue.journal.db.rows[queue.journal.mod.ATTEMPT][attempt["name"]]
	queue.journal.db.rows[queue.journal.mod.RUN]["other"] = {
		**queue.journal.run,
		"name": "other",
		"run_id": "other",
		"session_name": "other-session",
		"active_session": "other-session",
		"state": "running",
		"lease_until": 900,
	}
	queue.jobs.frappe.conf["optimus_ai_max_active_refreshes"] = 1
	assert _start(queue) == {"status": "refused", "reason": "site_cap"}
	assert queue.journal.db.rows[queue.journal.mod.RUN]["fake-run"]["state"] == "interrupted"
	[(title, exc, context)] = [row for row in rows if row[0] == "optimus ai refresh release"]
	assert title == "optimus ai refresh release" and context["run_id"] == "fake-run"
	assert [row[0] for row in rows] == ["optimus ai refresh release", "optimus ai refresh ended"]


@pytest.mark.parametrize(
	"values",
	[
		# A claim clears the pending flag; it stays set here so the state check alone must keep
		# this click off the session row.
		{"state": "running", "worker_token": "worker", "dispatch_pending": 1, "dispatched_at_epoch": 0},
		{"state": "queued", "dispatch_pending": 0, "dispatched_at_epoch": 0},
	],
	ids=["running", "claimed"],
)
def test_a_click_on_a_run_with_nothing_to_send_takes_no_lock(admitting, monkeypatch, values):
	"""The gate's state and pending checks keep a polling click lock-free: ``prepare_dispatch``
	would refuse such a run too, but only after taking the session row with NOWAIT."""
	queue = admitting
	queue.journal.run.update(lease_until=1800, **values)
	locks = []
	current = queue.journal.db.get_value

	def spy(table, name, *a, for_update=False, **kw):
		if for_update:
			locks.append(table)
		return current(table, name, *a, for_update=for_update, **kw)

	monkeypatch.setattr(queue.journal.db, "get_value", spy)
	assert _start(queue)["status"] == "already_running"
	assert not queue.sent and not locks


def test_a_click_never_re_sends_the_slice_of_an_expired_run(admitting):
	"""F1: an expired queued run is dead, whatever its pending intent: the click takes it over
	(a new run, its own slice) instead of re-sending the old slice to a worker that could not
	claim it."""
	queue = admitting
	queue.journal.run.update(lease_until=40, dispatch_pending=1, dispatched_at_epoch=0)  # now is 50
	out = _start(queue)
	assert out["status"] == "queued" and out["refresh"]["run_id"] != "fake-run"
	assert [kw["run_id"] for _, kw in queue.sent] == [out["refresh"]["run_id"]]
	assert queue.journal.run["state"] == "interrupted"


# --- one row when a call becomes uncertain or a run ends abnormally -------------------------


@pytest.fixture
def ended_rows(queue, monkeypatch):
	"""Every Error Log write as (title, context), with whether a journal transaction was open."""
	logged = []

	def log(title, exc=None, **context):
		logged.append((title, context, queue.journal.db.active))

	monkeypatch.setattr(ai_fix, "log_ai_failure", log)
	return logged


def _titled(logged, title):
	return [context for t, context, *_ in logged if t == title]


def test_a_takeover_of_a_crashed_call_writes_one_uncertain_row_and_one_ended_row(admitting, ended_rows):
	"""A crashed worker's open call became uncertain (its usage unknown, never sent again) and
	its run ended interrupted with no Error Log row at all. Each is one row, written after the
	takeover committed."""
	queue = admitting
	attempt = _crash(queue)
	assert _start(queue)["status"] == "queued"
	assert _titled(ended_rows, "optimus ai refresh uncertain") == [
		{
			"docname": "fake-session-doc",
			"run_id": "fake-run",
			"attempt": attempt["name"],
			"reason": "lease_expired",
		}
	]
	assert _titled(ended_rows, "optimus ai refresh ended") == [
		{
			"docname": "fake-session-doc",
			"run_id": "fake-run",
			"state": "interrupted",
			"reason": "lease_expired",
		}
	]
	assert len(ended_rows) == 2 and not any(active for *_, active in ended_rows)


def test_a_takeover_of_a_run_with_no_open_call_writes_only_the_ended_row(admitting, ended_rows):
	queue = admitting
	queue.journal.run.update(lease_until=40)  # a queued run that never reached a worker; now is 50
	assert _start(queue)["status"] == "queued"
	assert [t for t, *_ in ended_rows] == ["optimus ai refresh ended"]
	assert _titled(ended_rows, "optimus ai refresh ended")[0]["reason"] == "lease_expired"


def test_the_sweep_writes_the_rows_of_each_run_it_ends(queue, monkeypatch, ended_rows):
	attempt = _crash(queue)
	monkeypatch.setattr(queue.jobs.frappe, "get_all", queue.journal.db.get_all, raising=False)
	assert queue.jobs.recover_pending()["interrupted"] == 1
	assert _titled(ended_rows, "optimus ai refresh uncertain") == [
		{
			"docname": "fake-session-doc",
			"run_id": "fake-run",
			"attempt": attempt["name"],
			"reason": "lease_expired",
		}
	]
	assert [c["reason"] for c in _titled(ended_rows, "optimus ai refresh ended")] == ["lease_expired"]
	assert len(ended_rows) == 2 and not any(active for *_, active in ended_rows)


def test_a_cancel_during_a_call_writes_one_uncertain_row(queue, ended_rows):
	"""The call in flight is uncertain until its late reply settles it; a cancel is not an
	abnormal end, so it writes no ended row."""
	attempt = _crash(queue, lease_until=900)
	assert _cancel(queue)["status"] == "cancelled"
	assert ended_rows == [
		(
			"optimus ai refresh uncertain",
			{
				"docname": "fake-session-doc",
				"run_id": "fake-run",
				"attempt": attempt["name"],
				"reason": "cancelled",
			},
			False,
		)
	]


def test_a_cancel_with_no_open_call_writes_no_row(queue, ended_rows):
	assert _cancel(queue)["status"] == "cancelled" and ended_rows == []


def test_the_rows_of_a_takeover_are_committed_before_the_click_goes_on(admitting, monkeypatch):
	"""The rows are kept on their own: a later failure of the click (its dispatch, the web
	request) must not take them with it."""
	queue = admitting
	_crash(queue)
	events = []
	transaction = queue.jobs._transaction

	def tracked(operation):
		out = transaction(operation)
		events.append("commit")
		return out

	monkeypatch.setattr(queue.jobs, "_transaction", tracked)
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda title, exc=None, **kw: events.append(title))
	monkeypatch.setattr(queue.jobs, "_dispatch_pending", lambda *a, **kw: events.append("dispatch"))
	assert _start(queue)["status"] == "queued"
	rows = [i for i, e in enumerate(events) if e.startswith("optimus ai refresh")]
	assert len(rows) == 2 and "commit" in events[rows[-1] : events.index("dispatch")]


@pytest.mark.parametrize("outcome", ["ended", "failed"])
def test_the_rows_of_the_sweep_are_committed_after_the_last_one(queue, monkeypatch, outcome):
	"""The sweep writes its rows after the passes, the rows of each run it ended and of each
	failure, and keeps them: ``bench execute`` or the scheduler may roll back what it leaves."""
	_crash(queue)
	if outcome == "failed":

		def fail(*a, **kw):
			raise RuntimeError("fake storage failure")

		monkeypatch.setattr(queue.journal.mod, "interrupt", fail)
	events = []
	transaction = queue.jobs._transaction

	def tracked(operation):
		out = transaction(operation)
		events.append("commit")
		return out

	monkeypatch.setattr(queue.jobs, "_transaction", tracked)
	monkeypatch.setattr(queue.jobs.frappe, "get_all", queue.journal.db.get_all, raising=False)
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda title, exc=None, **kw: events.append(title))
	counts = queue.jobs.recover_pending()
	assert counts["interrupted" if outcome == "ended" else "failed"] == 1
	rows = [i for i, e in enumerate(events) if e.startswith("optimus ai refresh")]
	assert len(rows) == (2 if outcome == "ended" else 1)
	assert events[rows[-1] + 1 :] == ["commit"]
