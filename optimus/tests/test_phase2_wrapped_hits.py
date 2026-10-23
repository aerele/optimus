"""Use the actual extension to verify decorated function source receives hits."""

import functools

import pytest

from optimus.line_profile import capture


def _decorate(fn):
	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		return fn(*args, **kwargs)
	return wrapper


def _profile_target(value):
	result = value + 1
	return result * 2


def test_decorated_callable_profiles_the_unwrapped_source(monkeypatch):
	pytest.importorskip("line_profiler")
	decorated = _decorate(_profile_target)
	monkeypatch.setattr(capture, "_get_or_resolve_picks", lambda run: [decorated])
	profiler = capture.make_profiler("wrapped-run")
	assert profiler is not None
	profiler.enable_by_count()
	try:
		assert [decorated(i) for i in range(3)] == [2, 4, 6]
	finally:
		profiler.disable_by_count()
	rows = capture.serialize_stats(profiler)
	assert any(row["lineno"] == _profile_target.__code__.co_firstlineno + 1 and row["hits"] == 3 for row in rows), rows
