# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the streaming _fetch_recordings + tree/sidecar load."""

import base64
import gzip
import json
import pickle
from types import SimpleNamespace

import pytest

from optimus import analyze


@pytest.fixture(autouse=True)
def explicit_legacy_read_policy(monkeypatch):
	import frappe
	# These compatibility cases deliberately request the old Redis formats.
	# The signed-site default and downgrade tests live in test_recording_tree_security.
	monkeypatch.setattr(frappe, "conf", {"optimus_allow_unsigned_pickles": True}, raising=False)


def _signed_tree(obj):
	"""Round-trip a fixture tree through the HMAC envelope so it survives the
	verify step in ``_deserialize_tree`` (matches how the recorder stores it)."""
	from optimus.session import sign_blob

	return sign_blob(pickle.dumps(obj))


class FakeCache:
	def __init__(self, store=None):
		self.store = store or {}

	def hget(self, hash_name, key):
		return self.store.get((hash_name, key))

	def get_value(self, key):
		return self.store.get(key)

	def hdel(self, hash_name, key):
		self.store.pop((hash_name, key), None)

	def delete_value(self, key):
		self.store.pop(key, None)


def test_fetch_recordings_yields_tree_and_sidecar(monkeypatch):
	import frappe
	from frappe.recorder import RECORDER_REQUEST_HASH

	rec_uuid = "rec-1"
	store = {
		(RECORDER_REQUEST_HASH, rec_uuid): {
			"uuid": rec_uuid,
			"calls": [{"query": "SELECT 1", "duration": 5}],
		},
		# Phase K hardening: tree blobs are HMAC-signed by
		# ``hooks_callbacks._dump_capture_state_to_redis`` before
		# being stashed; ``analyze._fetch_recordings`` verifies the
		# signature via ``session.unsign_blob``. Round-trip the test
		# fixture through ``sign_blob`` so it survives the verify step.
		f"profiler:tree:{rec_uuid}": __import__(
			"optimus.session", fromlist=["sign_blob"]
		).sign_blob(pickle.dumps({"fake": "tree"})),
		f"profiler:sidecar:{rec_uuid}": [
			{"fn_name": "get_doc", "identifier_safe": ("User", "abc123")}
		],
	}
	monkeypatch.setattr(frappe, "cache", FakeCache(store), raising=False)

	results = list(analyze._fetch_recordings([rec_uuid]))
	assert len(results) == 1
	rec = results[0]
	assert rec["uuid"] == "rec-1"
	assert rec["pyi_session"] == {"fake": "tree"}
	assert rec["sidecar"] == [
		{"fn_name": "get_doc", "identifier_safe": ("User", "abc123")}
	]


def test_fetch_recordings_loads_drifted_signed_tree(monkeypatch):
	"""When the HMAC secret drifts across processes (workers fell back to
	per-process random keys), the stored blob is ``32-byte sig + pickle`` but
	unsign fails. The read-side fallback strips the first 32 bytes and loads
	the rest as pickle.
	"""
	import frappe
	from frappe.recorder import RECORDER_REQUEST_HASH

	rec_uuid = "rec-drifted"
	# 32 random bytes (pretending to be an HMAC from another secret) +
	# a valid pickle. The current process's HMAC will not verify these
	# bytes, so unsign_blob returns None and we fall through to the
	# stripped-sig attempt.
	import os
	fake_sig = os.urandom(32)
	store = {
		(RECORDER_REQUEST_HASH, rec_uuid): {"uuid": rec_uuid, "calls": []},
		f"profiler:tree:{rec_uuid}": fake_sig + pickle.dumps({"drifted": "tree"}),
	}
	monkeypatch.setattr(frappe, "cache", FakeCache(store), raising=False)

	results = list(analyze._fetch_recordings([rec_uuid]))
	assert len(results) == 1
	assert results[0]["pyi_session"] == {"drifted": "tree"}


