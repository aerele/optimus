# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for api.pill_state, with frappe.get_all faked by an in-memory table."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from optimus import api, session


class FakeDb:
	def __init__(self, sessions, runs):
		self.sessions = sessions
		self.runs = runs
		self.calls = []

	def get_all(self, doctype, filters=None, fields=None, limit=None, order_by=None, **kw):
		self.calls.append((doctype, filters))
		if doctype == "Optimus Session":
			rows = [s for s in self.sessions if self._match(s, filters)]
		else:
			rows = [r for r in self.runs if self._match(r, filters)]
		return rows[: limit or None]

	def get_value(self, doctype, name, field):
		return next((s[field] for s in self.sessions if s["name"] == name), None)

	@staticmethod
	def _match(row, filters):
		for key, cond in (filters or {}).items():
			if key == "modified":
				if not row.get("recent", True):
					return False
			elif isinstance(cond, list) and cond[0] == "in":
				if row.get(key) not in cond[1]:
					return False
			elif row.get(key) != cond:
				return False
		return True


@pytest.fixture
def env(monkeypatch):
	from optimus.line_profile import capture

	def build(sessions=(), runs=(), active=None, lp_active=None, pointer_gone=True, cap_warning=None):
		db = FakeDb(list(sessions), list(runs))
		monkeypatch.setattr(api.frappe, "db", db, raising=False)
		monkeypatch.setattr(capture, "is_active", lambda user, fresh=False: lp_active)
		monkeypatch.setattr(api, "_require_profiler_user", lambda: "alice", raising=False)
		monkeypatch.setattr(api.frappe, "get_all", db.get_all, raising=False)
		monkeypatch.setattr(api, "now_datetime", lambda: NOW)
		monkeypatch.setattr(api.session, "get_active_session_for", lambda user: active)
		monkeypatch.setattr(api, "get_datetime", lambda value: value)
		monkeypatch.setattr(api, "add_to_date", lambda d, **kw: d + timedelta(**kw))
		monkeypatch.setattr(api.session, "active_pointer_gone", lambda user: pointer_gone)
		meta = {"docname": "OS-1", "label": "L", "cap_warning": cap_warning}
		monkeypatch.setattr(api.session, "get_session_meta", lambda u: meta)
		return db

	return build


NOW = datetime(2026, 10, 26, 12, 0)


def _s(name, uuid, status, user="alice", recent=True, age_minutes=20):
	return {
		"name": name, "session_uuid": uuid, "status": status, "user": user, "recent": recent,
		"started_at": NOW - timedelta(minutes=age_minutes),
	}


def test_active_session_reports_recording(env):
	env(active="u1")
	assert api.pill_state()["phase"] == "recording"


def test_tracked_session_not_masked_by_a_new_recording(env):
	env(sessions=[_s("OS-1", "u1", "Ready")], active="u2")
	assert api.pill_state(session_uuid="u1")["phase"] == "ready"
	assert api.pill_state(session_uuid="u2")["phase"] == "recording"


@pytest.mark.parametrize(
	"status,phase",
	[
		("Stopping", "analyzing"),
		("Capturing Background Jobs", "analyzing"),
		("Analyzing", "analyzing"),
		("Ready", "ready"),
		("Failed", "failed"),
		("Recording", "ended"),
	],
)
def test_known_session_status_maps_to_phase(env, status, phase):
	env(sessions=[_s("OS-1", "u1", status)])
	out = api.pill_state(session_uuid="u1")
	assert out["phase"] == phase
	if phase != "idle":
		assert out["docname"] == "OS-1" and out["scope"] == "session"


def test_ready_session_is_never_reported_analyzing(env):
	env(sessions=[_s("OS-1", "u1", "Ready")])
	assert api.pill_state(session_uuid="u1")["phase"] == "ready"


def test_other_users_session_is_invisible(env):
	env(sessions=[_s("OS-9", "u9", "Analyzing", user="bob")])
	assert api.pill_state(session_uuid="u9") == {"phase": "idle"}
	assert api.pill_state() == {"phase": "idle"}


def test_non_string_ids_are_rejected(env):
	env()
	with pytest.raises(Exception):
		api.pill_state(session_uuid=["!=", ""])
	with pytest.raises(Exception):
		api.pill_state(session_uuid="u1", run_uuid={"a": 1})


def test_unknown_session_is_idle(env):
	env()
	assert api.pill_state(session_uuid="nope") == {"phase": "idle"}


def test_in_flight_lookup_finds_analyzing_session(env):
	env(sessions=[_s("OS-2", "u2", "Ready"), _s("OS-1", "u1", "Analyzing")])
	out = api.pill_state()
	assert (out["phase"], out["session_uuid"]) == ("analyzing", "u1")


def test_in_flight_lookup_ignores_sessions_outside_window(env):
	env(sessions=[_s("OS-1", "u1", "Analyzing", recent=False)])
	assert api.pill_state() == {"phase": "idle"}


def test_in_flight_lookup_finds_phase2_run_analyzing(env):
	env(
		sessions=[_s("OS-1", "u1", "Ready")],
		runs=[{"parent": "OS-1", "parenttype": "Optimus Session", "run_uuid": "r1", "status": "Analyzing"}],
	)
	out = api.pill_state()
	assert (out["phase"], out["scope"], out["run_uuid"]) == ("analyzing", "phase2", "r1")


@pytest.mark.parametrize(
	"run_status,phase",
	[("Recording", "recording"), ("Analyzing", "analyzing"), ("Ready", "ready"), ("Failed", "failed")],
)
def test_phase2_run_status_by_ids(env, run_status, phase):
	env(
		sessions=[_s("OS-1", "u1", "Ready")],
		runs=[{"parent": "OS-1", "parenttype": "Optimus Session", "run_uuid": "r1", "status": run_status}],
	)
	out = api.pill_state(session_uuid="u1", run_uuid="r1")
	assert (out["phase"], out["scope"]) == (phase, "phase2")


