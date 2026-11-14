"""The form offers Retry only for runs a manual retry can still admit."""

import types

import pytest

from optimus.line_profile import capture, jobs


def _row(run_uuid, status="Failed", attempts=0):
	return types.SimpleNamespace(run_uuid=run_uuid, status=status, analyze_attempts=attempts)


def test_runs_with_no_retries_left_or_no_input_are_not_retryable(monkeypatch):
	monkeypatch.setattr(capture, "runs_with_input", lambda runs: {run for run in runs if run != "gone"})
	rows = [
		_row("ok"), _row("busy", "Analyzing", 2), _row("spent", attempts=jobs.MAX_ATTEMPTS),
		_row("gone"), _row("done", "Ready"), _row("live", "Recording"), _row("none", attempts=None),
	]
	assert jobs.retryable_runs(rows) == ["ok", "busy", "none"]
	assert jobs.retryable_runs(None) == []


@pytest.mark.parametrize("exists,expected", [(1, True), (0, False)])
def test_has_input_checks_the_runs_picks_key(monkeypatch, exists, expected):
	import frappe

	seen = []
	monkeypatch.setattr(frappe, "cache", types.SimpleNamespace(exists=lambda key: seen.append(key) or exists), raising=False)
	monkeypatch.setattr(capture, "_require_frappe", lambda: None)
	assert capture.has_input("run-1") is expected
	assert seen == ["profiler:lp:run-1:picks"]


def test_redis_failure_keeps_retry_offered(monkeypatch):
	def boom(runs):
		raise ConnectionError("synthetic Redis failure")
	monkeypatch.setattr(capture, "runs_with_input", boom)
	assert jobs.retryable_runs([_row("a"), _row("spent", attempts=jobs.MAX_ATTEMPTS)]) == ["a"]


class _Pipe:
	def __init__(self, present):
		self.present, self.asked = present, []

	def __enter__(self):
		return self

	def __exit__(self, *exc):
		return False

	def exists(self, key):
		self.asked.append(key)

	def execute(self):
		return [1 if key in self.present else 0 for key in self.asked]


def test_runs_with_input_reads_every_run_in_one_round_trip(monkeypatch):
	import frappe

	pipes = []
	def pipeline():
		pipes.append(_Pipe({"site|profiler:lp:a:picks"}))
		return pipes[-1]
	cache = types.SimpleNamespace(pipeline=pipeline, make_key=lambda key: f"site|{key}")
	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	monkeypatch.setattr(capture, "_require_frappe", lambda: None)
	assert capture.runs_with_input(["a", "b"]) == {"a"}
	assert len(pipes) == 1 and pipes[0].asked == ["site|profiler:lp:a:picks", "site|profiler:lp:b:picks"]
	assert capture.runs_with_input([]) == set() and len(pipes) == 1


def test_is_live_needs_a_leased_analyzing_generation():
	row = {"status": "Analyzing", "analyze_generation": "g", "analyze_lease_until": 200}
	assert jobs.is_live(row, now=100)
	assert not jobs.is_live({**row, "analyze_lease_until": 50}, now=100)
	assert not jobs.is_live({**row, "analyze_generation": None}, now=100)
	assert not jobs.is_live({**row, "status": "Failed"}, now=100)
	assert not jobs.is_live(None, now=100)
