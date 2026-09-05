# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Drift-protection audit for the Redis-keys centralization contract.

Every ``frappe.cache.*`` call site in ``optimus/`` must use a builder from
:mod:`optimus.redis_keys` for its key argument. Inline f-string keys
(``f"profiler:..."`` / ``f"optimus:..."``) inside a ``frappe.cache.X(...)`` call
are orphans and fail this test. The audit also asserts the doc inventory
(``docs/REDIS-SCHEMA.md``) matches the canonical
:data:`optimus.redis_keys.KEY_PATTERNS`; drift either way fails CI.

Excluded:
  * ``optimus/tests/`` and ``optimus/tests_integration/``: fixtures stub keys.
  * ``optimus/patches/``: one-shot migration scripts; literal-key uses are by
    design.
  * ``optimus/redis_keys.py`` itself: its f-string builder bodies are the
    canonical strings.

session.py and line_profile/capture.py have their own centralized helpers
(``_active_key``, ``_meta_key`` etc.); the audit doesn't flag them because those
are function returns, not ``frappe.cache.X(...)`` calls (it only inspects key
arguments at the call site).
"""

from __future__ import annotations

import re
from pathlib import Path

from optimus import redis_keys

# Excluded directories every .py file under these paths is skipped.
EXCLUDED_DIRS = (
	"optimus/tests/",
	"optimus/tests_integration/",
	"optimus/patches/",
)

# Excluded files the audit's own canonical-string definitions live
# here, so the f-strings inside :mod:`optimus.redis_keys` are legitimate.
EXCLUDED_FILES = (
	"optimus/redis_keys.py",
)

# Pattern matches a ``frappe.cache.<method>(`` call where ``<method>``
# is a key-taking cache operation. Excludes ``frappe.cache.get_redis_connection``
# / ``frappe.cache.make_key`` / similar that don't take a Redis key.
_CACHE_CALL_RE = re.compile(
	r"frappe\.cache\.(set_value|get_value|hset|hget|hgetall|hdel|"
	r"rpush|lpush|llen|lrange|ltrim|sadd|srem|smembers|"
	r"expire_key|delete_value|hkeys|hexists)\("
)

# Pattern matches an inline f-string key argument containing one of
# the Optimus namespace prefixes. The audit flags any call site whose
# line matches both _CACHE_CALL_RE AND _INLINE_KEY_RE.
_INLINE_KEY_RE = re.compile(
	r"""f"(?:profiler:|optimus:|optimus_settings_cached)|"""
	r"""f'(?:profiler:|optimus:|optimus_settings_cached)"""
)


def _repo_root() -> Path:
	"""Resolve the repository root (the ``apps/optimus`` checkout) from this test
	file's location. Works regardless of cwd."""
	return Path(__file__).resolve().parent.parent.parent


def _is_excluded(posix_path: str) -> bool:
	if posix_path in EXCLUDED_FILES:
		return True
	return any(posix_path.startswith(d) for d in EXCLUDED_DIRS)


def _find_orphan_inline_keys() -> list[str]:
	"""Walk every .py under optimus/ outside the exclusion list; record an orphan
	for each line with a ``frappe.cache.X(`` call and an inline f-string key."""
	root = _repo_root()
	orphans: list[str] = []
	for path in sorted((root / "optimus").rglob("*.py")):
		posix = path.relative_to(root).as_posix()
		if _is_excluded(posix):
			continue
		try:
			lines = path.read_text(encoding="utf-8").splitlines()
		except Exception:
			orphans.append(f"{posix}  (could not read)")
			continue
		for i, line in enumerate(lines):
			if not _CACHE_CALL_RE.search(line):
				continue
			# If the key argument is inline on the same line AND it
			# starts with an Optimus namespace prefix, it's an orphan.
			# Multi-line cache calls (where the key is on the next line
			# via a continuation) require the f-string + the prefix to
			# co-occur with the cache call i.e. the WRITER wrote
			# `frappe.cache.X(f"profiler:..."` inline. If the key was
			# extracted to a previous line via a helper call, this
			# pattern won't match and that's the desired behaviour.
			if _INLINE_KEY_RE.search(line):
				orphans.append(f"{posix}:{i + 1}  {line.strip()}")
	return orphans


