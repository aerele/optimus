# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for api.pill_state, with frappe.get_all faked by an in-memory table."""

from __future__ import annotations

from datetime import datetime

import pytest

from optimus import api


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
	def build(sessions=(), runs=(), active=None):
		db = FakeDb(list(sessions), list(runs))
		monkeypatch.setattr(api, "_require_profiler_user", lambda: "alice", raising=False)
		monkeypatch.setattr(api.frappe, "get_all", db.get_all, raising=False)
		monkeypatch.setattr(api, "now_datetime", lambda: datetime(2026, 10, 26, 12, 0))
		monkeypatch.setattr(api.session, "get_active_session_for", lambda user: active)
		monkeypatch.setattr(api.session, "get_session_meta", lambda u: {"docname": "OS-1", "label": "L"})
		return db

	return build


def _s(name, uuid, status, user="alice", recent=True):
	return {"name": name, "session_uuid": uuid, "status": status, "user": user, "recent": recent}


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
		("Recording", "idle"),
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
	"run_status,phase", [("Analyzing", "analyzing"), ("Ready", "ready"), ("Failed", "failed")]
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
