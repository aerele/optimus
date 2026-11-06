# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""_dump_capture_state_to_redis stores a size-capped tree and never breaks the request."""

import types

import pytest

from optimus import analyze, capture, redis_keys
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


def test_a_tree_over_the_cap_writes_nothing_and_warns_once(env, monkeypatch):
	monkeypatch.setattr(capture, "TREE_MAX_BYTES", 2000)
	env.local.optimus_pyinstrument = Prof(fake_session([(["x" * 5000], 1.0)]))
	env.local.optimus_sidecar = [{"a": 1}]
	hc._dump_capture_state_to_redis(recording_uuid="rec-1")
	assert redis_keys.tree("rec-1") not in env.cache.values
	assert redis_keys.sidecar("rec-1") in env.cache.values
	assert len(env.warnings) == 1 and "rec-1" in env.warnings[0]


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
