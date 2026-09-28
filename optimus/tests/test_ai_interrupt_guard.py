# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""``ai_fix._InterruptGuard``: the one place ai_fix.py records an interrupt,
swallows it, and raises it again after the ``try``.

An RQ job timeout leaves as a fresh instance of its type (no chain, none of
the frames it interrupted); with ``base=True`` an interrupt that is not an
``Exception`` (``SystemExit`` from a gunicorn worker timeout,
``KeyboardInterrupt``, a gevent ``Timeout``) leaves as the SAME instance, with
its traceback, ``__context__`` and ``__cause__`` cleared. Anything else goes
through the guard untouched, for the caller's own ``except``.
"""

import ast
from pathlib import Path

import pytest

from optimus import ai_fix

KEY = "sk-live-0123456789abcdefXYZ"


class _JobTimeout(Exception):
	"""Stands in for rq's ``JobTimeoutException`` (an ``Exception``)."""


@pytest.fixture
def job_timeout(monkeypatch):
	monkeypatch.setattr(ai_fix, "_job_timeout_types", lambda: (_JobTimeout,))
	return _JobTimeout("Task exceeded maximum timeout value (60 seconds)")


def _held(secret):
	"""Raise from a frame whose local holds ``secret``."""
	def _raise(exc):
		held = secret  # noqa: F841
		raise exc
	return _raise


class TestTheGuard:
	def test_a_job_timeout_is_swallowed_then_raised_fresh(self, job_timeout):
		raiser = _held(KEY)
		guard = ai_fix._InterruptGuard()
		with guard:
			raiser(job_timeout)
		assert guard.pending()
		with pytest.raises(_JobTimeout) as ei:
			raise guard.interrupt()
		assert not guard.pending()
		assert ei.value is not job_timeout and ei.value.args == job_timeout.args
		assert ei.value.__context__ is None and ei.value.__cause__ is None
		tb, codes = ei.value.__traceback__, set()
		while tb is not None:
			codes.add(tb.tb_frame.f_code)
			tb = tb.tb_next
		assert raiser.__code__ not in codes

	@pytest.mark.parametrize(
		"interrupt",
		[SystemExit(1), KeyboardInterrupt(), type("GreenletTimeout", (BaseException,), {})(5)],
		ids=["SystemExit", "KeyboardInterrupt", "gevent-Timeout"],
	)
	def test_with_base_a_non_exception_interrupt_keeps_its_identity_cleared(self, interrupt):
		raiser = _held(KEY)
		guard = ai_fix._InterruptGuard(base=True)
		with guard:
			try:
				raise ValueError(KEY)
			except ValueError:
				raiser(interrupt)
		with pytest.raises(BaseException) as ei:
			raise guard.interrupt()
		assert ei.value is interrupt
		assert ei.value.__context__ is None and ei.value.__cause__ is None
		assert ei.value.__suppress_context__ is True
		tb, codes = ei.value.__traceback__, set()
		while tb is not None:
			codes.add(tb.tb_frame.f_code)
			tb = tb.tb_next
		assert raiser.__code__ not in codes

	def test_without_base_a_non_exception_interrupt_goes_through(self):
		guard = ai_fix._InterruptGuard()
		with pytest.raises(SystemExit):
			with guard:
				raise SystemExit(1)
		assert not guard.pending() and guard.interrupt() is None

	def test_an_ordinary_exception_goes_through_to_the_callers_except(self, job_timeout):
		guard = ai_fix._InterruptGuard(base=True)
		with pytest.raises(RuntimeError):
			with guard:
				raise RuntimeError("x")
		assert not guard.pending() and guard.interrupt() is None

	def test_nothing_recorded_raises_nothing(self):
		guard = ai_fix._InterruptGuard(base=True)
		with guard:
			pass
		assert not guard.pending() and guard.interrupt() is None

	def test_note_records_a_timeout_passed_in(self, job_timeout):
		guard = ai_fix._InterruptGuard()
		guard.note(RuntimeError("not a timeout"))
		guard.note(None)
		assert not guard.pending()
		guard.note(job_timeout)
		with pytest.raises(_JobTimeout) as ei:
			raise guard.interrupt()
		assert ei.value is not job_timeout and ei.value.args == job_timeout.args

	def test_the_guard_never_holds_the_traceback(self, job_timeout):
		guard = ai_fix._InterruptGuard(base=True)
		with guard:
			raise job_timeout
		assert not hasattr(guard, "__dict__")
		assert all(not isinstance(getattr(guard, s), BaseException) for s in guard.__slots__ if s != "_escaping")


def test_ai_fix_has_one_copy_of_the_interrupt_idiom():
	"""Every site in ai_fix.py uses ``_InterruptGuard``: no hand-copied
	``raise interrupt[0](*interrupt[1])`` or ``except _job_timeout_types() as e``
	record-and-swallow is left outside the guard (a bare ``except _job_timeout_types(): raise``,
	which lets the timeout through to an outer guard, is not the idiom)."""
	tree = ast.parse(Path(ai_fix.__file__).read_text(encoding="utf-8"))
	guard = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "_InterruptGuard")
	inside = {id(n) for n in ast.walk(guard)}
	offenders = []
	for node in ast.walk(tree):
		if id(node) in inside:
			continue
		if isinstance(node, ast.ExceptHandler) and node.name and isinstance(node.type, ast.Call):
			if getattr(node.type.func, "id", None) == "_job_timeout_types":
				offenders.append(node.lineno)
		if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
			func = node.exc.func
			if isinstance(func, ast.Subscript) and isinstance(func.value, ast.Name):
				offenders.append(node.lineno)
	# The non-Exception branch (clear the traceback, the context and the
	# cause, then raise the same instance) lives only in the guard.
	for node in ast.walk(tree):
		if isinstance(node, ast.Attribute) and node.attr == "__traceback__" and isinstance(node.ctx, ast.Store):
			if id(node) not in inside:
				offenders.append(node.lineno)
	assert offenders == []