def test_fetch_recordings_loads_legacy_unsigned_tree(monkeypatch):
	"""Blobs written before the HMAC rollout lack the 32-byte signature prefix.
	The analyze fetch should still load them (with a warning log) when
	``optimus_allow_unsigned_pickles`` is explicitly enabled for legacy Redis compatibility.
	"""
	import frappe
	from frappe.recorder import RECORDER_REQUEST_HASH

	rec_uuid = "rec-legacy"
	store = {
		(RECORDER_REQUEST_HASH, rec_uuid): {"uuid": rec_uuid, "calls": []},
		# Raw pickle - NO HMAC prefix - simulates a pre-Sprint-1 blob.
		f"profiler:tree:{rec_uuid}": pickle.dumps({"legacy": "tree"}),
	}
	monkeypatch.setattr(frappe, "cache", FakeCache(store), raising=False)

	results = list(analyze._fetch_recordings([rec_uuid]))
	assert len(results) == 1
	# Fallback succeeded - the tree loaded despite the missing signature.
	assert results[0]["pyi_session"] == {"legacy": "tree"}


def test_fetch_recordings_handles_missing_tree(monkeypatch):
	import frappe
	from frappe.recorder import RECORDER_REQUEST_HASH

	rec_uuid = "rec-2"
	store = {
		(RECORDER_REQUEST_HASH, rec_uuid): {"uuid": rec_uuid, "calls": []},
		# No tree key, no sidecar key
	}
	monkeypatch.setattr(frappe, "cache", FakeCache(store), raising=False)

	results = list(analyze._fetch_recordings([rec_uuid]))
	assert len(results) == 1
	assert results[0]["pyi_session"] is None
	assert results[0]["sidecar"] == []


def test_fetch_recordings_handles_pickle_failure(monkeypatch):
	import frappe
	from frappe.recorder import RECORDER_REQUEST_HASH

	rec_uuid = "rec-3"
	store = {
		(RECORDER_REQUEST_HASH, rec_uuid): {"uuid": rec_uuid, "calls": []},
		f"profiler:tree:{rec_uuid}": b"not-a-valid-pickle",
		f"profiler:sidecar:{rec_uuid}": [],
	}
	monkeypatch.setattr(frappe, "cache", FakeCache(store), raising=False)

	# log_error is monkey-patched to a no-op so the test doesn't need a site
	monkeypatch.setattr(frappe, "log_error", lambda **kw: None, raising=False)

	# Should not raise; returns recording with pyi_session=None
	results = list(analyze._fetch_recordings([rec_uuid]))
	assert len(results) == 1
	assert results[0]["pyi_session"] is None


def test_fetch_recordings_is_a_generator():
	"""The function must be a generator (lazy), not a list-returning function."""
	import inspect

	assert inspect.isgeneratorfunction(analyze._fetch_recordings)


def test_cleanup_redis_deletes_tree_and_sidecar_keys(monkeypatch):
	import frappe
	from frappe.recorder import RECORDER_REQUEST_HASH, RECORDER_REQUEST_SPARSE_HASH

	uuids = ["rec-a", "rec-b"]
	store = {
		(RECORDER_REQUEST_HASH, "rec-a"): {"uuid": "rec-a"},
		(RECORDER_REQUEST_HASH, "rec-b"): {"uuid": "rec-b"},
		(RECORDER_REQUEST_SPARSE_HASH, "rec-a"): {},
		(RECORDER_REQUEST_SPARSE_HASH, "rec-b"): {},
		"profiler:tree:rec-a": b"blob-a",
		"profiler:tree:rec-b": b"blob-b",
		"profiler:sidecar:rec-a": [],
		"profiler:sidecar:rec-b": [],
	}
	cache = FakeCache(store)
	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	# session.delete_session_state inside _cleanup_redis touches more keys;
	# stub it so the test stays focused on the per-recording cleanup.
	from optimus import session as ps_session

	monkeypatch.setattr(
		ps_session, "delete_session_state", lambda uuid: None, raising=True
	)
	# log_error is monkey-patched to a no-op so the test doesn't need a site
	monkeypatch.setattr(frappe, "log_error", lambda **kw: None, raising=False)

	analyze._cleanup_redis("test-session", uuids)

	# All four per-recording keys are gone
	for uuid in uuids:
		assert (RECORDER_REQUEST_HASH, uuid) not in cache.store
		assert (RECORDER_REQUEST_SPARSE_HASH, uuid) not in cache.store
		assert f"profiler:tree:{uuid}" not in cache.store
		assert f"profiler:sidecar:{uuid}" not in cache.store


