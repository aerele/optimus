"""Short SQL transactions shared by AI, Phase 2 and report replacement.

Callers supply commit/rollback and framework exception types. This module owns
conflict classification and fresh interrupt propagation; it never sends HTTP,
enqueues work or makes a domain transition. Retry only SQL operations whose
effects are rolled back or fenced by a durable outcome.
"""

import re
import time

from optimus import safe_call


class TransactionFailed(RuntimeError):
	"""Rollback failed; no underlying exception text is exposed."""


# Driver codes of a conflict that ended the statement or the whole transaction, so a short
# journal transaction can start again from a rollback: MariaDB deadlock 1213, lock wait
# timeout 1205 (also a NOWAIT conflict) and 1020 "Record has changed since last read"
# (innodb_snapshot_isolation, on by default since MariaDB 11.8); Postgres serialization
# failure 40001, deadlock 40P01 and lock not available 55P03.
_RETRY_CODES = frozenset({1213, 1205, 1020, "40001", "40P01", "55P03"})
_LOCK_UNAVAILABLE_CODES = frozenset({1205, "55P03"})
_DUPLICATE_CODES = frozenset({1062, "23505"})


def _driver_codes(exc):
	"""The driver error codes on ``exc`` and on the driver errors Frappe wrapped it around.

	``Database.sql`` raises ``QueryDeadlockError(e)`` / ``QueryTimeoutError(e)`` from ``e``
	and ``Document.insert`` raises ``DuplicateEntryError(doctype, name, e)``, so the driver
	error is one of the args (and the cause). PyMySQL keeps the code in ``args[0]``,
	psycopg2 in ``pgcode``."""
	codes, seen, stack = set(), set(), [exc]
	while stack and len(seen) < 8:
		layer = stack.pop()
		if not isinstance(layer, BaseException) or id(layer) in seen:
			continue
		seen.add(id(layer))
		pgcode = getattr(layer, "pgcode", None)
		if isinstance(pgcode, str) and pgcode:
			codes.add(pgcode)
		args = getattr(layer, "args", ())
		if args and type(args[0]) is int:
			codes.add(args[0])
		stack.extend(arg for arg in args if isinstance(arg, BaseException))
		stack.append(layer.__cause__)
	return codes


def _frappe_error(exc, name, framework):
	cls = getattr(framework, name, None)
	return isinstance(cls, type) and isinstance(exc, cls)


def is_database_error(exc, *, framework=None):
	"""Infrastructure failures must not be attributed to one malformed finding."""
	drivers = {"pymysql", "MySQLdb", "psycopg", "psycopg2", "sqlite3"}
	return any(cls.__module__.split(".")[0] in drivers for cls in type(exc).__mro__) or any(
		_frappe_error(exc, name, framework) for name in ("QueryDeadlockError", "QueryTimeoutError", "DuplicateEntryError")
	)


def is_retryable_conflict(exc, *, framework=None) -> bool:
	"""True when a short journal transaction may be rolled back and tried again: a
	deadlock, a lock wait, a snapshot or serialization conflict, raw or as Frappe wraps it.

	``frappe.QueryDeadlockError`` always is (the database ended the whole transaction, as
	``analyze._whole_transaction_rolled_back`` treats it). ``frappe.QueryTimeoutError`` is
	when it wraps a lock conflict, or no driver error at all; a Postgres statement timeout
	(57014) is not a conflict. Never a reason to repeat a provider call."""
	codes = _driver_codes(exc)
	if _frappe_error(exc, "QueryDeadlockError", framework) or codes & _RETRY_CODES:
		return True
	return _frappe_error(exc, "QueryTimeoutError", framework) and not codes


def is_lock_unavailable(exc, *, framework=None) -> bool:
	"""True when a lock was not granted: a NOWAIT conflict or a lock wait timeout (MariaDB
	1205, Postgres 55P03), raw or wrapped by Frappe as ``QueryTimeoutError``."""
	codes = _driver_codes(exc)
	if codes & _LOCK_UNAVAILABLE_CODES:
		return True
	return _frappe_error(exc, "QueryTimeoutError", framework) and not codes


def is_duplicate_key(exc, *, framework=None) -> bool:
	"""True when an INSERT hit an existing key (MariaDB 1062, Postgres 23505), raw or as
	``Document.insert`` raises it (``DuplicateEntryError``, ``UniqueValidationError``)."""
	codes = _driver_codes(exc)
	if codes & _DUPLICATE_CODES:
		return True
	return not codes and (
		_frappe_error(exc, "DuplicateEntryError", framework) or _frappe_error(exc, "UniqueValidationError", framework)
	)


