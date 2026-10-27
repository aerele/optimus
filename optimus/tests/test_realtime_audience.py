"""Session identity, rather than an actor or ambient job, selects the audience."""

from types import SimpleNamespace

import frappe
import pytest

from optimus import analyze, api, realtime
from optimus.line_profile import analyzer


@pytest.fixture
def audience(monkeypatch):
	state = SimpleNamespace(row={"name": "session-doc", "user": "recorder@example.invalid"}, calls=[], lookups=[])
	def get_value(*args, **kwargs):
		state.lookups.append((args, kwargs))
		return state.row
	monkeypatch.setattr(frappe, "db", SimpleNamespace(get_value=get_value), raising=False)
	monkeypatch.setattr(frappe, "publish_realtime", lambda event, payload, **kwargs: state.calls.append((event, payload, kwargs)), raising=False)
	return state


@pytest.mark.parametrize("publish", [
	lambda: analyze._publish_progress(40, "private analyzer", "session-uuid"),
	lambda: analyze._publish_session_event("optimus_session_ready", session_uuid="session-uuid", docname="session-doc"),
	lambda: api._publish_session_event("optimus_session_stopping", session_uuid="session-uuid", docname="session-doc", user="actor@example.invalid"),
	lambda: analyzer._publish("phase_2_run_ready", {"session_uuid": "session-uuid", "parent": "session-doc", "user": "actor@example.invalid"}),
])
def test_all_paths_use_recording_user_and_document_rooms(audience, publish):
	publish()
	expected = [
		{"room": "user:recorder@example.invalid"}, {"room": "doc:Optimus Session/session-doc"},
	]
	if audience.calls[0][0].startswith("phase_2_"):
		expected.append({"room": "user:actor@example.invalid"})
	assert [kwargs for _, _, kwargs in audience.calls] == expected
	assert len({payload["event_id"] for _, payload, _ in audience.calls}) == 1
	assert all(payload["recording_user"] == "recorder@example.invalid" for _, payload, _ in audience.calls)
	assert audience.lookups[0][0][1]["session_uuid"] == "session-uuid"


def test_publications_have_distinct_ids_and_do_not_mutate_the_callers_payload(audience):
	payload = {"session_uuid": "session-uuid", "recording_user": "forged", "event_id": "forged"}
	for _ in range(2):
		realtime.publish_session_event("optimus_session_ready", payload)
	assert payload["recording_user"] == "forged" and payload["event_id"] == "forged"
	assert audience.calls[0][1]["recording_user"] == "recorder@example.invalid"
	assert audience.calls[0][1]["event_id"] not in {"forged", audience.calls[2][1]["event_id"]}


@pytest.mark.parametrize("actor", [None, "Guest", "recorder@example.invalid"])
def test_phase2_does_not_add_an_invalid_or_duplicate_actor_room(audience, actor):
	realtime.publish_session_event("phase_2_run_ready", {"docname": "session-doc", "user": actor})
	assert len(audience.calls) == 2


@pytest.mark.parametrize("row", [None, {}, {"name": "session-doc"}, {"name": "session-doc", "user": "Guest"}])
def test_unresolved_principal_never_emits(audience, row):
	audience.row = row
	realtime.publish_session_event("private", {"session_uuid": "missing", "user": "untrusted-payload"})
	assert audience.calls == []


def test_missing_session_identity_never_emits(audience):
	realtime.publish_session_event("private", {"user": "payload-only"})
	assert audience.calls == [] and audience.lookups == []


def test_emitter_failure_still_attempts_document_room(audience, monkeypatch):
	def emit(event, payload, **kwargs):
		audience.calls.append((event, payload, kwargs))
		if kwargs["room"].startswith("user:"):
			raise RuntimeError("bridge unavailable")
	monkeypatch.setattr(frappe, "publish_realtime", emit)
	realtime.publish_session_event("private", {"docname": "session-doc"})
	assert len(audience.calls) == 2