# ---------------------------------------------------------------------------
# v0.13: persisted recordings bundle re-run AI / regenerate after cleanup
# ---------------------------------------------------------------------------


def test_light_reader_keeps_old_json_without_loading_persisted_trees(monkeypatch):
	"""Old snapshots retain JSON evidence without opaque trees or sidecars."""
	import frappe

	rec_uuid = "rec-b1"
	tree_blob = _signed_tree({"fake": "tree"})
	bundle = {
		"recordings": {
			rec_uuid: {
				"rec": {"uuid": rec_uuid, "calls": [{"query": "SELECT 1"}]},
				"tree_b64": base64.b64encode(bytes(tree_blob)).decode("ascii"),
				"sidecar": [{"fn_name": "get_doc"}],
			}
		}
	}
	# Redis is empty → hget / get_value return None.
	monkeypatch.setattr(frappe, "cache", FakeCache({}), raising=False)

	monkeypatch.setattr(analyze, "_load_recordings_bundle", lambda doc: bundle)
	results = analyze.load_recordings_light(SimpleNamespace(name="fake-doc", session_uuid="fake-session", recordings_file="fake-file"), [rec_uuid])
	assert len(results) == 1
	rec = results[0]
	assert rec["uuid"] == rec_uuid
	assert rec["calls"] == [{"query": "SELECT 1"}]
	assert "pyi_session" not in rec and "sidecar" not in rec


def test_fetch_recordings_prefers_redis_over_bundle(monkeypatch):
	"""When Redis still has the recording the bundle is ignored the initial
	analyze must be byte-identical regardless of any bundle."""
	import frappe
	from frappe.recorder import RECORDER_REQUEST_HASH

	rec_uuid = "rec-b2"
	store = {
		(RECORDER_REQUEST_HASH, rec_uuid): {"uuid": rec_uuid, "calls": [{"q": "live"}]},
		f"profiler:tree:{rec_uuid}": _signed_tree({"live": "tree"}),
		f"profiler:sidecar:{rec_uuid}": [{"src": "redis"}],
	}
	monkeypatch.setattr(frappe, "cache", FakeCache(store), raising=False)
	# A DIFFERENT bundle that must NOT be used while Redis is populated.
	bundle = {
		"recordings": {
			rec_uuid: {
				"rec": {"uuid": rec_uuid, "calls": [{"q": "stale"}]},
				"tree_b64": base64.b64encode(bytes(_signed_tree({"stale": "tree"}))).decode("ascii"),
				"sidecar": [{"src": "bundle"}],
			}
		}
	}

	monkeypatch.setattr(analyze, "_load_recordings_bundle", lambda doc: bundle)
	results = analyze.load_recordings_light(SimpleNamespace(name="fake-doc", session_uuid="fake-session", recordings_file="fake-file"), [rec_uuid])
	assert results[0]["calls"] == [{"q": "live"}]
	assert "pyi_session" not in results[0] and "sidecar" not in results[0]


def test_fetch_recordings_no_bundle_skips_missing(monkeypatch):
	"""No bundle + Redis miss → the uuid is skipped (unchanged legacy behavior)."""
	import frappe

	monkeypatch.setattr(frappe, "cache", FakeCache({}), raising=False)
	assert list(analyze._fetch_recordings(["gone"])) == []


