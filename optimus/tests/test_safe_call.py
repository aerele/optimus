# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""safe_call: the one leaf home of the interrupt guard and best_effort.

It imports nothing from Optimus, so the renderer, the AI layer and analyze share it
without importing one another; ai_fix keeps its old names as aliases."""

import ast
import sys
from pathlib import Path

import pytest

from optimus import ai_fix, safe_call

_PKG = Path(safe_call.__file__).resolve().parent


class _JobTimeout(Exception):
	"""Stands in for rq's JobTimeoutException (an Exception)."""


@pytest.fixture
def job_timeout(monkeypatch):
	monkeypatch.setattr(safe_call, "job_timeout_types", lambda: (_JobTimeout,))
	return _JobTimeout("Task exceeded maximum timeout value (60 seconds)")


def _imported_modules(path: Path) -> set[str]:
	tree = ast.parse(path.read_text(encoding="utf-8"))
	out: set[str] = set()
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			out |= {alias.name for alias in node.names}
		elif isinstance(node, ast.ImportFrom):
			out.add("." * node.level + (node.module or ""))
	return out


def test_safe_call_imports_nothing_from_optimus():
	assert {m for m in _imported_modules(_PKG / "safe_call.py") if m.startswith(("optimus", "."))} == set()


def test_ai_fix_never_imports_the_renderer():
	"""The cycle stays broken; the AI layer reads ai_grounding, never the renderer."""
	assert {m for m in _imported_modules(_PKG / "ai_fix.py") if m.startswith("optimus.renderer")} == set()
	assert {m for m in _imported_modules(_PKG / "ai_grounding.py") if m.startswith(("optimus.renderer", "optimus.ai_fix"))} == set()


def test_best_effort_returns_the_value_or_the_default():
	assert safe_call.best_effort(lambda: 3, 0) == 3
	assert safe_call.best_effort(lambda: 1 / 0, "fallback") == "fallback"


def test_on_error_runs_after_the_try_with_no_active_exception():
	seen = []

	def on_error(error_type):
		seen.append((error_type, sys.exc_info()))

	assert safe_call.best_effort(lambda: {}["missing"], None, on_error=on_error) is None
	assert seen == [("KeyError", (None, None, None))]


def test_on_error_is_not_called_on_success():
	seen = []
	assert safe_call.best_effort(lambda: 1, 0, on_error=seen.append) == 1
	assert seen == []


def test_a_job_timeout_escapes_as_a_fresh_instance(job_timeout):
	def interrupted():
		raise job_timeout

	def must_not_run(error_type):
		raise AssertionError("on_error must not run for a job timeout")

	with pytest.raises(_JobTimeout) as caught:
		safe_call.best_effort(interrupted, None, on_error=must_not_run)
	assert caught.value is not job_timeout and caught.value.args == job_timeout.args
	assert caught.value.__context__ is None and caught.value.__cause__ is None
	tb = caught.value.__traceback__
	while tb:
		assert tb.tb_frame.f_code is not interrupted.__code__
		tb = tb.tb_next


def test_a_non_exception_interrupt_passes_through():
	def interrupted():
		raise KeyboardInterrupt

	with pytest.raises(KeyboardInterrupt):
		safe_call.best_effort(interrupted, None)


def test_ai_fix_keeps_its_names_as_aliases(monkeypatch):
	assert issubclass(ai_fix._InterruptGuard, safe_call.InterruptGuard)
	monkeypatch.setattr(ai_fix, "_job_timeout_types", lambda: (_JobTimeout,))
	guard = ai_fix._InterruptGuard()
	with guard:
		raise _JobTimeout("t")
	assert guard.pending()
	assert isinstance(guard.interrupt(), _JobTimeout)


def test_safe_call_holds_the_one_copy_of_the_interrupt_idiom():
	tree = ast.parse((_PKG / "safe_call.py").read_text(encoding="utf-8"))
	guard = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "InterruptGuard")
	inside = {id(n) for n in ast.walk(guard)}
	outside_stores = [
		n.lineno for n in ast.walk(tree)
		if isinstance(n, ast.Attribute) and n.attr == "__traceback__"
		and isinstance(n.ctx, ast.Store) and id(n) not in inside
	]
	assert outside_stores == []
	assert "__traceback__ = None" not in (_PKG / "ai_fix.py").read_text(encoding="utf-8")
