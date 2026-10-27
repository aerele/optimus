"""SQL conflict handling of the refresh journal, as Frappe raises the errors.

Frappe's ``Database.sql`` (v15 and v16) wraps a MariaDB deadlock (1213) and a snapshot
isolation conflict (1020, on by default since MariaDB 11.8) as ``QueryDeadlockError(e)``, a
lock wait timeout (1205, also a NOWAIT conflict) and a Postgres lock-not-available (55P03) or
statement timeout (57014) as ``QueryTimeoutError(e)``, a Postgres deadlock (40P01) as
``QueryDeadlockError(e)``, and leaves a Postgres serialization failure (40001) raw.
``Document.insert`` raises a duplicate key as ``DuplicateEntryError(doctype, name, e)``
(primary key) or ``UniqueValidationError(doctype, name, e)`` (a unique column)."""

from types import SimpleNamespace

import pytest

from optimus import ai_jobs
from optimus import ai_refresh_store as store


class QueryDeadlockError(Exception):
	pass


class QueryTimeoutError(Exception):
	pass


class DuplicateEntryError(NameError):
	pass


class UniqueValidationError(Exception):
	pass


FRAPPE = SimpleNamespace(
	QueryDeadlockError=QueryDeadlockError,
	QueryTimeoutError=QueryTimeoutError,
	DuplicateEntryError=DuplicateEntryError,
	UniqueValidationError=UniqueValidationError,
)


class MariaDBError(Exception):
	"""PyMySQL's shape: ``args == (code, message)``."""


class PostgresError(Exception):
	"""psycopg2's shape: the SQLSTATE in ``pgcode``."""

	def __init__(self, pgcode):
		super().__init__("fake postgres error")
		self.pgcode = pgcode


def wrapped(cls, inner, *args):
	"""``raise cls(*args, inner) from inner``, the way Frappe wraps a driver error."""
	exc = cls(*args, inner)
	exc.__cause__ = inner
	return exc


def maria(code):
	return MariaDBError(code, "fake")


RETRIED = {
	"mariadb deadlock 1213": maria(1213),
	"mariadb lock wait 1205": maria(1205),
	"mariadb snapshot conflict 1020": maria(1020),
	"frappe-wrapped 1213": wrapped(QueryDeadlockError, maria(1213)),
	"frappe-wrapped 1020": wrapped(QueryDeadlockError, maria(1020)),
	"frappe-wrapped 1205": wrapped(QueryTimeoutError, maria(1205)),
	"postgres serialization 40001": PostgresError("40001"),
	"postgres deadlock 40P01": PostgresError("40P01"),
	"postgres lock not available 55P03": PostgresError("55P03"),
	"frappe-wrapped 40P01": wrapped(QueryDeadlockError, PostgresError("40P01")),
	"frappe-wrapped 55P03": wrapped(QueryTimeoutError, PostgresError("55P03")),
	"a deadlock with no driver error": QueryDeadlockError("fake"),
	"a lock timeout with no driver error": QueryTimeoutError("fake"),
}
NOT_RETRIED = {
	"ordinary error": ValueError("fake"),
	"mariadb duplicate 1062": maria(1062),
	"mariadb server gone 2006": maria(2006),
	"postgres statement timeout 57014": wrapped(QueryTimeoutError, PostgresError("57014")),
	"postgres unique violation 23505": PostgresError("23505"),
	"frappe duplicate entry": wrapped(DuplicateEntryError, maria(1062), "fake doctype", "fake name"),
}


@pytest.fixture(autouse=True)
def frappe_errors(monkeypatch):
	monkeypatch.setattr(store, "frappe", FRAPPE)


@pytest.mark.parametrize("exc", RETRIED.values(), ids=RETRIED.keys())
def test_conflicts_frappe_raises_are_retryable(exc):
	assert store.is_retryable_conflict(exc) is True


@pytest.mark.parametrize("exc", NOT_RETRIED.values(), ids=NOT_RETRIED.keys())
def test_other_errors_are_not_retryable(exc):
	assert store.is_retryable_conflict(exc) is False