def test_persist_recordings_file_builds_parseable_bundle(monkeypatch):
	"""The snapshot retains JSON evidence and excludes opaque tree/argument data."""
	import frappe
	from frappe.recorder import RECORDER_REQUEST_HASH, RECORDER_REQUEST_SPARSE_HASH

	rec_uuid = "rec-p1"
	tree_blob = _signed_tree({"persisted": "tree"})
	store = {
		(RECORDER_REQUEST_HASH, rec_uuid): {"uuid": rec_uuid, "calls": [{"query": "SELECT 2"}]},
		(RECORDER_REQUEST_SPARSE_HASH, rec_uuid): {"uuid": rec_uuid},
		f"profiler:tree:{rec_uuid}": tree_blob,
		f"profiler:sidecar:{rec_uuid}": [{"fn_name": "get_list"}],
		f"profiler:infra:{rec_uuid}": {"cpu": 1.5},
		f"profiler:lock_events:{rec_uuid}": [{"kind": "deadlock"}],
	}
	monkeypatch.setattr(frappe, "cache", FakeCache(store), raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda **kw: None, raising=False)

	from optimus import session as ps_session

	monkeypatch.setattr(ps_session, "get_session_meta", lambda u: {"user": "x@y"}, raising=True)

	# Capture the gzipped content and model its committed File binding.
	captured = {}
	monkeypatch.setattr(frappe, "db", SimpleNamespace(get_value=lambda *a: None, set_value=lambda *a: None))
	monkeypatch.setattr(analyze, "safe_commit", lambda: None)

	def _fake_save(*, docname, filename, attached_to_field, content):
		captured.update(
			docname=docname, filename=filename, field=attached_to_field, content=content
		)
		return "/private/files/capture.json.gz"

	monkeypatch.setattr(analyze, "_save_report_file", _fake_save, raising=True)

	analyze._persist_recordings_file("DOCNAME", "sess-1", [rec_uuid])

	assert captured["field"] == "recordings_file"
	assert captured["filename"] == "optimus_recordings_sess-1.json.gz"
	bundle = json.loads(gzip.decompress(captured["content"]).decode("utf-8"))
	assert bundle["session_uuid"] == "sess-1"
	assert bundle["session_state"] == {"user": "x@y"}
	entry = bundle["recordings"][rec_uuid]
	assert entry["rec"]["calls"] == [{"query": "SELECT 2"}]
	assert entry["sparse"] == {"uuid": rec_uuid}
	assert "sidecar" not in entry
	assert entry["infra"] == {"cpu": 1.5}
	assert "tree_b64" not in entry
	assert entry["rec"]["lock_events"] == [{"kind": "deadlock"}]

	# Round-trip: the persisted bundle rebuilds the recording with Redis empty.
	monkeypatch.setattr(frappe, "cache", FakeCache({}), raising=False)
	monkeypatch.setattr(analyze, "_load_recordings_bundle", lambda doc: bundle)
	results = analyze.load_recordings_light(SimpleNamespace(name="fake-doc", session_uuid="fake-session", recordings_file="fake-file"), [rec_uuid])
	assert results[0]["lock_events"] == [{"kind": "deadlock"}]
	assert "pyi_session" not in results[0] and "sidecar" not in results[0]


def test_load_recordings_bundle_none_without_file():
	"""No recordings_file on the session → None (callers behave as before)."""

	class Doc:
		recordings_file = None

	class Doc2:
		pass

	assert analyze._load_recordings_bundle(Doc()) is None
	assert analyze._load_recordings_bundle(Doc2()) is None


OWN_STACK = [
	{"filename": "myapp/myapp/api.py", "lineno": 3, "function": "attach"},
	{"filename": "optimus/optimus/managed.py", "lineno": 234, "function": "protect_file"},
	{"filename": "frappe/frappe/database/database.py", "lineno": 200, "function": "sql"},
]
USER_STACK = [OWN_STACK[0], OWN_STACK[2]]


def _recording(uuid, own, user):
	calls = [{"query": "select own", "stack": OWN_STACK} for _ in range(own)]
	calls += [{"query": "select user", "stack": USER_STACK} for _ in range(user)]
	return {"uuid": uuid, "calls": calls}


class _Levels:
	"""Stands in for ``frappe.logger("optimus")``: records (level, text)."""

	def __init__(self):
		self.lines = []

	def __getattr__(self, level):
		return lambda text: self.lines.append((level, text))


def _strip(monkeypatch, recordings):
	import frappe

	log = _Levels()
	monkeypatch.setattr(frappe, "logger", lambda *a, **k: log, raising=False)
	analyze._strip_own_calls(recordings, "sess-9")
	return log.lines


