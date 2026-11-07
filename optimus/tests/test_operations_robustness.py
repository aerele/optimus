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
	"""One Redis shared by every fake process; ``set`` honours NX and records the TTL."""

	def __init__(self):
		self.keys = {}
		self.ttls = {}

	def delete(self, name):
		self.keys.pop(name, None)

	def make_key(self, key):
		return f"db|{key}".encode()

	def set(self, name, value, nx=False, ex=None):
		if nx and name in self.keys:
			return None
		self.keys[name] = value
		self.ttls[name] = ex
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


def _log_failing_with(error, title="optimus before_job"):
	try:
		raise error
	except Exception:
		safe_call.log_hook_error(title)


def test_a_different_exception_class_under_one_title_is_not_hidden(monkeypatch):
	rows = []
	_frappe(monkeypatch, FakeRedis(), rows)
	_log_failing_with(ValueError("a"))
	_log_failing_with(ValueError("b"))
	_log_failing_with(KeyError("c"))
	assert rows == ["optimus before_job", "optimus before_job"]


def test_the_window_survives_the_redis_ttl_of_exactly_the_dedupe_seconds(monkeypatch):
	cache = FakeRedis()
	_frappe(monkeypatch, cache, [])
	safe_call.log_hook_error("a")
	safe_call.log_hook_error("b", dedupe_seconds=5)
	safe_call.log_hook_error("c", dedupe_seconds=0.2)
	assert sorted(cache.ttls.values()) == [1, 5, 60]


def test_the_same_title_on_another_site_is_logged_again(monkeypatch):
	import frappe

	rows = []
	_frappe(monkeypatch, BrokenRedis(), rows)
	safe_call.log_hook_error("t")
	monkeypatch.setattr(frappe, "local", types.SimpleNamespace(site="s2"), raising=False)
	safe_call.log_hook_error("t")
	assert rows == ["t", "t"]


@pytest.mark.parametrize("cache_class", [FakeRedis, BrokenRedis])
def test_a_title_and_a_log_line_of_one_text_do_not_share_a_slot(monkeypatch, cache_class):
	import frappe

	lines, rows = [], []
	_frappe(monkeypatch, cache_class(), rows)
	monkeypatch.setattr(
		frappe, "logger", lambda name=None: types.SimpleNamespace(error=lines.append), raising=False
	)
	# outside an except the title's key text is "<title>|", so the line uses that text
	safe_call.log_hook_error("same text")
	if cache_class is FakeRedis:
		_new_process()  # only the shared slot is left to tell them apart
	safe_call.log_error_line("same text|")
	assert rows == ["same text"] and lines == ["same text|"]


def test_a_failed_insert_releases_the_shared_slot_for_the_other_processes(monkeypatch):
	cache = FakeRedis()
	rows, calls = [], []

	def log_error(**kw):
		calls.append(kw)
		if len(calls) == 1:
			raise RuntimeError("insert failed")
		rows.append(kw["title"])

	_frappe(monkeypatch, cache, rows, log_error=log_error)
	safe_call.log_hook_error("t")
	assert cache.keys == {}
	safe_call.log_hook_error("t")  # same process: tried once a window, not on every request
	assert rows == []
	_new_process()
	safe_call.log_hook_error("t")
	safe_call.log_hook_error("t")
	assert rows == ["t"]


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


def _cap_hit_env(monkeypatch):
	"""``after_job`` for a session at its recording limit, with a real error-line dedupe."""
	import frappe

	from optimus import hooks_callbacks as hc

	levels = {"error": [], "warning": []}
	logger = types.SimpleNamespace(
		error=levels["error"].append, warning=levels["warning"].append, info=lambda m: None
	)
	_frappe(monkeypatch, FakeRedis(), [])
	monkeypatch.setattr(frappe, "logger", lambda name=None: logger, raising=False)
	monkeypatch.setattr(frappe, "session", types.SimpleNamespace(user="u"), raising=False)
	monkeypatch.setattr(hc, "session", types.SimpleNamespace(register_recording=lambda *a, **k: False))
	monkeypatch.setattr(hc, "_discard_dropped_recording", lambda *a, **k: None)
	monkeypatch.setattr(hc, "_dump_capture_state_to_redis", lambda **k: None)

	def run(recording_uuid):
		frappe.local = types.SimpleNamespace(
			site="s1", optimus_session_id="s-1", _recorder=types.SimpleNamespace(uuid=recording_uuid)
		)
		hc.after_job()

	return run, levels


def test_a_cap_hit_writes_one_error_level_line_for_many_recordings(monkeypatch):
	run, levels = _cap_hit_env(monkeypatch)
	for i in range(5):
		run(f"rec-{i}")
	assert len(levels["error"]) == 1 and "cap hit" in levels["error"][0]
	assert levels["warning"] == []  # Frappe drops warnings on a production site


def test_the_janitor_cap_line_is_keyed_without_the_backlog_count(monkeypatch):
	from optimus import janitor
	from optimus.tests.test_boundary_pins import _sweep

	keys = []
	_sweep(monkeypatch, 100, lambda message, **kw: keys.append(kw.get("dedupe_key")))
	assert keys == ["janitor deletion cap hit"]


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

	transaction_writes = 1

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


def test_a_migrate_with_nothing_written_commits_nothing(seed_env):
	store, db, logged, mp = seed_env
	order = []
	mp.setattr(install, "safe_commit", lambda: order.append("commit"))
	mp.setattr(store, "seed_control", lambda: None)
	mp.setattr(store, "ensure_indexes", lambda: order.append("indexes"))
	db.transaction_writes = 0
	install._seed_ai_refresh_store()
	assert order == ["indexes"]


def test_the_patch_never_raises_into_the_migrate(seed_env):
	store, db, logged, mp = seed_env
	mp.setattr(store, "seed_control", _raise(RuntimeError("insert failed")))
	mp.setattr(store, "ensure_indexes", lambda: None)
	store.execute()
	mp.setattr(store, "seed_control", lambda: None)
	mp.setattr(store, "ensure_indexes", _raise(RuntimeError("CREATE INDEX failed")))
	store.execute()
	assert logged == ["optimus ai refresh store seed", "optimus ai refresh store indexes"]


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


def _status_patch(monkeypatch, table_exists):
	patch = import_module("optimus.patches.v0_12_0.initialize_ai_refresh_store")
	created = []
	db = types.SimpleNamespace(
		db_type="postgres",
		db_schema="public",
		sql=lambda q, v: [],
		table_exists=table_exists,
		add_index=lambda doctype, columns, index_name: created.append(doctype),
	)
	status = types.SimpleNamespace(fieldname="status", search_index=1, unique=0)
	monkeypatch.setattr(
		patch,
		"frappe",
		types.SimpleNamespace(
			db=db,
			get_meta=lambda d: types.SimpleNamespace(fields=[status] if "Phase Two" in d else []),
		),
	)
	return patch, created


def test_a_missing_phase_two_run_table_is_skipped(monkeypatch):
	patch, created = _status_patch(monkeypatch, lambda doctype: False)
	patch.ensure_indexes()
	assert created == []
	patch, created = _status_patch(monkeypatch, lambda doctype: True)
	patch.ensure_indexes()
	assert created == ["Optimus Phase Two Run"]