@pytest.mark.parametrize(
	"exc,busy",
	[
		(maria(1205), True),
		(wrapped(QueryTimeoutError, maria(1205)), True),
		(PostgresError("55P03"), True),
		(wrapped(QueryTimeoutError, PostgresError("55P03")), True),
		(QueryTimeoutError("fake"), True),
		(maria(1213), False),
		(wrapped(QueryDeadlockError, maria(1020)), False),
		(wrapped(QueryTimeoutError, PostgresError("57014")), False),
		(ValueError("fake"), False),
	],
)
def test_nowait_contention_is_recognised_raw_and_wrapped(exc, busy):
	assert store.is_lock_unavailable(exc) is busy


@pytest.mark.parametrize(
	"exc,duplicate",
	[
		(maria(1062), True),
		(PostgresError("23505"), True),
		(wrapped(DuplicateEntryError, maria(1062), "fake doctype", "fake name"), True),
		(wrapped(UniqueValidationError, PostgresError("23505"), "fake doctype", "fake name"), True),
		(DuplicateEntryError("fake doctype", "fake name"), True),
		(maria(1213), False),
		(wrapped(QueryDeadlockError, maria(1213)), False),
		(ValueError("fake"), False),
	],
)
def test_duplicate_keys_are_recognised_raw_and_wrapped(exc, duplicate):
	assert store.is_duplicate_key(exc) is duplicate


def test_a_self_referencing_error_chain_is_classified_without_looping():
	exc = maria(1213)
	exc.__cause__ = exc
	assert store.is_retryable_conflict(exc) is True


@pytest.fixture
def sql_retry(monkeypatch):
	events = []

	class Db:
		def rollback(self):
			events.append("rollback")

	monkeypatch.setattr(ai_jobs, "frappe", SimpleNamespace(db=Db()))
	monkeypatch.setattr(ai_jobs, "safe_commit", lambda: events.append("commit"))
	monkeypatch.setattr(ai_jobs.time, "sleep", lambda seconds: None)
	return events


@pytest.mark.parametrize("exc", RETRIED.values(), ids=RETRIED.keys())
def test_retry_sql_retries_every_conflict_in_short_transactions(sql_retry, exc):
	calls = []

	def operation():
		calls.append(True)
		raise exc

	with pytest.raises(type(exc)):
		ai_jobs._retry_sql(operation)
	assert len(calls) == 3
	assert sql_retry.count("rollback") == 3


@pytest.mark.parametrize("exc", NOT_RETRIED.values(), ids=NOT_RETRIED.keys())
def test_retry_sql_never_retries_other_errors(sql_retry, exc):
	calls = []

	def operation():
		calls.append(True)
		raise exc

	with pytest.raises(type(exc)):
		ai_jobs._retry_sql(operation)
	assert calls == [True]


def test_retry_sql_recovers_from_a_wrapped_deadlock(sql_retry):
	calls = []

	def operation():
		calls.append(True)
		if len(calls) == 1:
			raise RETRIED["frappe-wrapped 1213"]
		return "fake result"

	assert ai_jobs._retry_sql(operation) == "fake result"
	assert len(calls) == 2


def test_retry_sql_starts_the_operation_in_a_fresh_transaction(sql_retry):
	"""The caller's transaction is committed first, so the operation's own first statement (a
	locking read) opens the transaction: no read view from before can turn its lock into a
	1020 on MariaDB 11.8."""

	def operation():
		sql_retry.append("operation")

	ai_jobs._retry_sql(operation)
	assert sql_retry == ["commit", "operation", "commit"]


def test_a_driver_error_is_found_in_the_args_alone_as_document_insert_raises_it():
	"""``Document.insert`` raises ``DuplicateEntryError(doctype, name, e)`` inside the
	``except``: the driver error is an argument and the implicit context, not the cause."""
	exc = DuplicateEntryError("fake doctype", "fake name", maria(1062))
	assert exc.__cause__ is None
	assert store.is_duplicate_key(exc) is True
	assert (
		store.is_retryable_conflict(UniqueValidationError("fake doctype", "fake name", maria(1213))) is True
	)