def test_analysis_strips_own_queries_and_logs_the_count_and_source_at_debug(monkeypatch):
	recordings = [_recording("rec-1", 1, 1), _recording("rec-2", 1, 1)]
	lines = _strip(monkeypatch, recordings)
	assert [[c["query"] for c in r["calls"]] for r in recordings] == [["select user"], ["select user"]]
	assert len(lines) == 1 and lines[0][0] == "debug"
	assert "dropped 2 of 4" in lines[0][1] and "session sess-9" in lines[0][1]
	assert "optimus/optimus/managed.py:protect_file x2" in lines[0][1]


def test_a_high_volume_own_session_is_not_an_error(monkeypatch):
	"""A normal Optimus-heavy session hides many own queries: that alone must never reach a production log."""
	lines = _strip(monkeypatch, [_recording("rec-1", 60, 20)])
	assert [level for level, _text in lines] == ["debug"]
	assert "dropped 60 of 80" in lines[0][1]
	assert "optimus: dropped 60 of 80 queries as Optimus's own from session sess-9" in lines[0][1]


@pytest.mark.parametrize("own,user", [(49, 1), (50, 50), (60, 61), (500, 1)])
def test_no_drop_volume_is_logged_above_debug(monkeypatch, own, user):
	lines = _strip(monkeypatch, [_recording("rec-1", own, user)])
	assert [level for level, _text in lines] == ["debug"]


def test_a_majority_drop_across_recordings_stays_at_debug(monkeypatch):
	lines = _strip(monkeypatch, [_recording("rec-1", 30, 5), _recording("rec-2", 30, 5)])
	assert [level for level, _text in lines] == ["debug"]
	assert "dropped 60 of 70" in lines[0][1]


def test_the_volume_alert_threshold_is_gone():
	assert not hasattr(analyze, "OWN_DROP_ALERT_MIN")


UNREGISTERED_WRAPPER = {"filename": "optimus/optimus/new_feature.py", "lineno": 30, "function": "forward_get_doc"}
WRAPPER_STACK = [
	OWN_STACK[0],
	UNREGISTERED_WRAPPER,
	{"filename": "frappe/frappe/__init__.py", "lineno": 90, "function": "get_doc"},
	OWN_STACK[2],
]
APP_LISTS = ((), frozenset({"frappe", "optimus", "myapp"}))


def _suspicious_recording(uuid, suspicious, own=0):
	calls = [{"query": "select user", "stack": WRAPPER_STACK} for _ in range(suspicious)]
	calls += [{"query": "select own", "stack": OWN_STACK} for _ in range(own)]
	return {"uuid": uuid, "calls": calls}


def _strip_with_lists(monkeypatch, recordings, lists=APP_LISTS):
	import frappe

	from optimus import settings

	log = _Levels()
	monkeypatch.setattr(frappe, "logger", lambda *a, **k: log, raising=False)
	monkeypatch.setattr(settings, "own_query_app_lists", lambda: lists)
	analyze._strip_own_calls(recordings, "sess-9")
	return log.lines


def test_a_suspicious_call_is_kept_and_logged_once_at_error(monkeypatch):
	recordings = [_suspicious_recording("rec-1", 2, own=3), _suspicious_recording("rec-2", 1)]
	lines = _strip_with_lists(monkeypatch, recordings)
	assert [len(r["calls"]) for r in recordings] == [2, 1]
	errors = [text for level, text in lines if level == "error"]
	assert len(errors) == 1
	assert "3 suspicious" in errors[0] and "session sess-9" in errors[0]
	assert "_OPTIMUS_PASS_THROUGH" in errors[0] and "optimus/optimus/new_feature.py:forward_get_doc" in errors[0]
	assert [level for level, _text in lines].count("debug") == 1
	assert "dropped 3 of 6" in next(text for level, text in lines if level == "debug")


def test_the_error_names_no_query_text(monkeypatch):
	lines = _strip_with_lists(monkeypatch, [_suspicious_recording("rec-1", 1)])
	assert all("select user" not in text for _level, text in lines)


def test_a_high_volume_session_without_a_suspicious_call_does_not_error_with_app_lists(monkeypatch):
	lines = _strip_with_lists(monkeypatch, [_recording("rec-1", 80, 5)])
	assert [level for level, _text in lines] == ["debug"]


