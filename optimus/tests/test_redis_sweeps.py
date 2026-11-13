# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The janitor orphan sweep and the uninstall cleanup must delete keys through
``frappe.cache``, which has no ``get_redis_connection`` method."""

import fnmatch
import types
from datetime import datetime, timedelta

import pytest

PREFIX = "_site|"


class FakeRedisWrapper:
	"""Like Frappe's RedisWrapper: raw scan/delete plus make_key, and the key/hash helpers."""

	def __init__(self, keys, members=None):
		self.store = {PREFIX + k for k in keys}
		self.members = {PREFIX + k: set(v) for k, v in (members or {}).items()}
		self.hdels = []
		self.fail_hdel = False

	def make_key(self, key):
		return (PREFIX + key).encode()

	def scan(self, cursor, match=None, count=None):
		return 0, [k.encode() for k in self.store if fnmatch.fnmatch(k, match)]

	def delete(self, *keys):
		names = [k.decode() if isinstance(k, bytes) else k for k in keys]
		hit = [k for k in names if k in self.store]
		self.store.difference_update(hit)
		return len(hit)

	def delete_value(self, key):
		self.delete(PREFIX + key)
		self.members.pop(PREFIX + key, None)

	def smembers(self, key):
		return {m.encode() for m in self.members.get(PREFIX + key, set())}

	def hdel(self, name, key):
		if self.fail_hdel:
			raise ConnectionError("redis down")
		self.hdels.append((name, key))


class FakeLogger:
	def __init__(self):
		self.lines = []

	def info(self, message):
		self.lines.append(("info", message))

	def warning(self, message):
		self.lines.append(("warning", message))

	def error(self, message):
		self.lines.append(("error", message))


NOW = datetime(2026, 11, 6, 12, 0, 0)
LONG_AGO = NOW - timedelta(days=2)


@pytest.fixture
def fake_frappe(monkeypatch):
	def _install(module, keys, session_rows=(), members=None):
		# Patch the frappe the module holds; other tests reload it against their own stub.
		frappe = module.frappe
		cache = FakeRedisWrapper(keys, members)
		rows = {}
		for row in session_rows:  # a uuid, or (uuid, status, modified)
			uuid, status, modified = (row, "Recording", LONG_AGO) if isinstance(row, str) else row
			rows[uuid] = {"session_uuid": uuid, "status": status, "modified": modified}
		monkeypatch.setattr(frappe, "cache", cache, raising=False)
		monkeypatch.setattr(
			frappe, "db", types.SimpleNamespace(get_all=lambda *a, **k: list(rows.values())), raising=False
		)
		monkeypatch.setattr(module, "now_datetime", lambda: NOW, raising=False)
		monkeypatch.setattr(module, "add_to_date", lambda d, **kw: d + timedelta(**kw), raising=False)
		cache.log = FakeLogger()
		monkeypatch.setattr(frappe, "logger", lambda *a, **kw: cache.log, raising=False)
		cache.lines = []
		from optimus import safe_call

		monkeypatch.setattr(
			safe_call, "log_error_line", lambda message, **kw: cache.lines.append((message, kw)), raising=False
		)
		# analyze and janitor share the cache the test installed.
		from optimus import analyze, session

		if hasattr(module, "session"):  # an earlier test may have reloaded the module with a stub
			monkeypatch.setattr(module, "session", session)
		monkeypatch.setattr(analyze.frappe, "cache", cache, raising=False)
		monkeypatch.setattr(session.frappe, "cache", cache, raising=False)
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


def test_orphan_sweep_removes_the_recorder_entries_the_orphan_set_lists(fake_frappe):
	from frappe.recorder import RECORDER_REQUEST_HASH, RECORDER_REQUEST_SPARSE_HASH

	from optimus import janitor

	cache = fake_frappe(
		janitor,
		["profiler:session:dead:recordings", "profiler:session:dead:meta"],
		members={"profiler:session:dead:recordings": {"r1", "r2"}},
	)
	janitor._sweep_orphan_redis_state()

	assert sorted(cache.hdels) == sorted(
		(hash_name, uuid) for uuid in ("r1", "r2") for hash_name in (RECORDER_REQUEST_HASH, RECORDER_REQUEST_SPARSE_HASH)
	)
	assert cache.store == set()


