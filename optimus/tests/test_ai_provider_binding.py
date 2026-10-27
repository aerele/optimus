"""A saved endpoint and its credential must be read from one SQL snapshot."""

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from optimus import ai_fix, settings

pytestmark = pytest.mark.rq


def config(**overrides):
	return settings.OptimusConfig(ai_enabled=True, ai_provider="OpenAI-compatible",
		ai_base_url=overrides.pop("ai_base_url", "https://first.invalid/v1"), ai_model=overrides.pop("ai_model", "fake"), **overrides)


@pytest.fixture
def bound(monkeypatch):
	import frappe.utils.password as password

	cached = config()
	monkeypatch.setattr(settings, "get_config", lambda: cached)
	provider = ai_fix._provider_config()
	state = {"cfg": cached, "ciphertext": "fake encrypted value", "reads": 0, "decrypts": 0, "calls": []}
	def snapshot():
		state["reads"] += 1
		return state["cfg"], state["ciphertext"]
	def decrypt(*a, **kw):
		state["decrypts"] += 1
		return "FakeCredential" * 3
	monkeypatch.setattr(ai_fix, "_read_provider_snapshot", snapshot, raising=False)
	monkeypatch.setattr(password, "decrypt", decrypt, raising=False)
	monkeypatch.setattr(password, "get_decrypted_password", decrypt)
	monkeypatch.setattr(ai_fix, "_call_openai_chat", lambda *a, **kw: state["calls"].append(a[0]) or "OK")
	return provider, state


@pytest.mark.parametrize("change", ["endpoint", "path_case", "model", "context", "disabled", "provider"])
def test_changed_config_cannot_receive_newly_saved_key(bound, change):
	provider, state = bound
	fresh = SimpleNamespace(**asdict(config()))
	if change == "endpoint":
		fresh.ai_base_url = "https://second.invalid/v1"
	elif change == "path_case":
		fresh.ai_base_url = "https://first.invalid/V1"
	elif change == "model":
		fresh.ai_model = "different"
	elif change == "context":
		fresh.ai_context_tokens = 1234
	elif change == "disabled":
		fresh.ai_enabled = False
	else:
		fresh.ai_provider = "OpenAI"
	state["cfg"] = fresh
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._dispatch_call(provider, "private prompt", [], usage_out={})
	assert caught.value.kind == "config"
	assert not state["calls"] and state["decrypts"] == 0


def test_matching_snapshot_decrypts_once_per_call(bound):
	provider, state = bound
	assert ai_fix._dispatch_call(provider, "private prompt", [], usage_out={}) == "OK"
	assert state["reads"] == state["decrypts"] == 1
	assert state["calls"] == [provider["base_url"]]


def test_unreadable_snapshot_never_falls_back_to_unbound_key(bound, monkeypatch):
	provider, state = bound
	def fail():
		raise RuntimeError("fake private details")
	monkeypatch.setattr(ai_fix, "_read_provider_snapshot", fail)
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._dispatch_call(provider, "private prompt", [], usage_out={})
	assert caught.value.kind == "config"
	assert "fake private details" not in str(caught.value)
	assert not state["calls"] and not state["decrypts"]


def test_snapshot_interrupt_is_fresh_and_never_calls_provider(bound, monkeypatch):
	provider, state = bound
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	interrupt = Timeout("fake timeout")
	def fail():
		raise interrupt
	monkeypatch.setattr(ai_fix, "_read_provider_snapshot", fail)
	with pytest.raises(Timeout) as caught:
		ai_fix._dispatch_call(provider, "private prompt", [], usage_out={})
	assert caught.value is not interrupt
	assert not state["calls"]


@pytest.mark.parametrize("backend", ["mariadb", "postgres"])
@pytest.mark.parametrize("stored", [False, True])
def test_snapshot_selects_only_the_bound_encrypted_password(monkeypatch, backend, stored):
	import sqlite3

	import frappe

	with sqlite3.connect(":memory:") as db:
		db.execute('CREATE TABLE "tabSingles" (doctype TEXT, field TEXT, value TEXT)')
		db.execute('CREATE TABLE "__Auth" (doctype TEXT, name TEXT, fieldname TEXT, password TEXT, encrypted INTEGER)')
		values = {"ai_enabled": "1", "ai_provider": "OpenAI-compatible", "ai_base_url": "https://first.invalid/v1",
			"ai_model": "fake", "ai_context_tokens": "32768", "unrelated_private_field": "omit"}
		db.executemany('INSERT INTO "tabSingles" VALUES (?, ?, ?)', [("Optimus Settings", field, value) for field, value in values.items()])
		db.execute('INSERT INTO "tabSingles" VALUES (?, ?, ?)', ("Other Settings", "ai_provider", "omit"))
		for doctype, name, field, encrypted in [("Other", "Optimus Settings", "ai_api_key", 1),
			("Optimus Settings", "Other", "ai_api_key", 1), ("Optimus Settings", "Optimus Settings", "other", 1),
			("Optimus Settings", "Optimus Settings", "ai_api_key", 0)]:
			db.execute('INSERT INTO "__Auth" VALUES (?, ?, ?, ?, ?)', (doctype, name, field, "omit", encrypted))
		if stored:
			db.execute('INSERT INTO "__Auth" VALUES (?, ?, ?, ?, ?)', ("Optimus Settings", "Optimus Settings", "ai_api_key", "wanted ciphertext", 1))
		queries = []
		def multisql(queries_by_backend, values):
			queries.append(queries_by_backend[backend])
			return db.execute(queries[-1].replace("%s", "?"), values).fetchall()
		monkeypatch.setattr(frappe, "db", SimpleNamespace(multisql=multisql), raising=False)
		cfg, ciphertext = ai_fix._read_provider_snapshot()
		assert vars(cfg) == {**{key: value for key, value in values.items() if key != "unrelated_private_field"}, "ai_enabled": True}
		assert ciphertext == ("wanted ciphertext" if stored else None)
		assert len(queries) == 1


@pytest.mark.parametrize("needs_key", [False, True])
def test_unreadable_bound_key_retains_keyless_provider_behavior(bound, monkeypatch, needs_key):
	provider, state = bound
	if needs_key:
		state["cfg"] = settings.OptimusConfig(ai_enabled=True, ai_provider="OpenAI")
		provider = ai_fix._provider_config(state["cfg"])
	def fail(*a, **kw):
		raise ValueError("fake decryption failure")
	monkeypatch.setattr("frappe.utils.password.decrypt", fail)
	if needs_key:
		with pytest.raises(ai_fix.AiFixError) as caught:
			ai_fix._dispatch_call(provider, "", [], usage_out={})
		assert caught.value.kind == "config" and not state["calls"]
	else:
		assert ai_fix._dispatch_call(provider, "", [], usage_out={}) == "OK"
