# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""_dump_capture_state_to_redis stores a size-capped tree and never breaks the request."""

import types

import pytest

from optimus import analyze, capture, redis_keys, safe_call
from optimus import hooks_callbacks as hc
from optimus.tests.test_tree_size_cap import fake_session, unique_records

frappe = hc.frappe


class Cache:
	def __init__(self):
		self.values = {}

	def set_value(self, key, value, expires_in_sec=None, **kw):
		self.values[key] = value


class Prof:
	def __init__(self, last_session):
		self.last_session = last_session

	def stop(self):
		pass


@pytest.fixture
def env(monkeypatch):
	cache = Cache()
	warnings = []
	local = types.SimpleNamespace()
	logger = types.SimpleNamespace(warning=warnings.append, debug=lambda *a: None)
	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	monkeypatch.setattr(frappe, "local", local, raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda *a, **kw: None, raising=False)
	monkeypatch.setattr(frappe, "logger", lambda *a, **kw: logger, raising=False)
	return types.SimpleNamespace(cache=cache, local=local, warnings=warnings)


def test_the_stored_blob_is_within_the_cap_and_round_trips(env):
	session = fake_session(unique_records(30000, depth=20))
	env.local.optimus_pyinstrument = Prof(session)
	hc._dump_capture_state_to_redis(recording_uuid="rec-1")
	blob = env.cache.values[redis_keys.tree("rec-1")]
	assert len(blob) <= capture.TREE_MAX_BYTES + 64
	loaded = analyze._deserialize_tree("rec-1", blob, allow_unsigned=True)
	assert loaded.optimus_thinned["samples"] == 30000


def test_a_tree_over_the_cap_stores_only_a_marker_and_logs_one_line_per_process(env, monkeypatch):
	lines = []
	monkeypatch.setattr(safe_call, "log_error_line", lambda message, **kw: lines.append((message, kw)))
	monkeypatch.setattr(capture, "TREE_MAX_BYTES", 2000)
	env.local.optimus_pyinstrument = Prof(fake_session([(["x" * 5000], 1.0)]))
	env.local.optimus_sidecar = [{"a": 1}]
	hc._dump_capture_state_to_redis(recording_uuid="rec-1")
	env.local.optimus_pyinstrument = Prof(fake_session([(["x" * 5000], 1.0)]))
	hc._dump_capture_state_to_redis(recording_uuid="rec-2")
	marker = analyze._deserialize_tree("rec-1", env.cache.values[redis_keys.tree("rec-1")], allow_unsigned=True)
	assert marker == {capture.TREE_DROPPED_KEY: "too_large"}
	assert redis_keys.sidecar("rec-1") in env.cache.values
	assert len(lines) == 2 and lines[0] == lines[1]
	assert "rec-" not in lines[0][0] and lines[0][1]["dedupe_seconds"] >= 3600


def test_the_over_the_cap_line_is_written_once_by_the_real_dedupe(env, monkeypatch):
	written = []
	monkeypatch.setattr(
		frappe, "logger", lambda *a, **kw: types.SimpleNamespace(error=written.append, debug=lambda *x: None)
	)
	monkeypatch.setattr(capture, "TREE_MAX_BYTES", 2000)
	for uuid in ("rec-1", "rec-2", "rec-3"):
		env.local.optimus_pyinstrument = Prof(fake_session([(["x" * 5000], 1.0)]))
		hc._dump_capture_state_to_redis(recording_uuid=uuid)
	assert len(written) == 1


def test_a_failing_helper_never_breaks_the_request(env, monkeypatch):
	def boom(*a, **kw):
		raise RuntimeError("x")

	monkeypatch.setattr(capture, "pickle_pyi_session", boom)
	env.local.optimus_pyinstrument = Prof(fake_session(unique_records(5)))
	env.local.optimus_sidecar = [{"a": 1}]
	env.local.optimus_lock_events = [{"kind": "deadlock"}]
	hc._dump_capture_state_to_redis(recording_uuid="rec-1")
	assert redis_keys.tree("rec-1") not in env.cache.values
	assert redis_keys.sidecar("rec-1") in env.cache.values
	assert redis_keys.lock_events("rec-1") in env.cache.values


def test_no_last_session_is_a_noop(env):
	env.local.optimus_pyinstrument = Prof(None)
	hc._dump_capture_state_to_redis(recording_uuid="rec-1")
	assert env.cache.values == {}


def test_a_compaction_failure_falls_back_to_the_raw_pickle_when_it_fits(env, monkeypatch):
	monkeypatch.setattr(capture, "_intern_records", lambda *a, **k: 1 / 0)
	env.local.optimus_pyinstrument = Prof(fake_session(unique_records(3, depth=2)))
	hc._dump_capture_state_to_redis(recording_uuid="rec-1")
	assert redis_keys.tree("rec-1") in env.cache.values


def test_fetch_turns_a_stored_marker_into_a_flag_not_a_tree(env, monkeypatch):
	from optimus.session import sign_blob

	blob = sign_blob(capture.dropped_tree_blob())
	monkeypatch.setattr(analyze, "_allow_unsigned_pickles", lambda: True)
	store = {redis_keys.tree("rec-1"): blob}
	monkeypatch.setattr(
		frappe,
		"cache",
		types.SimpleNamespace(hget=lambda *a: {"uuid": "rec-1"}, get_value=lambda key: store.get(key)),
		raising=False,
	)
	monkeypatch.setattr(analyze, "strip_profiler_own_calls", lambda rec: None)
	(rec,) = analyze._fetch_recordings(["rec-1"])
	assert rec["pyi_session"] is None and rec["tree_dropped"] is True


def test_a_compaction_failure_is_logged_and_a_huge_raw_session_is_not_pickled_in_full(env, monkeypatch):
	lines = []
	monkeypatch.setattr(safe_call, "log_error_line", lambda message, **kw: lines.append(message))
	monkeypatch.setattr(capture, "TREE_MAX_BYTES", 5000)
	monkeypatch.setattr(capture, "_intern_records", lambda *a, **k: 1 / 0)
	session = fake_session(unique_records(3000, depth=20))
	blob, info = capture.pickle_pyi_session(session)
	assert blob is None and info is None
	assert lines and "compaction failed (ZeroDivisionError)" in lines[0]


def test_a_pickle_stops_at_twice_the_cap(monkeypatch):
	written = []
	real = capture._CappedBuffer.write
	monkeypatch.setattr(capture._CappedBuffer, "write", lambda self, data: (written.append(len(data)), real(self, data))[1])
	monkeypatch.setattr(capture, "TREE_MAX_BYTES", 5000)
	monkeypatch.setattr(capture, "_intern_records", lambda *a, **k: 1 / 0)
	capture.pickle_pyi_session(fake_session(unique_records(30000, depth=20)))
	assert written and sum(written) < 5000 * 2 + 70_000
