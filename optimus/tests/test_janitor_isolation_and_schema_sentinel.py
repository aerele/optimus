# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Janitor sweep failure isolation and the Redis schema sentinel."""

import types

import frappe
import pytest

from optimus import janitor, redis_keys, redis_schema


@pytest.fixture
def errors(monkeypatch):
	logged = []
	monkeypatch.setattr(janitor.frappe, "log_error", lambda *a, **kw: logged.append(kw.get("title")), raising=False)
	return logged


def _boom():
	raise RuntimeError("sweep failed")


class TestJanitorEntryPoints:
	def test_one_failing_sweep_does_not_stop_the_other_stale_sweeps(self, errors, monkeypatch):
		ran = []
		monkeypatch.setattr(janitor, "_sweep_stale_recording", _boom)
		monkeypatch.setattr(janitor, "_sweep_stuck_analyzing", lambda: ran.append("analyzing"))
		monkeypatch.setattr(janitor, "_sweep_stale_stopping", _boom)
		monkeypatch.setattr(janitor, "_sweep_stale_phase2_runs", lambda: ran.append("phase2"))

		janitor.sweep_stale_sessions()

		assert ran == ["analyzing", "phase2"]
		assert errors == [
			"optimus janitor sweep_stale_recording",
			"optimus janitor sweep_stale_stopping",
		]

	def test_failed_retention_sweep_still_cleans_orphan_redis_state(self, errors, monkeypatch):
		ran = []
		monkeypatch.setattr(janitor, "_sweep_old_sessions", _boom)
		monkeypatch.setattr(janitor, "_sweep_orphan_redis_state", lambda: ran.append("orphans"))

		janitor.sweep_old_sessions()

		assert ran == ["orphans"]
		assert errors == ["optimus janitor sweep_old_sessions"]

	def test_failed_orphan_sweep_is_logged_not_raised(self, errors, monkeypatch):
		monkeypatch.setattr(janitor, "_sweep_old_sessions", lambda: None)
		monkeypatch.setattr(janitor, "_sweep_orphan_redis_state", _boom)

		janitor.sweep_old_sessions()

		assert errors == ["optimus janitor sweep_orphan_redis_state"]


class TestSchemaSentinel:
	def _cache(self, monkeypatch, **methods):
		monkeypatch.setattr(frappe, "cache", types.SimpleNamespace(**methods), raising=False)

	def test_write_then_read_round_trips_the_current_version(self, monkeypatch):
		store = {}
		self._cache(
			monkeypatch,
			set_value=store.__setitem__,
			get_value=store.get,
		)

		redis_schema.write_schema_sentinel()

		assert store == {redis_keys.schema_version(): redis_schema.SCHEMA_VERSION}
		assert redis_schema.read_schema_sentinel() == redis_schema.SCHEMA_VERSION

	def test_missing_sentinel_reads_as_none(self, monkeypatch):
		self._cache(monkeypatch, get_value=lambda key: None)

		assert redis_schema.read_schema_sentinel() is None

	@pytest.mark.parametrize("raw", ["garbage", [1], {}])
	def test_corrupt_sentinel_reads_as_none_so_the_next_write_repairs_it(self, monkeypatch, raw):
		self._cache(monkeypatch, get_value=lambda key: raw)

		assert redis_schema.read_schema_sentinel() is None

	def test_redis_down_never_breaks_app_load(self, monkeypatch):
		def _down(*a, **kw):
			raise ConnectionError("redis down")

		self._cache(monkeypatch, set_value=_down, get_value=_down)

		redis_schema.write_schema_sentinel()  # must not raise
		assert redis_schema.read_schema_sentinel() is None