def test_a_suspicious_call_alone_logs_the_error_without_a_debug_line(monkeypatch):
	lines = _strip_with_lists(monkeypatch, [_suspicious_recording("rec-1", 2)])
	assert [level for level, _text in lines] == ["error"]


def test_without_app_lists_nothing_is_suspicious(monkeypatch):
	recordings = [_suspicious_recording("rec-1", 2)]
	lines = _strip_with_lists(monkeypatch, recordings, lists=((), None))
	assert recordings[0]["calls"] == [] and [level for level, _text in lines] == ["debug"]


def test_nothing_dropped_logs_nothing(monkeypatch):
	assert _strip(monkeypatch, [_recording("rec-1", 0, 3)]) == []


def test_a_failing_logger_never_fails_the_analysis(monkeypatch):
	import frappe

	def broken(*a, **k):
		raise RuntimeError("no logger")

	monkeypatch.setattr(frappe, "logger", broken, raising=False)
	recordings = [_recording("rec-1", 60, 20)]
	analyze._strip_own_calls(recordings, "sess-9")
	assert len(recordings[0]["calls"]) == 20


WRAPPED_FRAME = {"filename": "optimus/optimus/capture.py", "lineno": 216, "function": "wrapped"}
GET_DOC_FRAME = {"filename": "frappe/frappe/__init__.py", "lineno": 90, "function": "get_doc"}
SQL_FRAME = OWN_STACK[2]


def test_own_source_names_the_deciding_frame_not_a_pass_through_wrapper_inside_it():
	stack = [OWN_STACK[0], OWN_STACK[1], WRAPPED_FRAME, GET_DOC_FRAME, SQL_FRAME]
	assert analyze._own_source(stack) == "optimus/optimus/managed.py:protect_file"


def test_own_source_is_unknown_without_a_deciding_frame_or_a_stack():
	assert analyze._own_source([OWN_STACK[0], WRAPPED_FRAME, GET_DOC_FRAME, SQL_FRAME]) == "unknown"
	assert analyze._own_source(None) == "unknown"
	assert analyze._own_source("not a stack") == "unknown"


def test_own_source_skips_malformed_frames_and_a_non_string_function():
	stack = [None, 7, {"filename": 5}, {"filename": "optimus/optimus/managed.py", "function": 9}, OWN_STACK[2]]
	assert analyze._own_source(stack) == "optimus/optimus/managed.py:?"


def test_a_log_names_the_deciding_frame_of_each_dropped_call(monkeypatch):
	through_wrapper = [OWN_STACK[0], OWN_STACK[1], WRAPPED_FRAME, GET_DOC_FRAME, SQL_FRAME]
	recording = {"uuid": "rec-1", "calls": [{"query": "q", "stack": through_wrapper} for _ in range(3)]}
	lines = _strip(monkeypatch, [recording])
	assert "optimus/optimus/managed.py:protect_file x3" in lines[0][1]
	assert "capture.py:wrapped" not in lines[0][1]


@pytest.mark.parametrize("bad", [None, "text", 7, {}, {"filename": 3}, {"filename": ["x"], "function": "f"}])
def test_one_malformed_frame_or_call_never_fails_the_own_call_strip(monkeypatch, bad):
	recording = {"uuid": "rec-1", "calls": [
		{"query": "own", "stack": [OWN_STACK[0], bad, OWN_STACK[1], SQL_FRAME]},
		{"query": "user", "stack": [OWN_STACK[0], bad, SQL_FRAME]},
		{"query": "bare"}, {"stack": bad}, bad,
	]}
	lines = _strip(monkeypatch, [recording, bad, {"calls": bad}, {"uuid": "rec-3"}])
	assert [c["query"] for c in recording["calls"] if isinstance(c, dict) and "query" in c] == ["user", "bare"]
	assert len(lines) == 1 and "dropped 1 of 5" in lines[0][1]
	assert "optimus/optimus/managed.py:protect_file x1" in lines[0][1]


