# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Best-effort calls that still let an RQ job timeout stop the job.

A leaf module: it imports nothing from Optimus, so the renderer, the AI layer and
analyze share one copy without importing one another. ``ai_fix`` keeps
``_InterruptGuard`` and ``_job_timeout_types`` as alias names for its own call sites
and tests. ``error_log_mask`` keeps a private copy on purpose: it must import nothing
that can fail inside the Error Log hook.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def job_timeout_types() -> tuple[type[BaseException], ...]:
	"""RQ's job-timeout exception classes (subclasses of ``Exception``), or ``()``
	when rq is not importable (pure unit-test runs)."""
	try:
		from rq.timeouts import BaseTimeoutException
	except Exception:
		return ()
	return (BaseTimeoutException,)


class InterruptGuard:
	"""The record-then-raise-after-the-``try`` idiom::

		guard = InterruptGuard()           # base=True: also non-Exception ones
		try:
			with guard:
				...                          # the guarded work
		except Exception:
			...                          # the site's own handling
		if guard.pending():
			raise guard.interrupt()

	Leaving the ``with`` block, the guard swallows and records:

	- an RQ job timeout (``_timeout_types``): only its type and args; it is raised
	  again as a FRESH instance of that type, with no chain and none of the frames it
	  interrupted (the job must still stop, and those frames can hold secrets);
	- with ``base=True``, an interrupt that is not an ``Exception`` (``SystemExit`` from
	  a gunicorn worker timeout, ``KeyboardInterrupt``, a gevent ``Timeout``): it is
	  raised again as the SAME instance (gevent matches its timeout by identity) with
	  its traceback, ``__context__`` and ``__cause__`` cleared, so Sentry's WSGI
	  middleware never ships the interrupted frames' locals.

	Anything else goes through to the site's own ``except``. The site raises
	``interrupt()`` itself, after the ``try``, where no exception is being handled, so
	it chains nothing. ``note(exc)`` records a timeout that was passed in rather than
	raised. The guard never holds a traceback."""

	__slots__ = ("_base", "_timeout", "_escaping")

	def __init__(self, *, base: bool = False):
		self._base = base
		self._timeout: tuple[type[BaseException], tuple] | None = None
		self._escaping: BaseException | None = None

	def __enter__(self) -> InterruptGuard:
		return self

	def __exit__(self, exc_type, exc, tb) -> bool:
		if exc is None:
			return False
		if self._record_timeout(exc):
			return True
		if self._base and not isinstance(exc, Exception):
			self._escaping = exc
			return True
		return False

	def _timeout_types(self) -> tuple[type[BaseException], ...]:
		"""The timeout classes this guard records (``ai_fix._InterruptGuard`` reads
		them through ``ai_fix._job_timeout_types`` instead)."""
		return job_timeout_types()

	def _record_timeout(self, exc) -> bool:
		timeout_types = self._timeout_types()
		if timeout_types and isinstance(exc, timeout_types):
			self._timeout = (type(exc), exc.args)
			return True
		return False

	def note(self, exc: BaseException | None) -> None:
		"""Record ``exc`` when it is an RQ job timeout, so it is raised again,
		fresh. Never raises."""
		try:
			if exc is not None:
				self._record_timeout(exc)
		except Exception:
			pass

	def pending(self) -> bool:
		"""True when the guard recorded an interrupt to raise again."""
		return self._timeout is not None or self._escaping is not None

	def interrupt(self) -> BaseException | None:
		"""What the guard recorded, ready to raise (see the class), or None; the
		guard forgets it."""
		escaping, self._escaping = self._escaping, None
		if escaping is not None:
			escaping.__traceback__ = None
			escaping.__context__ = None
			escaping.__cause__ = None
			escaping.__suppress_context__ = True
			return escaping
		timeout, self._timeout = self._timeout, None
		if timeout is not None:
			return timeout[0](*timeout[1])
		return None


def best_effort(
	fn: Callable[[], Any], default: Any, *, on_error: Callable[[str], None] | None = None,
) -> Any:
	"""``fn()``, or ``default`` when it raises an ordinary ``Exception``.

	An RQ job timeout escapes as a fresh instance after the ``try`` (the job must still
	stop); a non-``Exception`` interrupt is never caught. ``on_error`` receives the
	failed call's exception type name AFTER the ``try``, never inside the ``except``,
	so a logger it calls never sees an active exception."""
	guard = InterruptGuard()
	value = default
	error_type: str | None = None
	try:
		with guard:
			value = fn()
	except Exception as exc:
		error_type = type(exc).__name__
		value = default
	if guard.pending():
		raise guard.interrupt()
	if error_type is not None and on_error is not None:
		on_error(error_type)
	return value


_RECENT_LINES: dict[str, float] = {}
_RECENT_LINES_CAP = 64


def log_error_line(message: str, *, dedupe_seconds: float = 60.0) -> None:
	"""One ``frappe.logger("optimus")`` line at ERROR (Frappe drops lower levels on a
	production site), skipped when the same text was logged in the last ``dedupe_seconds``
	so a failure that repeats per finding writes one line. It never raises (an RQ job
	timeout excepted) and must be called outside any ``except``."""
	import time

	now = time.monotonic()
	last = _RECENT_LINES.get(message)
	if last is not None and now - last < dedupe_seconds:
		return
	if len(_RECENT_LINES) >= _RECENT_LINES_CAP:
		_RECENT_LINES.clear()
	_RECENT_LINES[message] = now

	def _write() -> None:
		import frappe

		frappe.logger("optimus").error(message)

	best_effort(_write, None)
