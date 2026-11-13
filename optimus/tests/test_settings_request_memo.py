# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Settings are read once per request or job, never once per recorded query."""

import sys
import types
from collections import defaultdict

import pytest

import optimus
from optimus import redis_keys, redis_schema, settings

KEY = redis_keys.settings_cache()


def fresh_local():
	"""What frappe.init gives each request and job."""
	return types.SimpleNamespace(request_cache=defaultdict(dict))


class Env:
	"""A fake Frappe with a counting cache, a DB row and a request-local namespace."""

	def __init__(self):
		self.redis = {}
		self.gets = []
		self.sets = []
		self.row_reads = 0
		self.row = {"enabled": 1}
		self.row_error = None
		self.transaction_writes = 0
		self.get_error = None
		self.local = fresh_local()
		self.db = types.SimpleNamespace(transaction_writes=0)

	def get_value(self, key, expires=False):
		self.gets.append(key)
		if self.get_error:
			raise self.get_error
		return self.redis.get(key)

	def set_value(self, key, value, expires_in_sec=None):
		self.sets.append(key)
		self.redis[key] = value

	def delete_value(self, key):
		self.redis.pop(key, None)

	def read_row(self):
		self.row_reads += 1
		if self.row_error:
			raise self.row_error
		return dict(self.row)

	def memoized(self):
		return bool(self.local.request_cache.get(settings._MEMO_KEY))

	def new_request(self):
		self.local = fresh_local()
		frappe = sys.modules["frappe"]
		frappe.local = self.local

	def settings_gets(self):
		return self.gets.count(KEY)


@pytest.fixture
def env(monkeypatch):
	state = Env()
	frappe = sys.modules["frappe"]
	monkeypatch.setattr(
		frappe,
		"cache",
		types.SimpleNamespace(
			get_value=state.get_value, set_value=state.set_value, delete_value=state.delete_value
		),
		raising=False,
	)
	monkeypatch.setattr(frappe, "local", state.local, raising=False)
	monkeypatch.setattr(frappe, "db", state.db, raising=False)
	monkeypatch.setattr(settings, "_read_doctype_row", state.read_row)
	return state


def test_fifty_calls_in_one_request_make_one_redis_read(env):
	for _ in range(50):
		assert settings.get_config().enabled
	assert env.settings_gets() == 1
	assert env.row_reads == 1


def test_a_redis_hit_is_memoized_without_touching_the_database(env):
	env.redis[KEY] = redis_schema.wrap_value(
		settings._resolve(row={"enabled": 0}).__dict__, version=settings.CONFIG_CACHE_ENVELOPE_VERSION
	)
	for _ in range(5):
		assert not settings.get_config().enabled
	assert env.settings_gets() == 1 and env.row_reads == 0


def test_a_new_request_reads_redis_again(env):
	settings.get_config()
	env.new_request()
	settings.get_config()
	assert env.settings_gets() == 2


def test_invalidate_config_forces_one_fresh_read_and_one_database_read(env):
	settings.get_config()
	env.row["enabled"] = 0
	settings.invalidate_config()
	assert not settings.get_config().enabled
	assert env.settings_gets() == 2 and env.row_reads == 2


def test_invalidate_config_clears_the_memo_even_if_the_redis_delete_fails(env, monkeypatch):
	settings.get_config()
	assert env.memoized()

	def fail(key):
		raise RuntimeError("redis down")

	monkeypatch.setattr(sys.modules["frappe"].cache, "delete_value", fail)
	with pytest.raises(RuntimeError):
		settings.invalidate_config()
	assert not env.memoized()


def test_a_first_read_that_raises_is_retried_and_not_memoized(env):
	env.get_error = RuntimeError("blip")
	env.row_error = RuntimeError("blip")
	assert settings.get_config().sensitive_sql_columns == ()
	assert not env.memoized()
	env.get_error = env.row_error = None
	env.row["sensitive_sql_columns"] = ["secret_col"]
	assert settings.get_config().sensitive_sql_columns == ("secret_col",)


def test_a_failed_resolve_returns_defaults_and_is_not_memoized(env, monkeypatch):
	monkeypatch.setattr(settings, "_resolve", lambda row=None: 1 / 0)
	assert settings.get_config().sensitive_sql_columns == ()
	assert not env.memoized()


def test_a_config_whose_redis_write_failed_is_not_memoized(env, monkeypatch):
	def fail(key, value, expires_in_sec=None):
		raise RuntimeError("redis down")

	monkeypatch.setattr(sys.modules["frappe"].cache, "set_value", fail)
	assert settings.get_config().enabled
	assert not env.memoized()


def test_settings_read_during_a_pending_write_are_not_memoized(env):
	env.db.transaction_writes = 1
	env.row["enabled"] = 0
	assert not settings.get_config().enabled
	assert KEY not in env.redis and not env.memoized()
	env.db.transaction_writes = 0
	env.row["enabled"] = 1
	assert settings.get_config().enabled


def test_a_config_written_to_redis_is_memoized(env):
	settings.get_config()
	assert KEY in env.redis and env.memoized()


def test_an_unbound_frappe_local_still_returns_a_config(env, monkeypatch):
	class Unbound:
		def __getattr__(self, name):
			raise RuntimeError("Working outside of application context")

		def __setattr__(self, name, value):
			raise RuntimeError("Working outside of application context")

	monkeypatch.setattr(sys.modules["frappe"], "local", Unbound(), raising=False)
	assert settings.get_config().enabled
	assert settings.get_config().enabled


def test_a_frappe_without_local_still_returns_a_config(env, monkeypatch):
	monkeypatch.delattr(sys.modules["frappe"], "local")
	assert settings.get_config().enabled


class FakeRecorder:
	def __init__(self, *args, **kwargs):
		self.calls = []
		self.form_dict = {"cmd": "x"}
		self.headers = {}

	def register(self, data):
		self.calls.append(data)


def test_recorder_init_and_a_thousand_queries_make_at_most_one_settings_read(env, monkeypatch):
	fake_module = types.ModuleType("frappe.recorder")
	fake_module.Recorder = type("Recorder", (FakeRecorder,), {})
	monkeypatch.setitem(sys.modules, "frappe.recorder", fake_module)
	monkeypatch.setattr(sys.modules["frappe"], "recorder", fake_module, raising=False)
	optimus._patch_recorder()
	recorder = fake_module.Recorder()
	for i in range(1000):
		recorder.register({"query": f"select {i}"})
	assert env.settings_gets() <= 1
	assert len(recorder.calls) == 1000
