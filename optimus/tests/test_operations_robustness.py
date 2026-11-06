# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Operations: hook failures never break a request, repeated failures write one row a
minute across processes, the AI-store seed never fails its hook, and the settings cache
survives a mixed-version deploy."""

import dataclasses
import fnmatch
import json
import types
from importlib import import_module
from pathlib import Path

import pytest

from optimus import hooks, install, safe_call, settings

ROOT = Path(safe_call.__file__).resolve().parent


class FakeRedis:
	"""One Redis shared by every fake process; ``set`` honours NX."""

	def __init__(self):
		self.keys = {}

	def make_key(self, key):
		return f"db|{key}".encode()

	def set(self, name, value, nx=False, ex=None):
		if nx and name in self.keys:
			return None
		self.keys[name] = value
		return True


class BrokenRedis(FakeRedis):
	def set(self, *a, **kw):
		raise ConnectionError("redis down")


def _frappe(monkeypatch, cache, rows, *, log_error=None):
	import frappe

	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	monkeypatch.setattr(frappe, "local", types.SimpleNamespace(site="s1"), raising=False)
	monkeypatch.setattr(
		frappe, "log_error", log_error or (lambda **kw: rows.append(kw["title"])), raising=False
	)
	return frappe


def _new_process():
	"""RQ forks a work horse per job: the process dict starts empty."""
	safe_call._RECENT_LINES.clear()


# --- Error Log dedupe, shared through Redis, never raising ---------------------------


def test_two_processes_share_one_error_log_row_through_redis(monkeypatch):
	rows = []
	_frappe(monkeypatch, FakeRedis(), rows)
	safe_call.log_hook_error("optimus before_job")
	_new_process()
	safe_call.log_hook_error("optimus before_job")
	assert rows == ["optimus before_job"]


def test_a_broken_redis_falls_back_to_the_process_dict(monkeypatch):
	rows = []
	_frappe(monkeypatch, BrokenRedis(), rows)
	for _ in range(50):
		safe_call.log_hook_error("optimus before_request")
	assert rows == ["optimus before_request"]


def test_a_new_row_is_written_after_the_window(monkeypatch):
	rows = []
	_frappe(monkeypatch, BrokenRedis(), rows)
	clock = [100.0]
	monkeypatch.setattr("time.monotonic", lambda: clock[0])
	safe_call.log_hook_error("t")
	clock[0] += 59
	safe_call.log_hook_error("t")
	clock[0] += 2
	safe_call.log_hook_error("t")
	assert rows == ["t", "t"]


def test_different_titles_are_not_merged(monkeypatch):
	rows = []
	_frappe(monkeypatch, FakeRedis(), rows)
	safe_call.log_hook_error("a")
	safe_call.log_hook_error("b")
	assert rows == ["a", "b"]


def test_a_log_error_that_raises_does_not_escape(monkeypatch):
	def boom(**kw):
		raise RuntimeError("insert failed on an aborted transaction")

	_frappe(monkeypatch, FakeRedis(), [], log_error=boom)
	safe_call.log_hook_error("optimus after_request")


def test_the_message_is_passed_on_but_never_part_of_the_key(monkeypatch):
	import frappe

	seen = []
	cache = FakeRedis()
	_frappe(monkeypatch, cache, [], log_error=lambda **kw: seen.append(kw))
	safe_call.log_hook_error("optimus before_job unexpected kwargs", message="kwargs type: int")
	assert seen == [{"title": "optimus before_job unexpected kwargs", "message": "kwargs type: int"}]
	assert all(k.startswith(b"db|optimus:errlog:") and b"int" not in k for k in cache.keys)
	assert frappe.cache is cache


def test_the_log_line_dedupe_key_ignores_per_event_ids(monkeypatch):
	import frappe

	lines = []
	_frappe(monkeypatch, FakeRedis(), [])
	monkeypatch.setattr(
		frappe, "logger", lambda name=None: types.SimpleNamespace(error=lines.append), raising=False
	)
	for i in range(50):
		safe_call.log_error_line(f"cap hit, dropped rec-{i}", dedupe_key="cap hit for session s1")
	assert lines == ["cap hit, dropped rec-0"]
	_new_process()
	safe_call.log_error_line("cap hit, dropped rec-51", dedupe_key="cap hit for session s1")
	assert len(lines) == 1  # another process: Redis still holds the window


def _hook_env(monkeypatch):
	import frappe

	from optimus import hooks_callbacks as hc

	rows = []

	def down(user):
		raise RuntimeError("down")

	monkeypatch.setattr(hc, "session", types.SimpleNamespace(get_active_session_for=down))
	monkeypatch.setattr("optimus.settings.is_enabled", lambda: down("u"))
	monkeypatch.setattr(frappe, "local", types.SimpleNamespace(form_dict={}, site="s1"), raising=False)
	monkeypatch.setattr(frappe, "session", types.SimpleNamespace(user="u"), raising=False)
	monkeypatch.setattr(frappe, "cache", BrokenRedis(), raising=False)

	def boom(**kw):
		rows.append(kw["title"])
		raise RuntimeError("log insert failed")

	monkeypatch.setattr(frappe, "log_error", boom, raising=False)
	return hc, rows


@pytest.mark.parametrize("hook", ["before_request", "before_job"])
def test_a_failing_hook_with_a_failing_logger_never_raises(monkeypatch, hook):
	hc, rows = _hook_env(monkeypatch)
	for _ in range(5):
		getattr(hc, hook)()
	assert len(rows) <= 1


# --- levels ---------------------------------------------------------------------------


def test_the_sub_error_sites_use_the_error_level_line():
	for name in (
		"__init__.py",
		"hooks_callbacks.py",
		"capture.py",
		"janitor.py",
		"api.py",
		"line_profile/hooks.py",
		"line_profile/jobs.py",
	):
		text = (ROOT / name).read_text()
		assert "logger().warning(" not in text and 'logger("optimus").warning(' not in text, name


def test_the_import_time_probe_keeps_its_print_fallback():
	text = (ROOT / "__init__.py").read_text()
	assert "log = print" in text and 'log = frappe.logger("optimus").error' in text


def test_the_retention_backlog_marker_outlives_the_daily_sweep():
	from optimus import janitor

	assert janitor.RETENTION_BACKLOG_TTL_SECONDS == 25 * 3600


# --- persistent cache keys -------------------------------------------------------------


def test_every_optimus_key_pattern_is_persistent_but_the_settings_cache_is_not():
	from optimus import redis_keys

	patterns = hooks.persistent_cache_keys
	for pattern in redis_keys.KEY_PATTERNS:
		if not pattern.startswith(("optimus:", "profiler:")):
			continue
		concrete = pattern.replace("<", "x").replace(">", "x")
		assert any(fnmatch.fnmatch(concrete, p) for p in patterns), pattern
	assert not any(fnmatch.fnmatch(settings._CACHE_KEY, p) for p in patterns)


# --- the seed never fails its hook ------------------------------------------------------


class SeedDb:
	"""Records the order of calls; a COMMIT inside the index step models PostgreSQL's
	``add_index``, which destroys every savepoint."""

	def __init__(self):
		self.calls = []

	def savepoint(self, name):
		self.calls.append(("savepoint", name))

	def rollback(self, save_point=None):
		if save_point and ("commit",) in self.calls:
			raise RuntimeError("SAVEPOINT does not exist")
		self.calls.append(("rollback", save_point))


@pytest.fixture
def seed_env(monkeypatch):
	store = import_module("optimus.patches.v0_12_0.initialize_ai_refresh_store")
	db = SeedDb()
	logged = []
	monkeypatch.setattr(install, "frappe", types.SimpleNamespace(db=db))
	monkeypatch.setattr(install, "safe_commit", lambda: None)
	monkeypatch.setattr(safe_call, "log_hook_error", lambda title, **kw: logged.append(title))
	return store, db, logged, monkeypatch


def _raise(error):
	def step():
		raise error

	return step


def test_a_raising_seed_returns_logs_once_and_rolls_back_to_its_savepoint(seed_env):
	store, db, logged, mp = seed_env
	mp.setattr(store, "seed_control", _raise(RuntimeError("insert failed")))
	mp.setattr(store, "ensure_indexes", lambda: db.calls.append(("indexes",)))
	install._seed_ai_refresh_store()
	assert logged == ["optimus ai refresh store seed"]
	assert ("rollback", "optimus_ai_store_seed") in db.calls
	assert ("indexes",) in db.calls


def test_a_commit_inside_the_index_step_never_meets_a_savepoint_rollback(seed_env):
	store, db, logged, mp = seed_env
	mp.setattr(store, "seed_control", lambda: None)

	def indexes():
		db.calls.append(("commit",))
		raise RuntimeError("CREATE INDEX failed")

	mp.setattr(store, "ensure_indexes", indexes)
	install._seed_ai_refresh_store()
	assert logged == ["optimus ai refresh store indexes"]
	assert db.calls[-1] == ("rollback", None)
	assert not any(c[0] == "rollback" and c[1] for c in db.calls)


def test_a_rollback_that_itself_fails_does_not_escape(seed_env):
	store, db, logged, mp = seed_env
	db.rollback = lambda save_point=None: _raise(RuntimeError("gone"))()
	mp.setattr(store, "seed_control", _raise(RuntimeError("x")))
	mp.setattr(store, "ensure_indexes", lambda: None)
	install._seed_ai_refresh_store()
	assert logged == ["optimus ai refresh store seed"]


# --- the Phase Two Run scan index --------------------------------------------------------


def test_the_phase_two_status_index_goes_through_the_table_qualified_path():
	store = import_module("optimus.patches.v0_12_0.initialize_ai_refresh_store")
	assert store.SCAN_INDEXES == {"Optimus Phase Two Run": ("status",)}
	path = ROOT / "optimus" / "doctype" / "optimus_phase_two_run" / "optimus_phase_two_run.json"
	status = next(f for f in json.loads(path.read_text())["fields"] if f["fieldname"] == "status")
	assert not status.get("search_index")


# --- settings cache drift ----------------------------------------------------------------


def _settings_cache(monkeypatch, entry):
	import frappe

	reads = []
	cache = types.SimpleNamespace(
		get_value=lambda key, expires=False: entry,
		set_value=lambda *a, **kw: reads.append("write"),
	)
	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	monkeypatch.setattr(settings, "_read_doctype_row", lambda: reads.append("db") or {})
	return reads


def test_a_cached_payload_with_an_unknown_key_is_used_without_a_db_read(monkeypatch):
	from optimus import redis_schema

	payload = {**dataclasses.asdict(settings.OptimusConfig()), "a_field_from_a_newer_release": 1}
	reads = _settings_cache(
		monkeypatch,
		redis_schema.wrap_value(payload, version=settings.CONFIG_CACHE_ENVELOPE_VERSION),
	)
	assert isinstance(settings.get_config(), settings.OptimusConfig)
	assert reads == []


def test_an_old_release_envelope_is_ignored_not_crashed_on(monkeypatch):
	from optimus import redis_schema

	reads = _settings_cache(monkeypatch, redis_schema.wrap_value({"enabled": True}, version=1))
	settings.get_config()
	assert "db" in reads
	assert settings.CONFIG_CACHE_ENVELOPE_VERSION > 1


def test_the_index_step_commits_before_it_runs_on_either_dialect(seed_env):
	store, db, logged, mp = seed_env
	order = []
	mp.setattr(install, "safe_commit", lambda: order.append("commit"))
	mp.setattr(store, "seed_control", lambda: None)
	mp.setattr(store, "ensure_indexes", lambda: order.append("indexes"))
	for db_type in ("postgres", "mariadb"):
		order.clear()
		db.db_type = db_type
		install._seed_ai_refresh_store()
		assert order == ["commit", "indexes"], db_type
	assert logged == []


def test_a_failing_index_step_rolls_back_only_after_its_commit(seed_env):
	store, db, logged, mp = seed_env
	db.calls.clear()
	mp.setattr(install, "safe_commit", lambda: db.calls.append(("commit",)))
	mp.setattr(store, "seed_control", lambda: None)
	mp.setattr(store, "ensure_indexes", _raise(RuntimeError("CREATE INDEX failed")))
	install._seed_ai_refresh_store()
	assert db.calls.index(("commit",)) < db.calls.index(("rollback", None))


def _permission_env(monkeypatch):
	import frappe

	from optimus import api

	lines = []
	monkeypatch.setattr(
		frappe,
		"db",
		types.SimpleNamespace(get_value=lambda dt, flt, field: "PS-" + flt["session_uuid"]),
		raising=False,
	)
	monkeypatch.setattr(frappe, "cache", FakeRedis(), raising=False)
	monkeypatch.setattr(frappe, "local", types.SimpleNamespace(site="s1"), raising=False)
	monkeypatch.setattr(frappe, "has_permission", lambda *a: _raise(RuntimeError("down"))(), raising=False)
	monkeypatch.setattr(frappe, "throw", lambda *a, **k: _raise(PermissionError("denied"))(), raising=False)
	monkeypatch.setattr(frappe, "PermissionError", PermissionError, raising=False)
	monkeypatch.setattr(
		frappe, "logger", lambda name=None: types.SimpleNamespace(error=lines.append), raising=False
	)
	monkeypatch.setattr(api, "_", lambda s: s)
	return api, lines


def test_the_permission_line_is_keyed_without_the_document_name(monkeypatch):
	api, lines = _permission_env(monkeypatch)
	for uuid in ("a", "b", "c"):
		with pytest.raises(PermissionError):
			api._require_session_permission(uuid, "read")
	assert len(lines) == 1 and "PS-a" in lines[0]


def test_the_cap_hit_key_is_a_stable_title():
	text = (ROOT / "hooks_callbacks.py").read_text()
	assert text.count('dedupe_key="recording cap hit"') == 2


def _scan_patch(monkeypatch, db_type, table_exists):
	patch = import_module("optimus.patches.v0_12_0.initialize_ai_refresh_store")
	created = []
	db = types.SimpleNamespace(
		db_type=db_type,
		db_schema="public",
		sql=lambda q, v: [],
		table_exists=table_exists,
		add_index=lambda doctype, columns, index_name: created.append(doctype),
	)
	monkeypatch.setattr(
		patch,
		"frappe",
		types.SimpleNamespace(db=db, get_meta=lambda d: types.SimpleNamespace(fields=[])),
	)
	return patch, created


@pytest.mark.parametrize("db_type", ["postgres", "mariadb"])
def test_a_missing_phase_two_run_table_is_skipped(monkeypatch, db_type):
	patch, created = _scan_patch(monkeypatch, db_type, lambda doctype: False)
	patch.ensure_indexes()
	assert created == []
	patch, created = _scan_patch(monkeypatch, db_type, lambda doctype: True)
	patch.ensure_indexes()
	assert created == ["Optimus Phase Two Run"]
