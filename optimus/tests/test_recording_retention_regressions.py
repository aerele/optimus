"""Ordinary captures survive snapshots; missing evidence cannot replace a report."""

from types import SimpleNamespace

import pytest

from optimus import ai_fix, analyze, recording_bundle, session
from optimus.tests.fixtures.ai_refresh_journal import journal as _journal_fixture  # noqa: F401
from optimus.tests.test_ai_report_refresh import report  # noqa: F401


def test_normal_sql_capture_round_trips_beyond_the_old_node_limit(tmp_path):
	frame = {
		"filename": "apps/example/example/api.py",
		"lineno": 42,
		"function": "read_customer",
		"code": "load_customer(name)",
	}
	call = {
		"query": "SELECT name FROM tabCustomer WHERE name = 'CUST-1'",
		"normalized_query": "SELECT name FROM tabCustomer WHERE name = ?",
		"duration": 2.5,
		"stack": [frame] * 20,
		"exact_copies": 1,
		"index": 0,
	}
	rec = {"uuid": "r", "calls": [call] * 1300}
	payload = {"schema": 1, "session_uuid": "s", "recordings": {"r": {"rec": rec}}}
	path = tmp_path / "capture.json.gz"
	path.write_bytes(recording_bundle.encode(payload))
	loaded = recording_bundle.read(str(path), private_root=str(tmp_path), session_uuid="s")
	assert loaded["recordings"]["r"]["rec"] == rec
	assert len(loaded["recordings"]["r"]["rec"]["calls"]) == 1300


def test_over_detail_limit_keeps_every_query_in_a_disclosed_snapshot(monkeypatch, tmp_path):
	frame = {"filename": "apps/example/example/api.py", "lineno": 42, "function": "run"}
	call = {"query": "SELECT name FROM tabUser", "stack": [frame] * 20, "explain_result": [{"rows": 100}]}
	bundle = {
		"schema": 1,
		"session_uuid": "s",
		"recordings": {"r": {"rec": {"uuid": "r", "calls": [call] * 5}}},
	}
	monkeypatch.setattr(recording_bundle, "MAX_JSON_NODES", 500)
	payload, degraded = recording_bundle.encode_snapshot(bundle)
	assert degraded and len(call["stack"]) == 20 and call["explain_result"]
	path = tmp_path / "reduced.json.gz"
	path.write_bytes(payload)
	loaded = recording_bundle.read(str(path), private_root=str(tmp_path), session_uuid="s")
	assert loaded["snapshot_detail"] == "reduced-stacks-without-explain"
	calls = loaded["recordings"]["r"]["rec"]["calls"]
	assert len(calls) == 5 and all(c["query"] == call["query"] for c in calls)
	assert all(len(c["stack"]) == 3 and "explain_result" not in c for c in calls)


def test_retry_reuses_a_snapshot_after_lost_commit_ack(monkeypatch):
	rec = {"uuid": "r", "calls": [{"query": "SELECT 1"}]}
	bundle = {"schema": 1, "session_uuid": "s", "session_state": {}, "recordings": {"r": {"rec": rec}}}
	monkeypatch.setattr(
		analyze,
		"frappe",
		SimpleNamespace(
			cache=SimpleNamespace(
				hget=lambda key, uuid: rec if key == analyze.RECORDER_REQUEST_HASH else None,
				get_value=lambda key: None,
			),
			db=SimpleNamespace(get_value=lambda *a: "/private/files/existing.json.gz"),
		),
	)
	monkeypatch.setattr(session, "get_session_meta", lambda uuid: {})
	monkeypatch.setattr(analyze, "_load_recordings_bundle", lambda doc: bundle)
	monkeypatch.setattr(analyze, "_save_report_file", lambda **kw: pytest.fail("duplicate File created"))
	commits = []
	monkeypatch.setattr(analyze, "safe_commit", lambda: commits.append(True))
	analyze._persist_recordings_file("doc", "s", ["r"])
	assert commits == [True]


def test_real_analyze_orchestrator_never_cleans_up_after_snapshot_failure(monkeypatch):
	from optimus.tests.singleflight_fakes import FlagCache
	from optimus.tests.test_analyze_singleflight_heartbeat import _drive_run

	def fail(*a):
		raise recording_bundle.SnapshotUnavailable("snapshot_write_failed")

	with pytest.raises(recording_bundle.SnapshotUnavailable):
		_drive_run(
			monkeypatch,
			cache=FlagCache(),
			scheduler_disabled=True,
			snapshot=fail,
			cleanup=lambda *a: pytest.fail("deleted unsaved Recorder input"),
		)


