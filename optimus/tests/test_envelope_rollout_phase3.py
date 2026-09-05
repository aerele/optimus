# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests the ``wrap_value`` / ``unwrap_value`` envelope on ``explain_cache``
(the cross-session EXPLAIN-result cache in ``analyze.py``), whose payload is a
list of dicts, to confirm ``unwrap_value`` preserves nested structure."""

from __future__ import annotations


class TestExplainCacheEnvelopeRoundTrip:
	"""Round-trip tests on the envelope helpers alone (the analyze.py call site
	uses the same wrap-on-write / unwrap-on-read pattern)."""

	def test_list_of_dicts_roundtrip(self):
		"""wrap + unwrap must preserve a list[dict] EXPLAIN payload, including
		inner value types."""
		from optimus import redis_schema

		payload = [
			{"id": 1, "select_type": "SIMPLE", "table": "tabUser", "rows": 100},
			{"id": 2, "select_type": "SIMPLE", "table": "tabSession", "rows": 5},
		]
		wrapped = redis_schema.wrap_value(payload)
		unwrapped, version = redis_schema.unwrap_value(wrapped)
		assert unwrapped == payload, f"list-of-dicts payload should round-trip unchanged; got: {unwrapped!r}"
		assert version == redis_schema.SCHEMA_VERSION

	def test_empty_list_roundtrip(self):
		"""A failed EXPLAIN returns []; the empty-list shape must round-trip."""
		from optimus import redis_schema

		wrapped = redis_schema.wrap_value([])
		unwrapped, version = redis_schema.unwrap_value(wrapped)
		assert unwrapped == []
		assert version == redis_schema.SCHEMA_VERSION

	def test_legacy_bare_list_passes_through(self):
		"""A legacy bare list[dict] with no envelope must pass through
		``unwrap_value`` unchanged (version None)."""
		from optimus import redis_schema

		legacy_payload = [
			{"select_type": "SIMPLE", "table": "tabRole", "rows": 50},
		]
		unwrapped, version = redis_schema.unwrap_value(legacy_payload)
		assert unwrapped == legacy_payload, (
			"legacy bare-list shape must pass through unchanged so "
			"new readers don't drop pre-v0.12.17 cached EXPLAIN results"
		)
		assert version is None, f"legacy un-wrapped value should report version=None; got: {version!r}"


class TestAnalyzeSourceUsesEnvelope:
	"""Source-grep canary: confirm analyze.py's explain_cache write and read both
	use the envelope helpers, catching a refactor that reverts wrap / unwrap."""

	def test_explain_cache_write_wraps_via_redis_schema(self):
		import os

		path = os.path.join(os.path.dirname(__file__), "..", "analyze.py")
		with open(path) as f:
			src = f.read()
		# Locate the explain_cache shared-cache write block. Anchor on
		# the shared_key variable that the explain branch builds, then
		# look for wrap_value nearby.
		assert "_redis_keys.explain_cache(" in src, (
			"analyze.py should reference _redis_keys.explain_cache for the shared-cache key"
		)
		# The set_value call must wrap its payload via _redis_schema.
		assert "_redis_schema.wrap_value(result)" in src, (
			"explain_cache write must wrap the EXPLAIN result via _redis_schema.wrap_value before set_value"
		)

	def test_explain_cache_read_unwraps_via_redis_schema(self):
		import os

		path = os.path.join(os.path.dirname(__file__), "..", "analyze.py")
		with open(path) as f:
			src = f.read()
		assert "_redis_schema.unwrap_value(raw_cached)" in src, (
			"explain_cache read must unwrap the cached value via "
			"_redis_schema.unwrap_value so both new envelopes and legacy "
			"bare lists resolve consistently"
		)
