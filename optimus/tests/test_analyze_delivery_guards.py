"""Run the Phase 1 orchestrator across duplicate, missing-input and commit failures."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from optimus import analyze, recording_bundle


@pytest.fixture
def delivery(monkeypatch):
	import frappe
	state = SimpleNamespace(status="Stopping", total_queries=0, requested=["r"],
		recordings=[{"uuid": "r", "calls": [{"query": "SELECT 1"}]}],
		events=[], committed_status="Stopping", fail_ready_commit=False, cleanup_failure=False,
		on_acquire=lambda: None, metadata={})
	def value(doctype, name, field=None, **kw):
		if isinstance(name, dict):
			return "doc"
		return {"status": state.status, "total_queries": state.total_queries, "owner": "owner"}.get(field)
	def write(doctype, name, field, value):
		state.events.append(("write", field, value))
		if field == "status":
			state.status = value
	def commit():
		state.events.append(("commit", state.status))
		if state.status == "Ready" and state.fail_ready_commit:
			raise RuntimeError("synthetic Ready commit failure")
		state.committed_status = state.status
	def rollback():
		state.status = state.committed_status
	def record(name):
		return lambda *a, **kw: state.events.append((name,))
	def acquire(*a):
		state.on_acquire()
		return True
	def cleanup(*a):
		state.events.append(("cleanup", state.committed_status))
		if state.cleanup_failure:
			raise ConnectionError("synthetic Redis outage")
		state.recordings.clear()
	monkeypatch.setattr(frappe, "db", SimpleNamespace(get_value=value, set_value=write,
		commit=commit, rollback=rollback), raising=False)
	monkeypatch.setattr(frappe, "local", SimpleNamespace(), raising=False)
	monkeypatch.setattr(frappe, "conf", {"optimus_analyze_gc_collect": False}, raising=False)
	monkeypatch.setattr(frappe, "cache", SimpleNamespace(get_value=lambda *a: None), raising=False)
	monkeypatch.setattr(analyze, "session", SimpleNamespace(get_recordings=lambda s: state.requested,
		get_session_meta=lambda s: state.metadata, delete_session_state=record("delete_session_state")))
	monkeypatch.setattr(analyze, "is_scheduler_disabled", lambda: True)
	monkeypatch.setattr(analyze, "_acquire_singleflight", acquire)
	monkeypatch.setattr(analyze, "_release_singleflight", record("release"))
	monkeypatch.setattr(analyze, "_touch_singleflight", lambda *a: None)
	monkeypatch.setattr(analyze, "_bg_wait_for_pending_jobs", lambda *a: 0)
	monkeypatch.setattr(analyze, "_fetch_recordings", lambda uuids: iter(state.recordings))
	monkeypatch.setattr(analyze, "_get_analyzers", lambda: [])
	monkeypatch.setattr(analyze, "_enrich_recordings", lambda *a: [])
	for name in ("_persist", "_persist_recordings_file", "_render_and_attach_reports",
		"_finalize_with_empty_session", "_auto_arm_phase2", "queue_analyze_time_ai"):
		monkeypatch.setattr(analyze, name, record(name))
	monkeypatch.setattr(analyze, "_publish_progress", lambda *a: None)
	monkeypatch.setattr(analyze, "_publish_session_event", lambda *a, **kw: None)
	monkeypatch.setattr(analyze, "_log_ai_step_failure", record("log"))
	monkeypatch.setattr(analyze, "_cleanup_redis", cleanup)
	return state


@pytest.mark.parametrize("became_ready_while_queued", [False, True])
def test_ready_duplicate_has_no_mutations_or_input_cleanup(delivery, became_ready_while_queued):
	state = delivery
	state.requested, state.recordings, state.total_queries = [], [], 17
	if became_ready_while_queued:
		def finish_previous():
			state.status = state.committed_status = "Ready"
		state.on_acquire = finish_previous
	else:
		state.status = state.committed_status = "Ready"
	analyze.run("s")
	assert state.status == "Ready"
	assert not [e for e in state.events if e[0] in {"write", "cleanup", "_persist", "_persist_recordings_file",
		"_finalize_with_empty_session", "_render_and_attach_reports", "delete_session_state", "queue_analyze_time_ai"}]


@pytest.mark.parametrize("kind", ["partial", "all_missing", "expired", "lost_before_first_analysis"])
def test_missing_expected_inputs_never_finalize_or_delete_last_input(delivery, kind):
	state = delivery
	if kind == "partial":
		state.requested.append("missing")
	elif kind == "all_missing":
		state.recordings = []
	elif kind == "expired":
		state.requested, state.recordings, state.total_queries = [], [], 17
	else:
		state.requested, state.recordings, state.metadata = [], [], None
	before = deepcopy(state.recordings)
	with pytest.raises(recording_bundle.SnapshotUnavailable):
		analyze.run("s")
	assert state.status == "Failed" and state.recordings == before
	assert not [e for e in state.events if e[0] in {"cleanup", "_persist", "_persist_recordings_file",
		"_finalize_with_empty_session", "_render_and_attach_reports", "delete_session_state"}]


def test_genuine_empty_capture_still_finalizes(delivery):
	state = delivery
	state.requested, state.recordings = [], []
	analyze.run("s")
	assert ("_finalize_with_empty_session",) in state.events


def test_empty_capture_renders_report_before_ready(delivery):
	state = delivery
	state.requested, state.recordings = [], []
	analyze.run("s")
	order = [e[0] if e[0] != "write" else e[2] for e in state.events if e[0] in {
		"_finalize_with_empty_session", "_render_and_attach_reports", "write"}]
	assert order.index("_finalize_with_empty_session") < order.index("_render_and_attach_reports") < order.index("Ready")
	assert state.status == "Ready"


def test_empty_capture_publishes_ready_event(delivery, monkeypatch):
	state = delivery
	state.requested, state.recordings = [], []
	sent = []
	monkeypatch.setattr(analyze, "_publish_session_event", lambda name, **kw: sent.append(name))
	analyze.run("s")
	assert sent.count("optimus_session_ready") == 1


def test_empty_capture_render_failure_marks_failed_never_ready(delivery, monkeypatch):
	state = delivery
	state.requested, state.recordings = [], []
	def broken(*a, **kw):
		raise RuntimeError("render failed")
	monkeypatch.setattr(analyze, "_render_and_attach_reports", broken)
	with pytest.raises(RuntimeError, match="render failed"):
		analyze.run("s")
	assert state.status == "Failed"
	assert ("write", "status", "Ready") not in state.events


def test_empty_summary_save_does_not_set_ready(monkeypatch):
	saved = []
	doc = SimpleNamespace(status="Analyzing")
	monkeypatch.setattr(analyze.frappe, "get_doc", lambda *a: doc, raising=False)
	import optimus.managed as managed
	monkeypatch.setattr(managed, "save_session", lambda d: saved.append(d.status))
	monkeypatch.setattr(analyze, "safe_commit", lambda: None)
	analyze._finalize_with_empty_session("OS-1")
	assert saved == ["Analyzing"] and doc.total_queries == 0


def test_ready_commit_failure_retains_input(delivery):
	state = delivery
	state.fail_ready_commit = True
	before = deepcopy(state.recordings)
	with pytest.raises(RuntimeError, match="Ready commit"):
		analyze.run("s")
	assert state.recordings == before
	assert not [e for e in state.events if e[0] == "cleanup"]


def test_cleanup_failure_after_ready_never_demotes_or_skips_optional_admission(delivery):
	state = delivery
	state.cleanup_failure = True
	analyze.run("s")
	assert state.status == state.committed_status == "Ready"
	assert ("cleanup", "Ready") in state.events
	assert ("queue_analyze_time_ai",) in state.events


def test_late_failure_cannot_demote_a_concurrent_ready_result(delivery, monkeypatch):
	state = delivery
	def lost_input(*a):
		state.status = state.committed_status = "Ready"
		raise ConnectionError("a different worker completed while this one failed")
	monkeypatch.setattr(analyze, "_fetch_recordings", lost_input)
	analyze.run("s")
	assert state.status == "Ready"
	assert not [e for e in state.events if e[:2] == ("write", "status") and e[2] == "Failed"]


def test_completed_parent_fences_late_persistence(delivery, monkeypatch):
	state = delivery
	def another_completed(*a):
		state.status = state.committed_status = "Ready"
		return []
	monkeypatch.setattr(analyze, "_enrich_recordings", another_completed)
	analyze.run("s")
	assert state.status == "Ready" and ("_persist",) not in state.events



def test_persistence_conflict_is_not_replayed_with_mutated_trees(delivery, monkeypatch):
	calls = []
	from optimus import sql_work
	def persist(*a):
		calls.append("overflow tree mutated before SQL conflict")
		raise RuntimeError("synthetic deadlock")
	monkeypatch.setattr(analyze, "_persist", persist)
	monkeypatch.setattr(sql_work, "is_retryable_conflict", lambda *a, **kw: True)
	with pytest.raises(RuntimeError, match="synthetic deadlock"):
		analyze.run("s")
	assert len(calls) == 1 and delivery.status == "Failed"
	assert delivery.recordings and not [e for e in delivery.events if e[0] == "cleanup"]