def test_a_ready_session_that_still_has_its_set_is_cleaned_and_logged_once_through_the_level_aware_helper(
	fake_frappe,
):
	from optimus import janitor

	cache = fake_frappe(
		janitor,
		["profiler:session:done:recordings", "profiler:session:done:meta"],
		session_rows=[("done", "Ready", LONG_AGO)],
		members={"profiler:session:done:recordings": {"late-job"}},
	)
	janitor._sweep_orphan_redis_state()

	assert cache.store == set()
	assert any(name == "late-job" for _, name in cache.hdels)
	assert len(cache.lines) == 1 and "1 Ready sessions" in cache.lines[0][0]
	assert cache.lines[0][1]["dedupe_seconds"] >= 3600
	assert cache.log.lines == []  # frappe.logger() is silent at the production level


def test_orphans_are_logged_through_the_same_helper(fake_frappe):
	from optimus import janitor

	cache = fake_frappe(janitor, ["profiler:session:dead:meta"])
	janitor._sweep_orphan_redis_state()

	assert len(cache.lines) == 1 and "no row" in cache.lines[0][0]
	assert cache.lines[0][1]["dedupe_seconds"] >= 3600
	assert cache.log.lines == []


def test_nothing_is_logged_when_nothing_was_cleaned(fake_frappe):
	from optimus import janitor

	cache = fake_frappe(janitor, ["profiler:session:live:meta"], session_rows=["live"])
	janitor._sweep_orphan_redis_state()

	assert cache.lines == [] and cache.log.lines == []


def test_a_cleanup_that_failed_is_not_counted_or_logged_as_done(fake_frappe):
	from optimus import janitor

	cache = fake_frappe(
		janitor,
		["profiler:session:dead:recordings"],
		members={"profiler:session:dead:recordings": {"r1"}},
	)
	cache.fail_hdel = True
	janitor._sweep_orphan_redis_state()

	assert not any("no row" in message for message, _ in cache.lines)


def test_leftovers_only_get_the_cap_the_orphans_left_over(fake_frappe, monkeypatch):
	from optimus import janitor

	monkeypatch.setattr(janitor, "MAX_DELETIONS_PER_RUN", 3)
	keys = [f"profiler:session:dead{i}:meta" for i in range(2)]
	keys += [f"profiler:session:done{i}:meta" for i in range(3)]
	cache = fake_frappe(
		janitor, keys, session_rows=[(f"done{i}", "Ready", LONG_AGO) for i in range(3)]
	)
	janitor._sweep_orphan_redis_state()

	remaining = {k.split(":")[2] for k in cache.store}
	assert len(remaining) == 2 and remaining <= {"done0", "done1", "done2"}  # 2 orphans + 1 leftover cleaned


def test_a_ready_session_that_just_finished_is_left_to_its_own_cleanup(fake_frappe):
	from optimus import janitor

	cache = fake_frappe(
		janitor,
		["profiler:session:done:recordings"],
		session_rows=[("done", "Ready", NOW - timedelta(minutes=5))],
	)
	janitor._sweep_orphan_redis_state()

	assert cache.store == {PREFIX + "profiler:session:done:recordings"}


@pytest.mark.parametrize("status", ["Recording", "Stopping", "Capturing Background Jobs", "Analyzing", "Failed"])
def test_sessions_that_may_still_be_analyzed_or_retried_are_never_touched(fake_frappe, status):
	from optimus import janitor

	cache = fake_frappe(
		janitor,
		["profiler:session:s:recordings", "profiler:session:s:meta"],
		session_rows=[("s", status, LONG_AGO)],
		members={"profiler:session:s:recordings": {"r1"}},
	)
	janitor._sweep_orphan_redis_state()

	assert len(cache.store) == 2 and cache.hdels == []


def test_a_failed_delete_keeps_the_set_for_the_next_run(fake_frappe):
	from optimus import janitor

	cache = fake_frappe(
		janitor,
		["profiler:session:dead:recordings"],
		members={"profiler:session:dead:recordings": {"r1"}},
	)
	cache.fail_hdel = True
	janitor._sweep_orphan_redis_state()

	assert cache.store == {PREFIX + "profiler:session:dead:recordings"}


def test_one_run_cleans_at_most_max_deletions_sessions(fake_frappe, monkeypatch):
	from optimus import janitor

	monkeypatch.setattr(janitor, "MAX_DELETIONS_PER_RUN", 2)
	cache = fake_frappe(janitor, [f"profiler:session:dead{i}:meta" for i in range(5)])
	janitor._sweep_orphan_redis_state()

	assert len(cache.store) == 3


def test_uninstall_clears_all_optimus_keys(fake_frappe):
	from optimus import install

	cache = fake_frappe(install, ["profiler:session:a:meta", "optimus:schema_version", "optimus_settings_cached", "other:key"])
	install._clear_redis_state()
	assert cache.store == {PREFIX + "other:key"}
