# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""``settings.get_config()`` stores its cached value inside the ``wrap_value``
/ ``unwrap_value`` envelope. Three contract pieces under test:

  1. Write path: a cache miss re-resolves and stores the OptimusConfig field
     dict inside the envelope (``{"_v": 1, "data": {...}}``), not bare.
  2. Legacy-compat read path: a cache holding a bare-dict value (no ``_v``)
     ALSO unwraps cleanly, so new readers don't crash on stale values left by
     old writers.
  3. Drift: a cached envelope with a version this build does not know falls
     through to a fresh resolve.

Each test injects a dict-backed fake ``frappe.cache`` (replaced wholesale, not
patched) to introspect the exact shape written / read.
"""

from __future__ import annotations

import sys
import types
from unittest import mock

import pytest


class _FakeCache:
	"""Dict-backed ``frappe.cache`` substitute. Exposes the slice of API
	the rollout under test actually touches (``get_value`` / ``set_value``
	/ ``delete_value``)."""

	def __init__(self) -> None:
		self.store: dict = {}

	def get_value(self, key, expires=False):
		return self.store.get(key)

	def set_value(self, key, value, **_):
		self.store[key] = value

	def delete_value(self, key):
		self.store.pop(key, None)


@pytest.fixture
def fresh(monkeypatch):
	"""A fresh import of ``optimus.settings`` so each test's ``_CACHE_KEY``
	resolution doesn't carry state across tests (the module computes it once
	at import-time).

	The original module objects come back at teardown, both in
	``sys.modules`` and as the ``optimus`` package's ``settings`` attribute.
	Otherwise later tests would see two modules: ``from optimus import
	settings`` (the attribute) would be the fresh one, while ``from
	optimus.settings import get_config`` in the code under test (the
	``sys.modules`` entry) would still be the original, so their patches
	would miss it."""
	import optimus
	import optimus.settings  # noqa: F401  (make sure the original is loaded)

	monkeypatch.setattr(optimus, "settings", sys.modules["optimus.settings"])
	for mod_name in list(sys.modules):
		if mod_name == "optimus.settings" or mod_name.startswith("optimus.settings."):
			monkeypatch.delitem(sys.modules, mod_name)
	import optimus.settings as fresh_module

	return fresh_module


def _stub_frappe(cache: _FakeCache, *, has_doctype: bool = False):
	"""Build a minimal ``frappe`` stub that ``settings.get_config`` is
	happy with. ``has_doctype=False`` short-circuits ``_read_doctype_row``
	to None so ``_resolve()`` uses ``_DEFAULTS`` (deterministic test
	inputs)."""
	frappe = types.SimpleNamespace()
	frappe.cache = cache
	frappe.conf = {}
	frappe.flags = types.SimpleNamespace(in_test=True)
	# get_cached_doc / db.exists / get_single_value not reached on the
	# happy path when has_doctype=False; provide harmless stubs.
	db = types.SimpleNamespace()
	db.exists = lambda *_a, **_kw: False if not has_doctype else True
	frappe.db = db
	frappe.get_doc = lambda *a, **kw: {}
	frappe.get_cached_doc = lambda *_a, **_kw: (_ for _ in ()).throw(
		RuntimeError("get_cached_doc should not be called when has_doctype=False")
	)
	return frappe


class TestSettingsEnvelopeWrite:
	"""On a cache miss, ``get_config`` re-resolves and stores the result inside
	the envelope. Catches a regression that reverts the wrap_value call to
	bare-dict writes."""

	def test_fresh_write_stores_envelope_not_bare_dict(self, fresh):
		cache = _FakeCache()
		frappe_stub = _stub_frappe(cache, has_doctype=True)

		with mock.patch.dict(sys.modules, {"frappe": frappe_stub}):
			cfg = fresh.get_config()

		assert cfg is not None
		# The cache now has the envelope, not a bare cfg dict.
		stored = cache.store.get(fresh._CACHE_KEY)
		assert isinstance(stored, dict), f"expected envelope dict in cache; got: {stored!r}"
		assert "_v" in stored, (
			f"settings cache value missing envelope version sentinel ('_v'); got: {stored!r}"
		)
		assert "data" in stored, (
			f"settings cache value missing envelope payload field ('data'); got: {stored!r}"
		)
		# Version pins to the current SCHEMA_VERSION (= 1 in v0.12.0
		# baseline). If the test starts failing on a future bump, that's
		# the migration moment bump together with redis_schema.
		assert stored["_v"] == fresh.CONFIG_CACHE_ENVELOPE_VERSION
		# Payload is the OptimusConfig.__dict__ shape same keys the
		# OptimusConfig dataclass exposes.
		assert isinstance(stored["data"], dict)
		assert "ai_enabled" in stored["data"]


class TestSettingsEnvelopeLegacyCompat:
	"""A cache HIT against a bare-dict value (no envelope) STILL returns a valid
	OptimusConfig, so new readers handle stale legacy values left by old
	writers."""

	def test_hit_on_legacy_bare_dict_returns_config(self, fresh):
		cache = _FakeCache()
		# Pre-seed with a BARE OptimusConfig field dict no envelope, no
		# ``_v`` key. This is exactly what pre-v0.12.11 writers stored.
		seed_payload = {**fresh.OptimusConfig().__dict__, "session_retention_days": 77}
		cache.store[fresh._CACHE_KEY] = seed_payload
		frappe_stub = _stub_frappe(cache)

		with mock.patch.dict(sys.modules, {"frappe": frappe_stub}):
			cfg = fresh.get_config()

		assert cfg is not None
		# A real cache hit: the seeded non-default value comes back.
		assert cfg.session_retention_days == 77
		# CRITICAL: the legacy value was NOT discarded it was used
		# as-is. (Migrating legacy values on read is explicitly OUT of
		# scope for the rollout; the next on_update cache invalidation
		# + re-resolve will produce a new-shape envelope.)
		stored = cache.store.get(fresh._CACHE_KEY)
		assert stored == seed_payload, (
			"legacy value was rewritten on read; the rollout's "
			"compatibility contract is 'pass through unchanged'"
		)


class TestSettingsEnvelopeDriftHandling:
	"""A cache HIT against a value with a MISMATCHED ``_v`` (future
	schema not yet supported) falls through to ``_resolve`` and stores a
	fresh new-shape envelope."""

	def test_drift_falls_through_to_resolve(self, fresh):
		cache = _FakeCache()
		# Seed with an envelope tagged as schema version 999 a future
		# version this build doesn't recognise.
		cache.store[fresh._CACHE_KEY] = {"_v": 999, "data": {"ai_enabled": True}}
		frappe_stub = _stub_frappe(cache, has_doctype=True)

		with mock.patch.dict(sys.modules, {"frappe": frappe_stub}):
			cfg = fresh.get_config()

		# Drift → fall-through to _resolve → returns defaults; the cache
		# is then re-written with the current envelope shape.
		assert cfg is not None
		stored = cache.store.get(fresh._CACHE_KEY)
		assert isinstance(stored, dict)
		assert stored.get("_v") == fresh.CONFIG_CACHE_ENVELOPE_VERSION, (
			f"after drift detection, cache should be re-written with the "
			f"current envelope version; got: {stored!r}"
		)
