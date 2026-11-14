# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Per-recording and frontend keys live RECORDING_DATA_TTL_SECONDS, not the 10-minute pointer TTL."""

import json
import types

import pytest

from optimus import api, infra_capture, redis_keys, session
from optimus import hooks_callbacks as hc
from optimus.timing import RECORDING_DATA_TTL_SECONDS, SESSION_TTL_SECONDS

frappe = hc.frappe


class _Cache:
	def __init__(self):
		self.ttls = {}

	def set_value(self, key, value, expires_in_sec=None, **kw):
		self.ttls[key] = expires_in_sec

	def hdel(self, *a):
		pass


class _Prof:
	last_session = {"frames": 1}

	def stop(self):
		pass


@pytest.fixture
def env(monkeypatch):
	cache = _Cache()
	local = types.SimpleNamespace(form_dict={}, response=None)
	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	monkeypatch.setattr(frappe, "local", local, raising=False)
	monkeypatch.setattr(frappe, "session", types.SimpleNamespace(user="a@b.c"), raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda *a, **kw: None, raising=False)
	monkeypatch.setattr(
		frappe, "logger", lambda *a, **kw: types.SimpleNamespace(warning=print, debug=print), raising=False
	)
	monkeypatch.setattr(
		hc,
		"session",
		types.SimpleNamespace(
			register_recording=lambda *a, **kw: True, SESSION_TTL_SECONDS=SESSION_TTL_SECONDS
		),
	)
	monkeypatch.setattr(hc, "_inject_correlation_header", lambda *a, **kw: None)
	monkeypatch.setattr(infra_capture, "snapshot", lambda: {"cpu": 1})
	monkeypatch.setattr(infra_capture, "diff", lambda a, b: {"diff": True})
	return types.SimpleNamespace(cache=cache, local=local)


def test_the_tree_sidecar_and_lock_events_get_the_recording_data_ttl(env):
	env.local.optimus_pyinstrument = _Prof()
	env.local.optimus_sidecar = [{"a": 1}]
	env.local.optimus_lock_events = [{"kind": "deadlock"}]

	hc._dump_capture_state_to_redis(recording_uuid="rec-1")

	assert env.cache.ttls == {
		redis_keys.tree("rec-1"): RECORDING_DATA_TTL_SECONDS,
		redis_keys.sidecar("rec-1"): RECORDING_DATA_TTL_SECONDS,
		redis_keys.lock_events("rec-1"): RECORDING_DATA_TTL_SECONDS,
	}


def test_a_request_writes_its_infra_and_resolved_doc_keys_with_the_recording_data_ttl(env, monkeypatch):
	from optimus.renderer import doc_event_renderer

	monkeypatch.setattr(doc_event_renderer, "resolve_target_doc_from_response", lambda form, resp: "SO-1")
	monkeypatch.setattr(hc, "_dump_capture_state_to_redis", lambda recording_uuid=None: None)
	env.local.optimus_session_id = "s1"
	env.local._recorder = types.SimpleNamespace(uuid="rec-1")
	env.local.optimus_infra_start = {"cpu": 0}

	hc.after_request()

	assert env.cache.ttls == {
		redis_keys.infra("rec-1"): RECORDING_DATA_TTL_SECONDS,
		redis_keys.resolved_doc("rec-1"): RECORDING_DATA_TTL_SECONDS,
	}


def test_a_job_writes_its_infra_key_with_the_recording_data_ttl(env, monkeypatch):
	monkeypatch.setattr(hc, "_dump_capture_state_to_redis", lambda recording_uuid=None: None)
	env.local.optimus_session_id = "s1"
	env.local._recorder = types.SimpleNamespace(uuid="rec-1")
	env.local.optimus_infra_start = {"cpu": 0}

	hc.after_job()

	assert env.cache.ttls == {redis_keys.infra("rec-1"): RECORDING_DATA_TTL_SECONDS}


def test_frontend_metric_lists_get_the_recording_data_ttl(monkeypatch):
	expiries = {}

	class Cache:
		def rpush(self, *a):
			return 1

		def ltrim(self, *a):
			pass

		def llen(self, key):
			return 1

	monkeypatch.setattr(frappe, "cache", Cache(), raising=False)
	monkeypatch.setattr(frappe, "session", types.SimpleNamespace(user="a@b.c"), raising=False)
	monkeypatch.setattr(frappe, "parse_json", json.loads, raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"], raising=False)
	monkeypatch.setattr(api.ratelimit, "enforce_user_rate_limit", lambda *a, **kw: None)
	monkeypatch.setattr(api, "_require_profiler_user", lambda: "a@b.c")
	monkeypatch.setattr(
		api,
		"session",
		types.SimpleNamespace(
			get_session_meta=lambda uuid: {"user": "a@b.c"},
			expire_key=lambda key, ttl: expiries.__setitem__(key, ttl),
			SESSION_TTL_SECONDS=SESSION_TTL_SECONDS,
		),
	)

	api.submit_frontend_metrics(json.dumps({"session_uuid": "s1", "xhr": [{"u": 1}], "vitals": [{"n": 1}]}))

	assert set(expiries.values()) == {RECORDING_DATA_TTL_SECONDS}
	assert len(expiries) == 2


def test_the_active_pointer_keeps_the_short_ttl():
	assert session.SESSION_TTL_SECONDS == SESSION_TTL_SECONDS < RECORDING_DATA_TTL_SECONDS
