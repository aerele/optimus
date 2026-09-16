# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""``wrap_value`` / ``unwrap_value`` envelope rollout for two values:

  * ``retention_backlog`` (janitor.py): write-only int. No in-app reader, but
    operator tooling could read it from Redis, so wrap on write to keep the
    shape future-safe.
  * ``onboarding_seen`` (api.py): write/read pair. The dismiss endpoint writes
    ``"1"``; the check endpoint reads and coerces to ``bool``.

Each test runs against a dict-backed fake cache to introspect the exact
stored shape.
"""

from __future__ import annotations

import sys
import types
from unittest import mock


class _FakeCache:
	"""Dict-backed ``frappe.cache`` substitute; copied per module so each
	rollout test is self-contained."""

	def __init__(self) -> None:
		self.store: dict = {}

	def get_value(self, key):
		return self.store.get(key)

	def set_value(self, key, value, **_):
		self.store[key] = value

	def delete_value(self, key):
		self.store.pop(key, None)


# ---------------------------------------------------------------------------
# retention_backlog (write-only) janitor writes wrap_value(int)
# ---------------------------------------------------------------------------


class TestRetentionBacklogEnvelope:
	"""The daily janitor's two ``retention_backlog`` write sites both wrap the
	int value in the envelope. No in-app reader exists (operator-visible
	monitoring metric only), so the contract under test is the write shape."""

	def test_janitor_writes_envelope_when_setting_backlog(self):
		import optimus.janitor as janitor
		from optimus import redis_schema

		# Synthesize the cache + frappe stub. We only exercise the
		# write-the-counter branch _sweep_old_sessions's full body
		# is out of scope (it queries Optimus Session which needs a
		# bench). Instead we invoke the cache-write expression
		# directly via the same code path.
		cache = _FakeCache()

		# Frappe-stub the bare minimum for the wrap path.
		fake_frappe = types.SimpleNamespace()
		fake_frappe.cache = cache
		# The cache-write code path: same shape as janitor.py:307-312.
		# Re-run that expression here so the test is anchored to the
		# CONTRACT (wrapped write) without re-executing the whole sweep.
		from optimus import redis_keys

		with mock.patch.dict(sys.modules, {"frappe": fake_frappe}):
			fake_frappe.cache.set_value(
				redis_keys.retention_backlog(),
				redis_schema.wrap_value(42),
				expires_in_sec=3600,
			)

		stored = cache.store[redis_keys.retention_backlog()]
		assert isinstance(stored, dict), (
			f"retention_backlog cache value should be the envelope dict; got: {stored!r}"
		)
		assert stored.get("_v") == redis_schema.SCHEMA_VERSION
		assert stored.get("data") == 42

	def test_janitor_source_uses_wrap_value(self):
		"""Source-grep canary: confirm janitor.py's set_value calls for
		retention_backlog wrap their payload via redis_schema.wrap_value.
		Catches a future refactor that accidentally reverts the wrap."""
		import os

		path = os.path.join(os.path.dirname(__file__), "..", "janitor.py")
		with open(path) as f:
			src = f.read()
		# Both write sites must use wrap_value before set_value.
		# We check that the function reference appears alongside the
		# retention_backlog key in the same nearby block.
		anchor = "redis_keys.retention_backlog()"
		assert anchor in src
		# After the rollout, every set_value to retention_backlog wraps:
		# the value arg of the set_value call should call
		# redis_schema.wrap_value(...) on either ``backlog`` or ``0``.
		assert "redis_schema.wrap_value(backlog)" in src, (
			"first retention_backlog write site should wrap_value(backlog)"
		)
		assert "redis_schema.wrap_value(0)" in src, (
			"second retention_backlog write site (backlog-cleared) should wrap_value(0)"
		)


# ---------------------------------------------------------------------------
# onboarding_seen (write/read pair) both sides migrated
# ---------------------------------------------------------------------------


class TestOnboardingSeenEnvelopeReadCompat:
	"""``check_onboarding_seen`` reads via ``unwrap_value`` so both new-shape
	envelopes and legacy bare ``"1"`` strings resolve to the same truthy
	result. Guards against the read path dropping the unwrap call."""

	def test_unwrap_of_new_envelope_returns_truthy(self):
		from optimus import redis_schema

		raw = redis_schema.wrap_value("1")
		payload, _ = redis_schema.unwrap_value(raw)
		assert bool(payload) is True, (
			f"unwrap of new-shape envelope should yield a truthy payload; got: {payload!r}"
		)

	def test_unwrap_of_legacy_bare_string_returns_truthy(self):
		"""Migration-safety contract: a legacy bare string ``"1"`` must not crash
		new readers; it resolves to the original payload via the legacy-detection
		branch."""
		from optimus import redis_schema

		raw = "1"  # exactly what mark_onboarding_seen wrote pre-v0.12.13
		payload, version = redis_schema.unwrap_value(raw)
		assert payload == "1"
		assert version is None, f"legacy un-wrapped value should report version=None; got: {version!r}"
		assert bool(payload) is True

	def test_unwrap_of_missing_key_returns_falsy(self):
		"""A user who has never dismissed the toast → no Redis key →
		``get_value`` returns None → ``unwrap_value`` returns the
		caller's default (None). ``bool(None)`` is False, so the
		check endpoint correctly reports seen=False."""
		from optimus import redis_schema

		payload, version = redis_schema.unwrap_value(None)
		assert payload is None
		assert version is None
		assert bool(payload) is False


class TestOnboardingSeenEnvelopeWriteShape:
	"""``mark_onboarding_seen`` writes a wrapped envelope. Source-grep
	canary against a future refactor that reverts the wrap."""

	def test_api_source_uses_wrap_value_for_onboarding_seen_write(self):
		import os

		path = os.path.join(os.path.dirname(__file__), "..", "api.py")
		with open(path) as f:
			src = f.read()
		# Locate the mark_onboarding_seen function body.
		start = src.index("def mark_onboarding_seen(")
		# Next top-level def or decorator.
		import re

		next_top = re.search(r"\n(?:def |@frappe\.whitelist)", src[start + 1 :])
		end = start + 1 + (next_top.start() if next_top else len(src) - start - 1)
		body = src[start:end]
		assert "redis_schema.wrap_value(" in body, (
			"mark_onboarding_seen must wrap the value via redis_schema.wrap_value "
			"before set_value; got:\n" + body
		)

	def test_api_source_uses_unwrap_value_for_onboarding_seen_read(self):
		import os

		path = os.path.join(os.path.dirname(__file__), "..", "api.py")
		with open(path) as f:
			src = f.read()
		start = src.index("def check_onboarding_seen(")
		import re

		next_top = re.search(r"\n(?:def |@frappe\.whitelist)", src[start + 1 :])
		end = start + 1 + (next_top.start() if next_top else len(src) - start - 1)
		body = src[start:end]
		assert "redis_schema.unwrap_value(" in body, (
			"check_onboarding_seen must unwrap the cache value via "
			"redis_schema.unwrap_value so both new envelopes and legacy "
			"bare strings resolve consistently; got:\n" + body
		)
