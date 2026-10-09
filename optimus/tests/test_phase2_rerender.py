# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Phase 2 ``run_analyze``: once the run is committed Ready, nothing after it can flip it to
Failed. A failed or interrupted re-render keeps the run Ready with a warning that names
Regenerate Reports; an RQ timeout still stops the job, raised fresh; a session deleted
mid-run leaves one neutral log line. Failure logs run outside any ``except`` block."""

import json
import sys
from types import SimpleNamespace

import pytest

from optimus import ai_fix, api, safe_call
from optimus.analyzers.base import AnalyzerResult
from optimus.line_profile import analyzer as lp
from optimus.line_profile import capture

WARNING = "The report re-render did not finish. Use Regenerate Reports on this session to refresh it."


class _JobTimeout(Exception):
	pass


class _Db:
	def __init__(self, events, *, session="PS-1", warnings=None):
		self.events, self.session = events, session
		self.rows = {"RUN-ROW": json.dumps(warnings if warnings is not None else ["earlier"])}
		self.writes = []

	def get_value(self, doctype, filters, fieldname=None, **kw):
		return self.session

	def rollback(self, **kw):
		self.events.append("rollback")


class _Frappe:
	def __init__(self, events, **kw):
		self.db = _Db(events, **kw)
		self.events = events
		self.utils = SimpleNamespace(now_datetime=lambda: "now")

	def get_doc(self, *a, **k):
		return SimpleNamespace(actions=[])

	def get_all(self, doctype, filters=None, fields=None, limit=None, **k):
		assert doctype == "Optimus Phase Two Run" and filters == {"parent": "PS-1", "run_uuid": "run1"}
		return [{"name": "RUN-ROW", "warnings_json": self.db.rows["RUN-ROW"]}]

	def publish_realtime(self, *a, **k):
		pass


@pytest.fixture
def env(monkeypatch):
	events, logged, lines = [], [], []
	fake = _Frappe(events)
	monkeypatch.setattr(lp, "frappe", fake)
	monkeypatch.setattr(lp, "_FRAPPE_AVAILABLE", True)
	monkeypatch.setattr(lp, "_find_run_row", lambda s, r: SimpleNamespace(name="row", parent="PS-1"))
	monkeypatch.setattr(lp, "_persist_run", lambda *a: events.append("persist_run"))
	monkeypatch.setattr(lp, "_mark_run_failed", lambda p, r, err, tb: events.append(f"mark_failed:{err}"))
	monkeypatch.setattr(lp, "_publish", lambda ev, payload: events.append(ev))
	monkeypatch.setattr(lp, "safe_commit", lambda: events.append("commit"))
	monkeypatch.setattr(lp, "analyze", lambda results, call_trees=None: AnalyzerResult(findings=[], aggregate={}, warnings=[]))
	monkeypatch.setattr(capture, "read_all_samples", lambda r: [])
	monkeypatch.setattr(capture, "read_picks_meta", lambda r: {})
	monkeypatch.setattr(capture, "aggregate_samples", lambda s, p: [])
	monkeypatch.setattr(capture, "budget_was_hit", lambda r: False)
	monkeypatch.setattr(capture, "cleanup_run", lambda r: events.append("cleanup_run"))
	monkeypatch.setattr(safe_call, "job_timeout_types", lambda: (_JobTimeout,))
	monkeypatch.setattr(safe_call, "log_error_line", lambda message, **kw: lines.append(message))

	def log_failure(title, exc=None, **kw):
		logged.append((title, type(exc).__name__, kw, sys.exc_info()[0]))
		guard = ai_fix._InterruptGuard()
		guard.note(exc)
		if guard.pending():
			raise guard.interrupt()

	monkeypatch.setattr(ai_fix, "log_ai_failure", log_failure)

	def render_with(effect):
		def render(docname):
			events.append("render")
			if effect is not None:
				raise effect
			return {"regenerated": True}

		monkeypatch.setattr(api, "_render_session_report", render)

	render_with(None)
	return SimpleNamespace(events=events, logged=logged, lines=lines, fake=fake, render_with=render_with, mp=monkeypatch)


def _warnings(env):
	"""The warnings_json the run row was left with (the last write to it)."""
	return json.loads(env.fake.db.rows["RUN-ROW"])


@pytest.fixture(autouse=True)
def _record_warning_writes(env):
	def set_value(doctype, name, field, value, **kw):
		assert (doctype, name, field) == ("Optimus Phase Two Run", "RUN-ROW", "warnings_json")
		# a warning is not an edit of the run: its modified time stays
		assert kw == {"update_modified": False}
		env.fake.db.rows["RUN-ROW"] = value
		env.events.append("warn")

	env.fake.db.set_value = set_value


def test_a_clean_run_cleans_up_then_renders_then_publishes_ready(env):
	lp.run_analyze("u1", "run1")
	assert env.events == [
		"phase_2_run_analyzing", "persist_run", "cleanup_run", "render", "phase_2_run_ready",
	]
	assert env.logged == [] and env.lines == []
	assert _warnings(env) == ["earlier"]


def test_a_failed_rerender_keeps_the_run_ready_with_a_warning(env):
	env.render_with(RuntimeError("render broke"))
	lp.run_analyze("u1", "run1")
	assert not any(e.startswith("mark_failed") for e in env.events)
	assert env.events[-3:] == ["warn", "commit", "phase_2_run_ready"]
	# the failed render is rolled back before the warning is written
	assert env.events.index("rollback") < env.events.index("warn")
	assert "cleanup_run" in env.events[:env.events.index("render")]
	assert _warnings(env) == ["earlier", WARNING]
	# the failure is logged once, with its exception and session, outside any except block
	assert env.logged == [("optimus ai phase 2 re-render", "RuntimeError", {"session_uuid": "u1"}, None)]


def test_an_rq_timeout_in_the_rerender_keeps_ready_and_still_stops_the_job(env):
	original = _JobTimeout("expired")
	env.render_with(original)
	with pytest.raises(_JobTimeout) as caught:
		lp.run_analyze("u1", "run1")
	assert caught.value is not original and caught.value.args == original.args
	assert caught.value.__context__ is None and caught.value.__cause__ is None
	assert not any(e.startswith("mark_failed") or e == "phase_2_run_failed" for e in env.events)
	assert _warnings(env) == ["earlier", WARNING]
	assert "phase_2_run_ready" in env.events
	assert "cleanup_run" in env.events[:env.events.index("render")]


def test_the_warning_is_added_once_to_an_empty_or_unreadable_list(env):
	env.fake.db.rows["RUN-ROW"] = "not json"
	env.render_with(RuntimeError("x"))
	lp.run_analyze("u1", "run1")
	assert _warnings(env) == [WARNING]


def test_a_failure_before_the_run_is_committed_still_marks_it_failed(env):
	env.mp.setattr(capture, "read_all_samples", lambda r: (_ for _ in ()).throw(RuntimeError("redis gone")))
	with pytest.raises(RuntimeError):
		lp.run_analyze("u1", "run1")
	assert "mark_failed:redis gone" in env.events and "rollback" in env.events
	assert "persist_run" not in env.events and "render" not in env.events
	assert env.events[-1] == "phase_2_run_failed"


def test_a_failure_while_committing_the_run_marks_it_failed(env):
	def persist(*a):
		raise RuntimeError("deadlock")

	env.mp.setattr(lp, "_persist_run", persist)
	with pytest.raises(RuntimeError):
		lp.run_analyze("u1", "run1")
	assert "mark_failed:deadlock" in env.events and "render" not in env.events


def test_a_timeout_before_the_run_is_committed_marks_it_failed_and_is_raised(env):
	env.mp.setattr(capture, "read_all_samples", lambda r: (_ for _ in ()).throw(_JobTimeout("expired")))
	with pytest.raises(_JobTimeout):
		lp.run_analyze("u1", "run1")
	assert "mark_failed:expired" in env.events


def test_a_session_deleted_mid_run_leaves_one_neutral_log_line(env):
	env.fake.db.session = None
	lp.run_analyze("u1", "run1")
	assert env.lines == ["optimus: phase 2 report not re-rendered, session u1 no longer exists"]
	assert "render" not in env.events and env.logged == []
	assert not any(e.startswith("mark_failed") for e in env.events)
	assert env.events[-1] == "phase_2_run_ready"


def test_regenerate_parent_reports_says_whether_it_rendered(env):
	assert lp._regenerate_parent_reports("u1") is True
	env.render_with(RuntimeError("x"))
	assert lp._regenerate_parent_reports("u1") is False
	env.fake.db.session = None
	assert lp._regenerate_parent_reports("u1") is False


def test_a_warning_that_cannot_be_written_never_fails_the_run(env):
	def broken(*a, **k):
		raise RuntimeError("db gone")

	env.fake.db.set_value = broken
	env.render_with(RuntimeError("render broke"))
	lp.run_analyze("u1", "run1")
	assert env.events[-1] == "phase_2_run_ready" and not any(e.startswith("mark_failed") for e in env.events)
	assert env.lines == ["optimus: phase 2 could not record the re-render warning: RuntimeError"]


def test_a_run_row_that_is_gone_gets_no_warning(env):
	env.fake.get_all = lambda *a, **k: []
	env.render_with(RuntimeError("render broke"))
	lp.run_analyze("u1", "run1")
	assert "warn" not in env.events and env.events[-1] == "phase_2_run_ready"
	assert not any(e.startswith("mark_failed") for e in env.events)
	assert env.lines == []  # nothing to warn is not a failure to warn


def test_a_warnings_value_that_is_not_a_list_is_replaced(env):
	env.fake.db.rows["RUN-ROW"] = json.dumps({"not": "a list"})
	env.render_with(RuntimeError("x"))
	lp.run_analyze("u1", "run1")
	assert _warnings(env) == [WARNING]