def test_a_driver_error_is_found_in_the_cause_alone():
	exc = RuntimeError("fake wrapper")
	exc.__cause__ = PostgresError("40001")
	assert store.is_retryable_conflict(exc) is True


def test_settle_starts_the_write_in_a_fresh_transaction(sql_retry):
	"""#72's rule: commit what the caller's transaction holds first, so no read view from
	before the provider call reaches the settlement (a 1020 on MariaDB 11.8)."""

	def operation():
		sql_retry.append("operation")
		return True

	assert ai_jobs._settle(operation, run_id="fake-run", docname="fake-doc", attempt_id="fake", tokens=0)
	assert sql_retry == ["commit", "operation", "commit"]


@pytest.mark.parametrize(
	"exc,calls",
	[
		(RETRIED["frappe-wrapped 1205"], 1),
		(RETRIED["frappe-wrapped 55P03"], 1),
		(RETRIED["mariadb lock wait 1205"], 1),
		(RETRIED["frappe-wrapped 1213"], 3),
		(RETRIED["frappe-wrapped 1020"], 3),
		(RETRIED["postgres serialization 40001"], 3),
		(RETRIED["frappe-wrapped 40P01"], 3),
	],
)
def test_retry_sql_can_leave_lock_wait_timeouts_to_the_caller(sql_retry, exc, calls):
	"""An HTTP admission does not wait a second lock wait timeout; deadlocks, 1020 and
	serialization failures are still retried."""
	tried = []

	def operation():
		tried.append(True)
		raise exc

	with pytest.raises(type(exc)):
		ai_jobs._retry_sql(operation, retry_lock_waits=False)
	assert len(tried) == calls


@pytest.mark.parametrize(
	"exc", [RETRIED["frappe-wrapped 1205"], RETRIED["postgres serialization 40001"]], ids=["1205", "40001"]
)
def test_settlement_also_retries_lock_waits_and_serialization_failures(sql_retry, exc):
	"""Settlement retries more than #72's ``_write_ai_answer`` (whole-transaction rollbacks
	only): its locking reads can time out waiting (1205) or, on Postgres, find the session row
	changed after the snapshot (40001), and the settled bit makes trying again safe. A narrower
	rule would drop the paid answer."""
	tries = []

	def operation():
		tries.append(True)
		if len(tries) == 1:
			raise exc
		return True

	assert ai_jobs._settle(operation, run_id="fake-run", docname="fake-doc", attempt_id="fake", tokens=7)
	assert len(tries) == 2


@pytest.fixture
def unreadable(sql_retry, monkeypatch):
	logged = []
	monkeypatch.setattr(
		ai_jobs.ai_fix, "log_ai_failure", lambda title, exc=None, **kw: logged.append((exc, kw))
	)

	def lost(attempt_id):
		raise ConnectionError("fake lost connection")

	monkeypatch.setattr(store, "attempt_settled", lost)
	return logged


@pytest.mark.parametrize("with_fallback", [False, True])
def test_an_unreadable_outcome_is_logged_as_unknown_not_as_lost(unreadable, with_fallback):
	"""#72's semantics: when the attempt's state cannot be read back after an error,
	its tokens may well be recorded, so the row says unknown rather than "not committed"."""

	def fail():
		raise OSError("fake connection lost during COMMIT")

	with pytest.raises(OSError):
		ai_jobs._settle(
			fail,
			run_id="fake-run",
			docname="fake-doc",
			attempt_id="fake",
			tokens=7,
			fallback=fail if with_fallback else None,
		)
	assert unreadable and all(
		context["outcome"].startswith("unknown") and "may well be recorded" in context["outcome"]
		for exc, context in unreadable
	)
	assert len(unreadable) == (2 if with_fallback else 1)
	assert all(context["tokens"] == 7 for exc, context in unreadable)
