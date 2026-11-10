"""Per-recording analyze inputs must outlive a long session and be dropped after analysis."""

import types

import pytest

from optimus import analyze, redis_keys, session
from optimus import hooks_callbacks as hc


class _Cache:
	def __init__(self):
		self.store, self.ttls, self.deleted = {}, {}, []

	def set_value(self, key, value, expires_in_sec=None, **kw):
		self.store[key] = value
		self.ttls[key] = expires_in_sec

	def get_value(self, key, **kw):
		return self.store.get(key)

	def delete_value(self, key):
		self.deleted.append(key)
		self.store.pop(key, None)

	def hdel(self, name, key):
		self.deleted.append((name, key))


class _Profiler:
	last_session = {"frames": ["root"]}

	def stop(self):
		pass


@pytest.fixture
def cache(monkeypatch):
	import frappe

	fake = _Cache()
	monkeypatch.setattr(frappe, "cache", fake, raising=False)
	monkeypatch.setattr(frappe, "local", types.SimpleNamespace(), raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda *a, **kw: pytest.fail(f"unexpected log_error {kw}"), raising=False)
	monkeypatch.setattr(frappe, "logger", lambda *a, **kw: types.SimpleNamespace(debug=lambda *a: None), raising=False)
	monkeypatch.setattr(session, "sign_blob", lambda blob: b"sig" + blob)
	return fake


def test_recording_inputs_outlive_the_active_session_window():
	assert session.RECORDING_DATA_TTL_SECONDS > session.SESSION_TTL_SECONDS


def test_tree_sidecar_and_lock_events_use_the_recording_data_ttl(cache):
	import frappe

	frappe.local.optimus_pyinstrument = _Profiler()
	frappe.local.optimus_sidecar = [{"arg": 1}]
	frappe.local.optimus_lock_events = [{"kind": "deadlock"}]
	hc._dump_capture_state_to_redis(recording_uuid="rec-1")

	for key in (redis_keys.tree("rec-1"), redis_keys.sidecar("rec-1"), redis_keys.lock_events("rec-1")):
		assert key in cache.store
		assert cache.ttls[key] == session.RECORDING_DATA_TTL_SECONDS


def test_cleanup_drops_every_per_recording_key(cache, monkeypatch):
	monkeypatch.setattr(analyze.session, "delete_session_state", lambda uuid: None)
	analyze._cleanup_redis("s1", ["rec-1"])
	for key in (
		redis_keys.tree("rec-1"), redis_keys.sidecar("rec-1"), redis_keys.infra("rec-1"),
		redis_keys.resolved_doc("rec-1"), redis_keys.lock_events("rec-1"),
	):
		assert key in cache.deleted, key