def transaction(operation, *, commit, rollback, error_type=TransactionFailed):
	"""A short owned transaction, with fresh interrupts after rollback."""
	guard = safe_call.InterruptGuard(base=True)
	failure = result = None
	try:
		with guard:
			result = operation()
			commit()
	except Exception as exc:
		failure = exc
	if failure is not None or guard.pending():
		rollback_error = None
		try:
			with guard:
				rollback()
		except Exception as exc:
			rollback_error = type(exc).__name__
		if guard.pending():
			operation = result = failure = None
			raise guard.interrupt()
		if rollback_error:
			operation = result = failure = None
			raise error_type(f"SQL transaction rollback failed ({rollback_error})")
		raise failure
	return result


def retry(operation, *, transaction, framework=None, retry_lock_waits=True, sleep=time.sleep):
	"""Run one journal operation in its own short transaction; retry SQL conflicts only.

	The caller's transaction is committed first, so the operation's first statement (the
	locking read of the session row) opens the transaction. A read view opened before it
	would turn that lock into a 1020 on MariaDB 11.8 once the lock holder commits.

	A conflict (``store.is_retryable_conflict``: a deadlock, a lock wait, a snapshot or
	serialization conflict, raw or as Frappe wraps it) rolls back and runs the operation
	again, at most three transactions in all, with a short delay. Anything else is raised
	at once. The operation never contacts a provider, so a retry never repeats a call. A
	busy admission mutex is a refusal returned by ``store.admit``, not a conflict.

	``retry_lock_waits=False`` (an HTTP admission) raises a lock wait timeout (MariaDB 1205,
	Postgres 55P03) at once instead of waiting another timeout; deadlocks, snapshot and
	serialization conflicts are still retried. Worker and settlement paths retry it.
	"""
	transaction(lambda: None)
	for attempt in range(3):
		guard = safe_call.InterruptGuard(base=True)
		failure = None
		try:
			with guard:
				return transaction(operation)
		except Exception as exc:
			failure = exc
		if guard.pending():
			operation = failure = None
			raise guard.interrupt()
		if (
			not is_retryable_conflict(failure, framework=framework)
			or (not retry_lock_waits and is_lock_unavailable(failure, framework=framework))
			or attempt == 2
		):
			raise failure
		failure = None
		sleep(0.02 * (attempt + 1))


def bound_http_lock_wait(db, seconds=30):
	"""On Postgres, end this transaction's row lock waits after ``seconds`` at
	most: the first statements of an HTTP admission or cancellation.

	Postgres waits for a row lock for as long as it takes unless ``lock_timeout`` is set, and
	Frappe sets none, so a click on a session row that a dead or slow transaction holds would
	wait until the web server killed the request, and its backend would go on waiting. The
	current limit is read first: a stricter one of the role, the database or the server is kept;
	otherwise ``SET LOCAL`` sets the bound, which ends with this transaction, so the pooled
	connection keeps its own setting. ``SHOW`` and ``SET`` take no snapshot (a SELECT would), so
	the session lock is still the first read. A lock not granted in time raises 55P03, which both
	paths answer as ``busy``. MariaDB gets no statement: a session-level one would outlive the
	transaction on the pooled connection, so the server's ``innodb_lock_wait_timeout`` applies.
	Worker paths retry a lock wait and set nothing."""
	if db.db_type != "postgres":
		return
	rows = db.sql("SHOW lock_timeout", as_dict=True)
	current = lock_timeout_ms(rows[0].get("lock_timeout")) if rows else None
	if current and current <= seconds * 1000:
		return
	db.sql("SET LOCAL lock_timeout = %s", (f"{seconds}s",))


_PG_TIME_UNITS = {"ms": 1, "s": 1000, "min": 60_000, "h": 3_600_000, "d": 86_400_000}


def lock_timeout_ms(value):
	"""A Postgres time setting as ``SHOW`` prints it (``0``, ``250ms``, ``2s``, ``1min``) in
	milliseconds, or None when it cannot be read."""
	match = re.fullmatch(r"(\d+)(ms|s|min|h|d)?", str(value).strip())
	return int(match[1]) * _PG_TIME_UNITS[match[2] or "ms"] if match else None


def touch_session(db, name, **values):
	from frappe.utils import now_datetime

	values = {"modified": now_datetime(), **values}
	db.set_value("Optimus Session", name, values, update_modified=False)
	return values
