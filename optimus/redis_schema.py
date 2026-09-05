# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Versioned value envelope + schema-version sentinel for Optimus's Redis state.

Provides the single source-of-truth schema version (:data:`SCHEMA_VERSION`), a
sentinel key written at app import (:func:`write_schema_sentinel`) and opt-in
envelope helpers (:func:`wrap_value` / :func:`unwrap_value`) that future schema
changes can use to keep new code safe against in-flight Redis values from older
releases.

Contract for a future schema change:

1. Bump :data:`SCHEMA_VERSION` (keep it in sync with
   :data:`optimus.redis_keys.SCHEMA_VERSION`).
2. On write, wrap the payload via :func:`wrap_value`.
3. On read, :func:`unwrap_value` returns ``(payload, version)``:
   version == SCHEMA_VERSION uses ``payload``; version is None is a legacy
   un-enveloped value (treat as bare payload); any other version returns the
   caller's ``default`` so the host degrades gracefully.

Opt-in only: existing values are not wrapped, the HMAC envelope is unchanged and
no janitor sweep runs off the sentinel. The module imports nothing from Frappe at
top level (helpers lazy-import) so pure-pytest tests need no bench.
"""

from __future__ import annotations

from typing import Any

# v0.12.0 baseline. Bumping this constant signals to downstream readers
# that any in-flight value with a different ``_v`` (or no ``_v``) field
# may need migration. Bump together with
# :data:`optimus.redis_keys.SCHEMA_VERSION`.
SCHEMA_VERSION = 1

# Sentinel field name used inside :func:`wrap_value` envelopes. Short
# to keep storage overhead negligible; underscored to avoid clashing
# with payload keys.
_ENVELOPE_VERSION_FIELD = "_v"
_ENVELOPE_PAYLOAD_FIELD = "data"


# ---------------------------------------------------------------------------
# Envelope helpers opt-in for new schema-change code paths
# ---------------------------------------------------------------------------


def wrap_value(payload: Any, *, version: int = SCHEMA_VERSION) -> dict:
	"""Return a versioned envelope around ``payload`` for storage in Redis.

	Shape: ``{"_v": <int>, "data": <payload>}``. Callers writing bare
	(un-wrapped) values are unaffected: readers detect the legacy shape by the
	absent ``_v`` key.
	"""
	return {_ENVELOPE_VERSION_FIELD: int(version), _ENVELOPE_PAYLOAD_FIELD: payload}


def unwrap_value(
	value: Any,
	*,
	expected: int = SCHEMA_VERSION,
	default: Any = None,
) -> tuple[Any, int | None]:
	"""Inspect ``value`` and return ``(payload, version)``:

	  * ``value is None`` (missing key) → ``(default, None)``.
	  * dict with ``_v == expected`` → ``(value["data"], expected)``.
	  * dict with ``_v != expected`` → ``(default, <observed>)``.
	  * anything else (legacy un-wrapped) → ``(value, None)``.

	Legacy un-wrapped values flow through unchanged (migration-safety net).
	"""
	if value is None:
		return default, None
	# Require BOTH envelope fields. ``wrap_value`` always writes both, so a dict
	# carrying only ``_v`` (a legacy payload that happens to use that key) is NOT
	# an envelope treating it as one would discard its real value.
	if (
		isinstance(value, dict)
		and _ENVELOPE_VERSION_FIELD in value
		and _ENVELOPE_PAYLOAD_FIELD in value
	):
		try:
			observed = int(value.get(_ENVELOPE_VERSION_FIELD) or 0)
		except (TypeError, ValueError):
			observed = 0
		if observed == int(expected):
			return value.get(_ENVELOPE_PAYLOAD_FIELD), observed
		# Drift return the caller's default so the host code path can
		# degrade gracefully. We DON'T try to migrate inline; that would
		# couple this helper to every value shape's migration rules. A
		# future PR can ship the migration.
		return default, observed
	# Legacy un-wrapped value (or a non-dict like a raw string). Pass
	# through; the caller treats it as the bare payload.
	return value, None


# ---------------------------------------------------------------------------
# Sentinel key written at app import, read by future migration paths
# ---------------------------------------------------------------------------


def write_schema_sentinel() -> None:
	"""Write the current :data:`SCHEMA_VERSION` to the sentinel key. Idempotent;
	runs once per worker boot. Best-effort: a Redis hiccup at app import must
	never break app load, so any failure is swallowed.
	"""
	try:
		import frappe

		from optimus import redis_keys

		frappe.cache.set_value(
			redis_keys.schema_version(),
			SCHEMA_VERSION,
		)
	except Exception:
		# Sentinel-write failure is non-fatal app continues to function;
		# the next worker boot retries.
		pass


def read_schema_sentinel() -> int | None:
	"""Return the persisted schema version, or ``None`` when the sentinel is
	missing (fresh install or Redis flush). Best-effort: any read failure
	returns ``None``.
	"""
	try:
		import frappe

		from optimus import redis_keys

		raw = frappe.cache.get_value(redis_keys.schema_version())
	except Exception:
		return None
	if raw is None:
		return None
	try:
		return int(raw)
	except (TypeError, ValueError):
		# Sentinel got corrupted (manually set to a non-int?). Treat as
		# missing so the next write_schema_sentinel() overwrites it.
		return None
