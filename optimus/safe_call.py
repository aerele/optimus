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
		self._timeout: tuple[type[BaseException], tuple, dict] | None = None
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
			self._timeout = (type(exc), exc.args, self._timeout_state(exc))
			return True
		return False

	def _timeout_state(self, exc) -> dict:
		"""Explicitly allowlisted metadata; never copy arbitrary exception locals."""
		return {}

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

	@staticmethod
	def detach(exc: BaseException) -> BaseException:
		"""Drop private frames while preserving a typed failure or interrupt identity."""
		exc.__traceback__ = None
		exc.__context__ = None
		exc.__cause__ = None
		exc.__suppress_context__ = True
		return exc

	def interrupt(self) -> BaseException | None:
		"""What the guard recorded, ready to raise (see the class), or None; the
		guard forgets it."""
		escaping, self._escaping = self._escaping, None
		if escaping is not None:
			return self.detach(escaping)
		timeout, self._timeout = self._timeout, None
		if timeout is not None:
			fresh = timeout[0](*timeout[1])
			fresh.__dict__.update(timeout[2])
			return fresh
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


_RECENT_LINES: dict[tuple[str | None, str], float] = {}
_RECENT_LINES_CAP = 64
_ERRLOG_KEY_PREFIX = "optimus:errlog:"


def _current_site() -> str | None:
	def _site() -> str | None:
		import frappe

		return getattr(getattr(frappe, "local", None), "site", None)

	return best_effort(_site, None)


def _slot_name(kind: str, text: str) -> bytes:
	import hashlib

	import frappe

	digest = hashlib.sha1(f"{kind}:{text}".encode()).hexdigest()[:16]
	return frappe.cache.make_key(f"{_ERRLOG_KEY_PREFIX}{kind}:{digest}")


def _claim_slot(kind: str, text: str, dedupe_seconds: float) -> bool:
	"""True when this (site, kind, text) has not been logged in the last
	``dedupe_seconds``. The process dict answers repeats inside one process; a Redis
	``SET NX EX`` key shares the window across the web workers and the RQ work horses
	(a horse is forked per job, so a dict alone dies with it). A broken Redis falls back
	to the process dict. The key holds a stable title or reason class, never an id."""
	import time

	key = (_current_site(), f"{kind}:{text}")
	now = time.monotonic()
	last = _RECENT_LINES.get(key)
	if last is not None and now - last < dedupe_seconds:
		return False
	if len(_RECENT_LINES) >= _RECENT_LINES_CAP:
		_RECENT_LINES.clear()
	_RECENT_LINES[key] = now

	def _redis_claim() -> bool:
		import frappe

		# A raw SET is the only way to get NX; the key comes from make_key, so it carries
		# the site prefix that set_value would add.
		return bool(
			frappe.cache.set(  # nosemgrep: frappe-cache-breaks-multitenancy
				_slot_name(kind, text), 1, nx=True, ex=max(1, int(dedupe_seconds))
			)
		)

	return best_effort(_redis_claim, True)


def _release_shared_slot(kind: str, text: str) -> None:
	"""Give the shared Redis slot back, so a write that failed does not hide the next
	failure from the other processes. The process dict keeps its claim: a logger that
	keeps failing is then tried once a window per process, not on every request."""

	def _redis_release() -> None:
		import frappe

		frappe.cache.delete(_slot_name(kind, text))

	best_effort(_redis_release, None)


def log_error_line(message: str, *, dedupe_seconds: float = 60.0, dedupe_key: str | None = None) -> None:
	"""One ``frappe.logger("optimus")`` line at ERROR (Frappe drops lower levels on a
	production site), skipped when the same line was logged on the same site in the last
	``dedupe_seconds`` (per process, and across processes through Redis) so a failure
	that repeats per finding or per request writes one line. ``dedupe_key`` is the
	stable text to dedupe on when ``message`` carries an id that changes per event. It
	never raises (an RQ job timeout excepted) and should be called outside any
	``except``."""
	if not _claim_slot("line", dedupe_key or message, dedupe_seconds):
		return

	def _write() -> None:
		import frappe

		frappe.logger("optimus").error(message)

	best_effort(_write, None)


def log_hook_error(title: str, *, message: str | None = None, dedupe_seconds: float = 60.0) -> None:
	"""``frappe.log_error(title=...)`` for a failing request or job hook, at most once
	per ``dedupe_seconds`` per title and exception class (see ``_claim_slot``) and never
	raising, so a failing logger, a lost connection or an aborted transaction cannot turn
	a customer request into a 500. A second, different failure under one title is
	therefore still written. The shared slot is released when the insert fails. Call it inside
	the ``except``: Frappe reads the active exception for the traceback. Keep ids out of
	``title``; ``message`` may carry them."""
	import sys

	active = sys.exc_info()[1]
	text = f"{title}|{type(active).__name__ if active is not None else ''}"
	if not _claim_slot("title", text, dedupe_seconds):
		return

	def _write() -> bool:
		import frappe

		if message:
			frappe.log_error(title=title, message=message)
		else:
			frappe.log_error(title=title)
		return True

	if not best_effort(_write, False):
		_release_shared_slot("title", text)
