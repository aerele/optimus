# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""_cleanup_redis removes every recording's entries before the set that indexes them."""

import types

import pytest
from frappe.recorder import RECORDER_REQUEST_HASH, RECORDER_REQUEST_SPARSE_HASH

from optimus import analyze, redis_keys, safe_call
from optimus import session as ps_session

frappe = analyze.frappe


class _Cache:
	def __init__(self, fail_hdel_for=()):
		self.ops = []
		self.fail_hdel_for = set(fail_hdel_for)

	def hdel(self, name, key):
		if key in self.fail_hdel_for:
			raise ConnectionError("redis down")
		self.ops.append(("hdel", name, key))

	def delete_value(self, key):
		self.ops.append(("delete", key))


@pytest.fixture
def env(monkeypatch):
	cache = _Cache()
	registered = []
	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda **kw: None, raising=False)
	monkeypatch.setattr(ps_session, "get_recordings", lambda session_uuid: list(registered))
	monkeypatch.setattr(ps_session, "delete_session_state", lambda uuid: cache.ops.append(("state", uuid)))
	return types.SimpleNamespace(cache=cache, registered=registered)


def _per_recording_keys(uuid):
	return {
		("hdel", RECORDER_REQUEST_HASH, uuid),
		("hdel", RECORDER_REQUEST_SPARSE_HASH, uuid),
		("delete", redis_keys.tree(uuid)),
		("delete", redis_keys.sidecar(uuid)),
		("delete", redis_keys.lock_events(uuid)),
		("delete", redis_keys.resolved_doc(uuid)),
		("delete", redis_keys.infra(uuid)),
	}


def test_every_per_recording_key_goes_including_infra(env):
	env.registered[:] = ["a"]

	analyze._cleanup_redis("s1", ["a"])

	assert set(env.cache.ops) - {("state", "s1")} == _per_recording_keys("a")


def test_the_session_state_is_deleted_last(env):
	env.registered[:] = ["a", "b"]

	analyze._cleanup_redis("s1", ["a", "b"])

	assert env.cache.ops[-1] == ("state", "s1")
	assert ("state", "s1") not in env.cache.ops[:-1]


def test_a_recording_registered_after_the_analyze_snapshot_is_cleaned_too(env):
	env.registered[:] = ["a", "late"]  # "late" joined the set after analyze read its list

	analyze._cleanup_redis("s1", ["a"])

	assert _per_recording_keys("late") <= set(env.cache.ops)


def test_a_failed_delete_keeps_the_index_so_a_later_sweep_can_finish(env):
	env.cache.fail_hdel_for = {"a"}
	env.registered[:] = ["a", "b"]

	analyze._cleanup_redis("s1", ["a", "b"])

	assert ("state", "s1") not in env.cache.ops
	assert _per_recording_keys("b") <= set(env.cache.ops)  # the other recording still went


def test_cleanup_still_runs_when_the_current_set_cannot_be_read(env, monkeypatch):
	def unreadable(session_uuid):
		raise ConnectionError("redis down")

	monkeypatch.setattr(ps_session, "get_recordings", unreadable)

	analyze._cleanup_redis("s1", ["a"])

	assert _per_recording_keys("a") <= set(env.cache.ops)
	assert env.cache.ops[-1] == ("state", "s1")


def test_a_uuid_only_the_snapshot_has_is_cleaned_along_with_one_only_the_set_has(env):
	env.registered[:] = ["fresh"]  # the set no longer lists "snap"

	analyze._cleanup_redis("s1", ["snap"])

	assert _per_recording_keys("snap") <= set(env.cache.ops)
	assert _per_recording_keys("fresh") <= set(env.cache.ops)


def test_a_kept_index_is_logged_through_the_level_aware_helper(env, monkeypatch):
	lines = []
	monkeypatch.setattr(safe_call, "log_error_line", lambda message, **kw: lines.append(message))
	env.cache.fail_hdel_for = {"a"}
	env.registered[:] = ["a"]

	analyze._cleanup_redis("s1", ["a"])

	assert len(lines) == 1 and "s1" in lines[0] and "kept" in lines[0]


def test_a_clean_run_logs_nothing(env, monkeypatch):
	lines = []
	monkeypatch.setattr(safe_call, "log_error_line", lambda message, **kw: lines.append(message))
	env.registered[:] = ["a"]

	analyze._cleanup_redis("s1", ["a"])

	assert lines == []


def test_a_failing_session_state_delete_is_logged_not_raised(env, monkeypatch):
	titles = []
	monkeypatch.setattr(frappe, "log_error", lambda **kw: titles.append(kw.get("title")))

	def boom(uuid):
		raise ConnectionError("redis down")

	monkeypatch.setattr(ps_session, "delete_session_state", boom)

	analyze._cleanup_redis("s1", [])

	assert titles == ["optimus cleanup session_state"]