@pytest.mark.parametrize("failure", ["size", "file", "binding", "commit", "partial"])
def test_snapshot_failure_is_observable_before_cleanup(monkeypatch, failure):
	rec = {"uuid": "r", "calls": [{"query": "SELECT 1"}]}

	def bind(*args):
		if failure == "binding":
			raise RuntimeError("PRIVATE-SNAPSHOT-CANARY")

	monkeypatch.setattr(
		analyze,
		"frappe",
		SimpleNamespace(
			cache=SimpleNamespace(
				hget=lambda key, uuid: rec if key == analyze.RECORDER_REQUEST_HASH else None,
				get_value=lambda key: None,
			),
			db=SimpleNamespace(set_value=bind, get_value=lambda *a: None),
		),
	)
	monkeypatch.setattr(session, "get_session_meta", lambda uuid: {})
	monkeypatch.setattr(
		analyze, "_save_report_file", lambda **kw: None if failure == "file" else "/private/files/s.json.gz"
	)
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *args, **kw: None)

	def commit():
		if failure == "commit":
			raise RuntimeError("PRIVATE-COMMIT-CANARY")

	monkeypatch.setattr(analyze, "safe_commit", commit)
	if failure == "partial":
		analyze.frappe.cache.hget = lambda key, uuid: (
			rec if uuid == "r" and key == analyze.RECORDER_REQUEST_HASH else None
		)
	if failure == "size":
		monkeypatch.setattr(recording_bundle, "MAX_JSON_BYTES", 8)
	with pytest.raises(recording_bundle.InvalidBundle, match="snapshot"):
		analyze._persist_recordings_file("doc", "s", ["r", "missing"] if failure == "partial" else ["r"])
	assert rec["calls"], "the live recording must remain available for Retry Analyze"


def test_regeneration_with_missing_recordings_preserves_the_old_report(report):  # noqa: F811
	parent = report.journal.db.rows["Optimus Session"]["fake-session-doc"]
	parent["actions"] = [{"recording_uuid": "missing"}]
	parent["total_queries"] = 1300
	with pytest.raises(ValueError, match="recordings"):
		report.mod.render_report("fake-session-doc")
	assert parent["raw_report_file"] == "/private/files/old.html"
	assert parent["raw_report_pdf_file"] == "/private/files/old.pdf"
	assert not report.journal.db.rows.get("File")


@pytest.mark.parametrize("status,expired", [("Failed", True), ("Analyzing", False)])
def test_expired_failed_inputs_include_unpersisted_recordings_and_recheck_retry(monkeypatch, status, expired):
	from datetime import datetime, timedelta

	from optimus import janitor

	clock = datetime(2026, 10, 23)
	changes, cleaned = [], []
	doc = SimpleNamespace(
		status=status,
		modified=clock - timedelta(days=8),
		analyzer_warnings=recording_bundle.SNAPSHOT_FAILURE_WARNING,
		actions=[],
		session_uuid="s",
		db_set=lambda *a: changes.append(a),
	)
	monkeypatch.setattr(
		janitor,
		"frappe",
		SimpleNamespace(
			get_all=lambda *a, **kw: [{"name": "doc"}],
			get_doc=lambda *a, **kw: doc,
			db=SimpleNamespace(rollback=lambda: None),
		),
	)
	monkeypatch.setattr(janitor, "now_datetime", lambda: clock)
	monkeypatch.setattr(janitor, "add_to_date", lambda d, *, days: d + timedelta(days=days))
	monkeypatch.setattr(janitor, "safe_commit", lambda: None)
	monkeypatch.setattr(janitor, "session", SimpleNamespace(get_recordings=lambda uuid: ["not-yet-persisted"]))
	monkeypatch.setattr(analyze, "_cleanup_redis", lambda *a: cleaned.append(a))
	janitor._sweep_failed_snapshot_inputs()
	assert bool(cleaned) is expired and bool(changes) is expired
	if expired:
		assert cleaned == [("s", ["not-yet-persisted"])]
