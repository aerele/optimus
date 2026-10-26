# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Optimus Settings: AI config warning, validate and cache reset."""

import types

import pytest

from optimus.tests.test_profiler_settings_validation import _fresh_controller, _row


@pytest.fixture
def make_doc(monkeypatch):
	cls, stub = _fresh_controller(monkeypatch)

	def _make(**fields):
		doc = cls()
		for key, value in fields.items():
			setattr(doc, key, value)
		return doc

	return _make, stub


class TestIncompleteAiConfigWarning:
	def test_silent_when_ai_is_disabled(self, make_doc):
		make, stub = make_doc
		make(ai_enabled=0, ai_provider="Anthropic", ai_api_key="")._warn_on_incomplete_ai_config()

		assert stub.msgprint_calls == []

	def test_silent_when_provider_has_its_key(self, make_doc):
		make, stub = make_doc
		make(ai_enabled=1, ai_provider="Anthropic", ai_api_key="sk-x")._warn_on_incomplete_ai_config()

		assert stub.msgprint_calls == []

	def test_missing_key_is_reported_singular(self, make_doc):
		make, stub = make_doc
		make(ai_enabled=1, ai_provider="Anthropic", ai_api_key="  ")._warn_on_incomplete_ai_config()

		(call,) = stub.msgprint_calls
		assert "API Key is not set" in call["msg"]
		assert call["indicator"] == "orange"

	def test_provider_defaults_to_anthropic_when_unset(self, make_doc):
		make, stub = make_doc
		make(ai_enabled=1, ai_api_key="")._warn_on_incomplete_ai_config()

		assert "API Key" in stub.msgprint_calls[0]["msg"]

	def test_custom_provider_needs_base_url_and_model_but_not_a_key(self, make_doc):
		make, stub = make_doc
		make(
			ai_enabled=1, ai_provider="OpenAI-compatible", ai_base_url="", ai_model="", ai_api_key=""
		)._warn_on_incomplete_ai_config()

		(call,) = stub.msgprint_calls
		assert "Base URL, Model are not set" in call["msg"]
		assert "API Key" not in call["msg"]

	def test_fully_configured_custom_provider_is_silent(self, make_doc):
		make, stub = make_doc
		make(
			ai_enabled=1,
			ai_provider="OpenAI-compatible",
			ai_base_url="http://llm.local/v1",
			ai_model="qwen",
		)._warn_on_incomplete_ai_config()

		assert stub.msgprint_calls == []


class TestValidateAndOnUpdate:
	def test_validate_runs_every_check_in_one_save(self, make_doc):
		make, stub = make_doc
		doc = make(
			tracked_apps=[_row(" my_app "), _row("my_app"), _row(""), _row("frappe")],
			slow_query_threshold_ms=-5,
			ai_enabled=1,
			ai_provider="Anthropic",
			ai_api_key="",
		)

		doc.validate()

		assert [r.app_name for r in doc.tracked_apps] == ["my_app", "frappe"]
		assert doc.slow_query_threshold_ms == 1  # clamped to its floor
		titles = sorted(c["title"] for c in stub.msgprint_calls)
		assert titles == [
			"AI Fix Suggestions incomplete config",
			"Tracked Apps possible misconfiguration",
		]

	def test_on_update_drops_the_cached_settings(self, make_doc):
		make, stub = make_doc
		from optimus import redis_keys

		deleted = []
		stub.cache = types.SimpleNamespace(delete_value=deleted.append)

		make().on_update()

		assert deleted == [redis_keys.settings_cache()]
