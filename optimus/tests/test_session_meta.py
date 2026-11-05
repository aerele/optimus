# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for capture_python_tree handling in session.set_session_meta.

These tests use a fake cache backend so we don't depend on Redis.
"""

import pytest

from optimus import session


class FakeCache:
	"""Dict-backed cache whose pipeline honours WATCH: a write to a watched key aborts EXEC."""

	def __init__(self):
		self.store = {}
		self.versions = {}
		# Runs after a pipeline read, standing in for a request landing before EXEC.
		self.after_get = None

	def touch(self, key):
		self.versions[key] = self.versions.get(key, 0) + 1

	def make_key(self, key):
		return key

	def pipeline(self):
		import pickle

		from redis.exceptions import WatchError

		cache = self
		class Pipeline:
			def __init__(self): self.watched, self.queued = {}, None
			def __enter__(self): return self
			def __exit__(self, *args): pass
			def watch(self, *keys): self.watched = {key: cache.versions.get(key, 0) for key in keys}
			def exists(self, *keys): return sum(key in cache.store for key in keys)
			def multi(self): self.queued = []
			def set(self, key, value, ex=None): self.queued.append(("set", key, pickle.loads(value)))
			def get(self, key):
				raw = pickle.dumps(cache.store[key], protocol=4) if key in cache.store else None
				if cache.after_get:
					cache.after_get(key)
				return raw
			def delete(self, key): self.queued.append(("delete", key, None))
			def execute(self):
				if any(cache.versions.get(key, 0) != seen for key, seen in self.watched.items()):
					raise WatchError("watched key changed")
				for op, key, value in self.queued:
					if op == "set":
						cache.store[key] = value
					else:
						cache.store.pop(key, None)
					cache.touch(key)
		return Pipeline()

	def set_value(self, key, value, expires_in_sec=None):
		self.store[key] = value
		self.touch(key)

	def get_value(self, key):
		return self.store.get(key)

	def delete_value(self, key):
		self.store.pop(key, None)
		self.touch(key)

	def expire_key(self, key, time):
		# Mirror Redis EXPIRE semantics: returns 1 if the key exists
		# and TTL was refreshed; 0 (no-op) if the key is missing.
		# Our FakeCache doesn't track TTL, but the existence check is
		# what callers rely on.
		if key not in self.store:
			return 0
		self.touch(key)  # Redis counts EXPIRE as a write to a watched key.
		return 1

	def sadd(self, key, value):
		s = self.store.setdefault(key, set())
		s.add(value)

	def smembers(self, key):
		return self.store.get(key, set())


@pytest.fixture
def fake_cache(monkeypatch):
	import frappe

	cache = FakeCache()
	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	monkeypatch.setattr(frappe.local, "cache", {}, raising=False)

	# register_recording reads frappe.conf for the per-session cap;
	# in unit-test mode frappe.local is unbound, so stub conf with a
	# plain dict that satisfies .get(...). Idempotent: existing
	# session_meta tests that don't exercise this code path see no
	# behaviour change.
	monkeypatch.setattr(frappe, "conf", {}, raising=False)
	return cache


def test_set_session_meta_persists_capture_python_tree_true(fake_cache):
	session.set_session_meta(
		"test-uuid",
		{
			"session_uuid": "test-uuid",
			"docname": "PS-001",
			"user": "admin@example.com",
			"label": "test",
			"started_at": "2026-04-13T00:00:00",
			"capture_python_tree": True,
		},
	)
	meta = session.get_session_meta("test-uuid")
	assert meta["capture_python_tree"] is True


def test_set_session_meta_persists_capture_python_tree_false(fake_cache):
	session.set_session_meta(
		"test-uuid",
		{"session_uuid": "test-uuid", "capture_python_tree": False},
	)
	meta = session.get_session_meta("test-uuid")
	assert meta["capture_python_tree"] is False


def test_set_session_meta_default_when_not_specified(fake_cache):
	session.set_session_meta("test-uuid", {"session_uuid": "test-uuid"})
	meta = session.get_session_meta("test-uuid")
	# capture_python_tree absent means consumers should default to True
	assert "capture_python_tree" not in meta


def test_register_recording_does_not_resurrect_cleared_active_pointer(fake_cache):
	"""A Stop clears the active-session pointer; a subsequent
	``register_recording`` from an in-flight request must NOT re-create it
	(the TTL refresh uses ``expire_key``, a no-op on missing keys), or later
	requests get captured into the stopped session.
	"""
	user = "alice@example.com"
	session_uuid = "uuid-1"
	# Simulate an active session: pointer is set, session meta exists.
	session.set_active_session(user, session_uuid)
	session.set_session_meta(session_uuid, {"user": user})
	# User clicks Stop: active pointer cleared.
	session.clear_active_session(user)
	assert session.get_active_session_for(user) is None

	# An in-flight request's after_request now registers its recording.
	# Pre-fix: this call recreated the active pointer via set_value.
	# Post-fix: expire_key is a no-op on the missing key.
	session.register_recording(session_uuid, "rec-1", user=user)

	# Active pointer must STAY cleared. Subsequent before_request hooks
	# would otherwise see the user as still recording.
	assert session.get_active_session_for(user) is None


def test_register_recording_refreshes_ttl_when_session_still_active(fake_cache):
	"""While a session is active (pointer present), each ``register_recording``
	refreshes the TTL so a long flow doesn't expire at the 10-minute boundary."""
	user = "alice@example.com"
	session_uuid = "uuid-1"
	session.set_active_session(user, session_uuid)
	session.set_session_meta(session_uuid, {"user": user})

	# In-flight request: register_recording fires while session still active.
	session.register_recording(session_uuid, "rec-1", user=user)

	# Pointer remains set with the same session.
	assert session.get_active_session_for(user) == session_uuid


def test_clear_active_session_if_clears_only_the_given_session(fake_cache):
	session.set_active_session("fake-user", "fake-session")
	assert session.clear_active_session_if("fake-user", "other-session") is False
	assert session.get_active_session_for("fake-user") == "fake-session"
	assert session.clear_active_session_if("fake-user", "fake-session") is True
	assert session.get_active_session_for("fake-user") is None
	assert session.clear_active_session_if("fake-user", "fake-session") is False


def _renew_pointer(fake_cache, times):
	"""A recorded request renews the pointer between WATCH and EXEC ``times`` times."""
	left = [times]

	def renew(key):
		if left[0]:
			left[0] -= 1
			fake_cache.expire_key(key, 600)

	fake_cache.after_get = renew
	return left


def test_clear_active_session_if_retries_after_a_renewal(fake_cache):
	session.set_active_session("fake-user", "fake-session")
	left = _renew_pointer(fake_cache, 2)
	assert session.clear_active_session_if("fake-user", "fake-session") is True
	assert left == [0]
	assert session.get_active_session_for("fake-user") is None


def test_clear_active_session_if_reports_a_race_it_never_won(fake_cache):
	session.set_active_session("fake-user", "fake-session")
	_renew_pointer(fake_cache, session.CLEAR_ACTIVE_ATTEMPTS)
	assert session.clear_active_session_if("fake-user", "fake-session") is None
	assert session.get_active_session_for("fake-user") == "fake-session"


def test_clear_active_session_if_keeps_a_newer_pointer(fake_cache):
	session.set_active_session("fake-user", "fake-session")

	def replace(key):
		fake_cache.store[key] = "newer-session"
		fake_cache.touch(key)

	fake_cache.after_get = replace
	assert session.clear_active_session_if("fake-user", "fake-session") is False
	assert session.get_active_session_for("fake-user") == "newer-session"
