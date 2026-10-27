# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The janitor orphan sweep and the uninstall cleanup must delete keys through
``frappe.cache``, which has no ``get_redis_connection`` method."""

import fnmatch
import types

import pytest

PREFIX = "_site|"


class FakeRedisWrapper:
	"""Like Frappe's RedisWrapper: raw scan/delete plus make_key, nothing else."""

	def __init__(self, keys):
		self.store = {PREFIX + k for k in keys}

	def make_key(self, key):
		return (PREFIX + key).encode()

	def scan(self, cursor, match=None, count=None):
		return 0, [k.encode() for k in self.store if fnmatch.fnmatch(k, match)]

	def delete(self, *keys):
		names = [k.decode() if isinstance(k, bytes) else k for k in keys]
		hit = [k for k in names if k in self.store]
		self.store.difference_update(hit)
		return len(hit)


@pytest.fixture
def fake_frappe(monkeypatch):
	def _install(module, keys, session_rows=()):
		# Patch the frappe the module holds; other tests reload it against their own stub.
		frappe = module.frappe
		cache = FakeRedisWrapper(keys)
		monkeypatch.setattr(frappe, "cache", cache, raising=False)
		monkeypatch.setattr(
			frappe,
			"db",
			types.SimpleNamespace(get_all=lambda *a, **k: [{"session_uuid": u} for u in session_rows]),
			raising=False,
		)
		monkeypatch.setattr(frappe, "logger", lambda: types.SimpleNamespace(info=lambda m: None), raising=False)
		return cache

	return _install


def test_orphan_sweep_deletes_only_orphan_session_keys(fake_frappe):
	from optimus import janitor

	cache = fake_frappe(
		janitor,
		["profiler:session:dead:meta", "profiler:session:live:meta", "profiler:frontend:dead:xhr"],
		session_rows=["live"],
	)
	janitor._sweep_orphan_redis_state()
	assert cache.store == {PREFIX + "profiler:session:live:meta"}


def test_uninstall_clears_all_optimus_keys(fake_frappe):
	from optimus import install

	cache = fake_frappe(install, ["profiler:session:a:meta", "optimus:schema_version", "optimus_settings_cached", "other:key"])
	install._clear_redis_state()
	assert cache.store == {PREFIX + "other:key"}
