# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Optimus Settings reads which providers need an API key from ai_fix._PROVIDER_DEFAULTS, so
adding a provider stays data-only (K15). Covers the owner's LAN Ollama setup: an
OpenAI-compatible endpoint with no key must not trigger the "API Key missing" warning."""

import pytest

from optimus import ai_fix
from optimus.tests.test_profiler_settings_validation import _fresh_controller


@pytest.mark.parametrize("name", sorted(ai_fix._PROVIDER_DEFAULTS))
def test_provider_needs_key_reads_the_provider_table(name):
	assert ai_fix.provider_needs_key(name) is bool(ai_fix._PROVIDER_DEFAULTS[name]["needs_key"])


def test_provider_needs_key_strips_and_defaults_to_true():
	assert ai_fix.provider_needs_key("  OpenAI-compatible  ") is False
	assert ai_fix.provider_needs_key("No Such Provider") is True
	assert ai_fix.provider_needs_key("") is True


def _warnings(monkeypatch, **fields):
	OptimusSettings, stub = _fresh_controller(monkeypatch)
	values = {"ai_enabled": 1, "ai_api_key": "", "ai_base_url": "", "ai_model": ""}
	values.update(fields)
	OptimusSettings(**values)._warn_on_incomplete_ai_config()
	return stub.msgprint_calls


def test_hosted_provider_without_a_key_warns(monkeypatch):
	calls = _warnings(monkeypatch, ai_provider="OpenAI")
	assert len(calls) == 1 and "API Key" in calls[0]["msg"]


def test_keyless_local_endpoint_is_not_asked_for_a_key(monkeypatch):
	calls = _warnings(
		monkeypatch, ai_provider="OpenAI-compatible",
		ai_base_url="http://10.0.0.5:11434/v1", ai_model="qwen3-coder:30b",
	)
	assert calls == []


def test_needs_key_follows_the_table_not_a_hard_coded_name(monkeypatch):
	monkeypatch.setitem(
		ai_fix._PROVIDER_DEFAULTS, "Test Hosted Keyless",
		{"protocol": "openai", "base_url": "https://llm.example.com/v1", "model": "m", "needs_key": False},
	)
	assert _warnings(monkeypatch, ai_provider="Test Hosted Keyless") == []


# ---- subscription (local CLI) settings -------------------------------------


def test_subscription_mode_needs_no_api_key_model_or_url(monkeypatch):
	calls = _warnings(monkeypatch, ai_provider="OpenAI", ai_connection_type="Subscription (local CLI)")
	assert calls == []


def test_api_key_mode_still_warns_when_the_connection_type_is_set(monkeypatch):
	calls = _warnings(monkeypatch, ai_provider="OpenAI", ai_connection_type="API Key")
	assert len(calls) == 1 and "API Key" in calls[0]["msg"]


def _resolved(monkeypatch, row):
	from optimus import settings

	monkeypatch.setattr(settings, "_site_conf_fallback", lambda k: None)
	return settings._resolve(row=row)


def test_resolve_defaults_the_cli_fields_to_api_key_mode(monkeypatch):
	cfg = _resolved(monkeypatch, {})
	assert (cfg.ai_connection_type, cfg.ai_subscription_cli, cfg.ai_cli_path) == ("API Key", "Codex CLI", "")


def test_resolve_takes_the_cli_fields_from_the_row(monkeypatch):
	cfg = _resolved(monkeypatch, {
		"ai_connection_type": "Subscription (local CLI)", "ai_subscription_cli": "Codex CLI",
		"ai_cli_path": "/usr/local/bin/codex",
	})
	assert cfg.ai_connection_type == "Subscription (local CLI)" and cfg.ai_cli_path == "/usr/local/bin/codex"


def test_resolve_falls_back_to_the_defaults_for_blank_cli_fields(monkeypatch):
	cfg = _resolved(monkeypatch, {"ai_connection_type": None, "ai_subscription_cli": None, "ai_cli_path": None})
	assert (cfg.ai_connection_type, cfg.ai_cli_path) == ("API Key", "")


def test_the_doctype_row_reader_strips_the_cli_fields(monkeypatch):
	import frappe

	from optimus import settings

	class Doc(dict):
		pass

	doc = Doc({"ai_connection_type": " Subscription (local CLI) ", "ai_subscription_cli": "", "ai_cli_path": " /x/codex "})
	monkeypatch.setattr(frappe, "db", type("Db", (), {"exists": staticmethod(lambda *a, **k: True)})(), raising=False)
	monkeypatch.setattr(frappe, "get_doc", lambda *a, **k: doc, raising=False)
	row = settings._read_doctype_row()
	assert row["ai_connection_type"] == "Subscription (local CLI)"
	assert row["ai_subscription_cli"] is None and row["ai_cli_path"] == "/x/codex"