def _parse_documented_patterns(doc_text: str) -> list[str]:
	r"""Extract the key patterns from REDIS-SCHEMA.md's § 2 tables: the first
	backtick-wrapped token on each markdown table data row."""
	# A table row looks like: ``| `profiler:active:<user>` | string | …``
	# So the first backtick-wrapped token after the opening pipe is the key.
	row_re = re.compile(r"^\|\s*`([^`]+)`\s*\|")
	patterns: list[str] = []
	for line in doc_text.splitlines():
		m = row_re.match(line)
		if m:
			patterns.append(m.group(1).strip())
	return patterns


class TestEveryRedisCallUsesKeyBuilder:
	"""Drift canary: no inline f-string keys inside ``frappe.cache.X(...)`` calls
	outside the exclusion list. On failure, refactor the call to use
	``optimus.redis_keys.<feature>(...)`` (add a builder if none exists)."""

	def test_no_orphan_inline_keys(self):
		orphans = _find_orphan_inline_keys()
		assert not orphans, (
			"frappe.cache calls with inline f-string keys (use "
			"optimus.redis_keys.* instead):\n  " + "\n  ".join(orphans)
		)


class TestKeyBuildersMatchDoc:
	"""Drift canary: ``redis_keys.KEY_PATTERNS`` must equal the patterns documented
	in ``docs/REDIS-SCHEMA.md`` (adding a key without documenting it, or vice
	versa, fails here)."""

	def test_redis_keys_match_documented_schema(self):
		root = _repo_root()
		doc_path = root / "docs" / "REDIS-SCHEMA.md"
		assert doc_path.exists(), f"REDIS-SCHEMA.md missing at {doc_path}"
		doc_text = doc_path.read_text(encoding="utf-8")
		documented = sorted(set(_parse_documented_patterns(doc_text)))
		canonical = sorted(set(redis_keys.KEY_PATTERNS))
		missing_in_doc = sorted(set(canonical) - set(documented))
		missing_in_code = sorted(set(documented) - set(canonical))
		assert documented == canonical, (
			"Drift between redis_keys.KEY_PATTERNS and docs/REDIS-SCHEMA.md:\n"
			f"  in code but not documented:   {missing_in_doc!r}\n"
			f"  in doc but not in code:       {missing_in_code!r}"
		)


class TestSchemaSentinel:
	"""The sentinel write/read pair is idempotent and round-trips through the
	Frappe-cache stub."""

	def test_write_sentinel_idempotent(self, monkeypatch):
		# Install a tiny frappe.cache stub that stores set_value /
		# get_value in a dict exercises the real code path without
		# needing a bench.
		import sys
		from types import SimpleNamespace

		store: dict = {}

		class _FakeCache:
			def set_value(self, key, value, expires_in_sec=None):
				store[key] = value

			def get_value(self, key):
				return store.get(key)

		fake_frappe = SimpleNamespace(cache=_FakeCache())
		# Insert into sys.modules so the lazy import inside the helpers
		# sees the fake.
		monkeypatch.setitem(sys.modules, "frappe", fake_frappe)

		from optimus import redis_schema

		redis_schema.write_schema_sentinel()
		redis_schema.write_schema_sentinel()  # idempotent
		assert store.get("optimus:schema_version") == redis_schema.SCHEMA_VERSION


class TestWrapUnwrap:
	"""Versioned-value envelope round-trip + drift detection."""

	def test_wrap_roundtrip_current_version(self):
		from optimus import redis_schema

		payload = {"hello": "world", "count": 42}
		wrapped = redis_schema.wrap_value(payload)
		unwrapped, version = redis_schema.unwrap_value(wrapped)
		assert unwrapped == payload
		assert version == redis_schema.SCHEMA_VERSION

	def test_unwrap_legacy_returns_none_version(self):
		"""A bare dict (legacy shape) flows through as-is: no envelope, no drift."""
		from optimus import redis_schema

		legacy = {"some": "raw_payload"}
		unwrapped, version = redis_schema.unwrap_value(legacy)
		assert unwrapped == legacy
		assert version is None

	def test_unwrap_unknown_version_returns_default(self):
		"""A future schema version returns the caller's ``default`` so the host
		degrades gracefully instead of crashing on a shape it can't read."""
		from optimus import redis_schema

		future_value = {"_v": 99, "data": {"future_field": True}}
		unwrapped, version = redis_schema.unwrap_value(
			future_value, expected=1, default="MISSING"
		)
		assert unwrapped == "MISSING"
		assert version == 99

	def test_unwrap_none_value_returns_default(self):
		"""Missing key (``get_value`` returned ``None``) → default."""
		from optimus import redis_schema

		unwrapped, version = redis_schema.unwrap_value(None, default=[])
		assert unwrapped == []
		assert version is None