def test_phase2_unknown_run_is_idle(env):
	env(sessions=[_s("OS-1", "u1", "Ready")])
	assert api.pill_state(session_uuid="u1", run_uuid="zz") == {"phase": "idle"}


def _run(status, run_uuid="r1"):
	return {"parent": "OS-1", "parenttype": "Optimus Session", "run_uuid": run_uuid, "status": status}


def test_line_profiling_recording_is_reported_after_a_reload(env):
	env(sessions=[_s("OS-1", "u1", "Ready")], runs=[_run("Recording")], lp_active="r1")
	assert api.pill_state() == {
		"phase": "recording", "scope": "phase2", "session_uuid": "u1", "docname": "OS-1", "run_uuid": "r1",
	}


def test_line_profiling_recording_on_another_users_session_is_reported(env):
	env(sessions=[_s("OS-1", "u1", "Ready", user="bob")], runs=[_run("Recording")], lp_active="r1")
	assert (api.pill_state()["phase"], api.pill_state()["run_uuid"]) == ("recording", "r1")


def test_a_stale_line_profiling_flag_falls_back_to_the_in_flight_lookup(env):
	env(sessions=[_s("OS-1", "u1", "Ready")], runs=[_run("Analyzing")], lp_active="r1")
	assert (api.pill_state()["phase"], api.pill_state()["scope"]) == ("analyzing", "phase2")
	env(sessions=[_s("OS-1", "u1", "Ready")], lp_active="gone")
	assert api.pill_state() == {"phase": "idle"}


def test_a_recording_row_without_its_pointer_has_ended(env):
	env(sessions=[_s("OS-1", "u1", "Recording")])
	assert api.pill_state(session_uuid="u1") == {
		"phase": "ended", "scope": "session", "session_uuid": "u1", "docname": "OS-1", "cap_hit": False,
	}


def test_an_ended_recording_reports_whether_the_request_limit_was_hit(env):
	env(sessions=[_s("OS-1", "u1", "Recording")], cap_warning="limit reached")
	assert api.pill_state(session_uuid="u1")["cap_hit"] is True


def test_a_recording_row_younger_than_the_pointer_lifetime_is_not_ended(env):
	env(sessions=[_s("OS-1", "u1", "Recording", age_minutes=2)])
	assert api.pill_state(session_uuid="u1")["phase"] == "idle"
	assert api.pill_state()["phase"] == "idle"


def test_a_recording_row_exactly_at_the_pointer_lifetime_is_ended(env):
	env(sessions=[_s("OS-1", "u1", "Recording", age_minutes=10)])
	assert api.pill_state(session_uuid="u1")["phase"] == "ended"


def test_an_unconfirmed_pointer_read_is_never_ended(env):
	env(sessions=[_s("OS-1", "u1", "Recording")], pointer_gone=False)
	assert api.pill_state(session_uuid="u1")["phase"] == "idle"
	assert api.pill_state()["phase"] == "idle"


def _cache(get):
	return type("C", (), {"get": staticmethod(get), "make_key": staticmethod(lambda key: key)})()


def test_the_pointer_is_gone_only_when_redis_answers_with_nothing(monkeypatch):
	monkeypatch.setattr(session.frappe, "cache", _cache(lambda key: None), raising=False)
	assert session.active_pointer_gone("alice") is True
	monkeypatch.setattr(session.frappe, "cache", _cache(lambda key: b"pickled"), raising=False)
	assert session.active_pointer_gone("alice") is False


def test_a_redis_error_on_the_pointer_read_is_not_gone(monkeypatch):
	def boom(key):
		raise ConnectionError("redis down")

	monkeypatch.setattr(session.frappe, "cache", _cache(boom), raising=False)
	assert session.active_pointer_gone("alice") is False


def test_tracking_an_expired_session_while_another_is_active_reports_the_old_one_ended(env):
	env(sessions=[_s("OS-1", "u1", "Recording")], active="u2", pointer_gone=False)
	assert api.pill_state(session_uuid="u1")["phase"] == "ended"
	assert api.pill_state(session_uuid="u1")["session_uuid"] == "u1"


def test_a_live_pointer_still_reports_recording(env):
	env(sessions=[_s("OS-1", "u1", "Recording")], active="u1")
	assert api.pill_state(session_uuid="u1")["phase"] == "recording"


def test_the_in_flight_lookup_finds_an_ended_recording(env):
	env(sessions=[_s("OS-1", "u1", "Recording")])
	out = api.pill_state()
	assert (out["phase"], out["session_uuid"]) == ("ended", "u1")


def test_the_in_flight_lookup_skips_a_recent_recording_row(env):
	env(sessions=[_s("OS-1", "u1", "Recording", age_minutes=1)])
	assert api.pill_state() == {"phase": "idle"}


def test_another_users_recording_row_is_idle(env):
	env(sessions=[_s("OS-9", "u9", "Recording", user="bob")])
	assert api.pill_state(session_uuid="u9") == {"phase": "idle"}
	assert api.pill_state() == {"phase": "idle"}


def test_a_recording_row_without_a_start_time_is_idle_and_never_parsed(env, monkeypatch):
	env(sessions=[{**_s("OS-1", "u1", "Recording"), "started_at": None}])

	def boom(value):
		raise AssertionError("get_datetime must not be called for a missing start time")

	monkeypatch.setattr(api, "get_datetime", boom)
	assert api.pill_state(session_uuid="u1")["phase"] == "idle"
	assert api.pill_state()["phase"] == "idle"