def _persisted_snapshot(monkeypatch, store, rec_uuid):
	"""Run the real persist step against ``store``; the bundle it would save, decoded."""
	import frappe

	from optimus import session as ps_session

	monkeypatch.setattr(frappe, "log_error", lambda **kw: None, raising=False)
	monkeypatch.setattr(ps_session, "get_session_meta", lambda u: {"user": "x@y"}, raising=True)
	monkeypatch.setattr(frappe, "db", SimpleNamespace(get_value=lambda *a: None, set_value=lambda *a: None))
	monkeypatch.setattr(analyze, "safe_commit", lambda: None)
	saved = {}
	monkeypatch.setattr(
		analyze, "_save_report_file",
		lambda *, content, **kw: saved.update(content=content) or "/private/files/snapshot.json.gz",
		raising=True,
	)
	analyze._persist_recordings_file("DOCNAME", "sess-1", [rec_uuid])
	return json.loads(gzip.decompress(saved["content"]).decode("utf-8"))


def test_the_persisted_snapshot_carries_the_own_excluded_count_a_refreshed_footer_reads(monkeypatch):
	"""The analysis strips the recording it fetched from the cache, and the snapshot persists that same
	recording: its calls are already stripped, so the stamp is the only record of how many were excluded.
	A refresh reads the snapshot, so without the stamp its footer note would say nothing."""
	import frappe
	from frappe.recorder import RECORDER_REQUEST_HASH

	from optimus import renderer
	from optimus.analyzers.base import OWN_EXCLUDED_KEY
	from optimus.tests.test_own_queries_excluded_note import _session_doc

	rec_uuid = "rec-keep"
	own_calls = [{"query": "select own", "stack": OWN_STACK} for _ in range(3)]
	store = {
		(RECORDER_REQUEST_HASH, rec_uuid): {
			"uuid": rec_uuid, "calls": [*own_calls, {"query": "select user", "stack": USER_STACK}],
		},
		f"profiler:tree:{rec_uuid}": _signed_tree({"fake": "tree"}),
	}
	monkeypatch.setattr(frappe, "cache", FakeCache(store), raising=False)

	# analysis: fetch, then strip the fetched recordings in place
	recordings = list(analyze._fetch_recordings([rec_uuid]))
	_strip(monkeypatch, recordings)
	assert recordings[0][OWN_EXCLUDED_KEY] == 3

	# persist: the snapshot's recording is already stripped and carries the stamp
	bundle = _persisted_snapshot(monkeypatch, store, rec_uuid)
	persisted = bundle["recordings"][rec_uuid]["rec"]
	assert [c["query"] for c in persisted["calls"]] == ["select user"]
	assert persisted[OWN_EXCLUDED_KEY] == 3

	# refresh: Redis is gone, the light loader reads the snapshot and the footer still counts
	monkeypatch.setattr(frappe, "cache", FakeCache({}), raising=False)
	monkeypatch.setattr(analyze, "_load_recordings_bundle", lambda doc: bundle)
	light = analyze.load_recordings_light(
		SimpleNamespace(name="fake-doc", session_uuid="sess-1", recordings_file="fake-file"), [rec_uuid]
	)
	assert light[0][OWN_EXCLUDED_KEY] == 3
	html = renderer.render(_session_doc(), recordings=light)
	assert "3 queries from Optimus&#39;s own code were excluded from this report" in html


def test_a_snapshot_with_nothing_excluded_carries_no_stamp(monkeypatch):
	import frappe
	from frappe.recorder import RECORDER_REQUEST_HASH

	from optimus.analyzers.base import OWN_EXCLUDED_KEY

	rec_uuid = "rec-clean"
	store = {
		(RECORDER_REQUEST_HASH, rec_uuid): {"uuid": rec_uuid, "calls": [{"query": "q", "stack": USER_STACK}]},
		f"profiler:tree:{rec_uuid}": _signed_tree({"fake": "tree"}),
	}
	monkeypatch.setattr(frappe, "cache", FakeCache(store), raising=False)
	_strip(monkeypatch, list(analyze._fetch_recordings([rec_uuid])))
	bundle = _persisted_snapshot(monkeypatch, store, rec_uuid)
	assert OWN_EXCLUDED_KEY not in bundle["recordings"][rec_uuid]["rec"]
