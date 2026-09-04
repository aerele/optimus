# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for capture_python_tree handling in session.set_session_meta.

These tests use a fake cache backend so we don't depend on Redis.
"""

import pytest

from optimus import session


class FakeCache:
	def __init__(self):
		self.store = {}

	def set_value(self, key, value, expires_in_sec=None):
		self.store[key] = value

	def get_value(self, key):
		return self.store.get(key)

	def delete_value(self, key):
		self.store.pop(key, None)

	def expire_key(self, key, time):
		# Mirror Redis EXPIRE semantics: returns 1 if the key exists
		# and TTL was refreshed; 0 (no-op) if the key is missing.
		# Our FakeCache doesn't track TTL, but the existence check is
		# what callers rely on.
		return 1 if key in self.store else 0

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
	"""v0.7.x regression guard: a Stop call clears the active-session
	Redis pointer. If an in-flight request's ``after_request`` then calls
	``register_recording``, the TTL refresh must NOT re-create the
	pointer otherwise subsequent HTTP requests get captured into the
	already-stopped session and the widget flips back to Recording on
	the user's next interaction. The fix replaced ``set_value`` (which
	creates) with ``expire_key`` (Redis EXPIRE no-op on missing keys).
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
	"""Positive case: while a session is still active (pointer present),
	each register_recording call refreshes the TTL so a long flow
	doesn't silently expire at the 10-minute boundary. The
	expire_key call returns 1 (refreshed) when the key exists."""
	user = "alice@example.com"
	session_uuid = "uuid-1"
	session.set_active_session(user, session_uuid)
	session.set_session_meta(session_uuid, {"user": user})

	# In-flight request: register_recording fires while session still active.
	session.register_recording(session_uuid, "rec-1", user=user)

	# Pointer remains set with the same session.
	assert session.get_active_session_for(user) == session_uuid
