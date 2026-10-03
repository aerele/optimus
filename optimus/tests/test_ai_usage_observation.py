"""Reported zero differs from missing usage; explicit jobs own their accounting."""

from types import SimpleNamespace

import pytest

from optimus import ai_fix
from optimus.tests.test_ai_reliability_core import Reply


@pytest.fixture
def provider(monkeypatch):
	monkeypatch.setattr(ai_fix, "_current_key_or_empty", lambda: "")
	monkeypatch.setattr(ai_fix, "_get_api_key", lambda *a: "")
	monkeypatch.setattr(
		ai_fix,
		"_provider_config",
		lambda: {
			"name": "fake",
			"protocol": "openai",
			"model": "fake",
			"base_url": "https://fake.invalid/v1",
			"needs_key": False,
			"context_tokens": 128000,
		},
	)
	monkeypatch.setattr(ai_fix, "_resolve_display_threshold_ms", lambda: 1000)
	monkeypatch.setattr(ai_fix, "is_finding_type_excluded", lambda *a: False)
	spend = []
	import frappe
	monkeypatch.setattr(frappe, "db", SimpleNamespace(sql=lambda *a, **kw: spend.append(True)))

	def install(usage, *, content="answer", protocol="openai"):
		body = (
			{"choices": [{"message": {"content": content}}]}
			if protocol == "openai"
			else {"content": [{"type": "text", "text": content}]}
		)
		if usage is not None:
			body["usage"] = usage
		monkeypatch.setattr(ai_fix.requests, "post", lambda *a, **kw: Reply(payload=body))

	return SimpleNamespace(install=install, spend=spend)


@pytest.mark.parametrize(
	"raw,known,total",
	[
		(None, False, 0),
		({}, False, 0),
		({"total_tokens": 0}, True, 0),
		({"prompt_tokens": 4, "completion_tokens": 3}, True, 7),
		({"prompt_tokens": 4}, False, 4),
		({"total_tokens": "bad"}, False, 0),
		({"total_tokens": True}, False, 0),
		({"total_tokens": 1.5}, False, 1),
		({"total_tokens": "7"}, True, 7),
		({"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 2}, False, 7),
		({"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 0}, False, 7),
		({"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 9}, True, 9),
	],
)
def test_openai_usage_presence_is_not_inferred_from_normalized_zero(provider, raw, known, total):
	provider.install(raw)
	usage = ai_fix.Usage()
	ai_fix._call_openai_chat(
		"https://fake.invalid/v1", "", "fake", "system", [], usage_out=usage, session_uuid="fake-session"
	)
	assert usage.complete is known and usage.calls == 1
	assert usage["total_tokens"] == total
	assert provider.spend == []


@pytest.mark.parametrize(
	"raw,known,total",
	[
		(None, False, 0),
		({"input_tokens": 0, "output_tokens": 0}, True, 0),
		({"input_tokens": 4, "output_tokens": 3, "cache_read_input_tokens": 10}, True, 17),
		({"input_tokens": 4, "output_tokens": 3, "cache_read_input_tokens": "bad"}, False, 7),
	],
)
def test_anthropic_usage_accounts_for_cache_components(provider, raw, known, total):
	provider.install(raw, protocol="anthropic")
	usage = ai_fix.Usage()
	ai_fix._call_anthropic(
		"https://fake.invalid", "", "fake", "system", [], usage_out=usage, session_uuid="fake-session"
	)
	assert usage.complete is known and usage["total_tokens"] == total
	assert provider.spend == []


def test_unattributed_call_collects_usage_without_ambient_spending(provider):
	provider.install({"total_tokens": 7})
	usage = ai_fix.Usage()
	ai_fix._call_openai_chat("https://fake.invalid/v1", "", "fake", "system", [], usage_out=usage)
	assert usage["total_tokens"] == 7 and provider.spend == []


def test_known_zero_survives_public_result(provider):
	provider.install({"total_tokens": 0})
	result = ai_fix.suggest_fix({"finding_type": "N+1 Query"}, session_uuid="fake-session")
	assert result["tokens"]["total_tokens"] == 0 and result["usage_complete"] is True


def test_response_processing_failure_preserves_usage_completeness(provider, monkeypatch):
	provider.install({"total_tokens": 7})

	def fail(*a, **kw):
		raise ValueError("fake processing failure")

	monkeypatch.setattr(ai_fix.ai_guardrails, "verify_fix", fail)
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix.suggest_fix({"finding_type": "N+1 Query"}, session_uuid="fake-session")
	assert caught.value.usage["total_tokens"] == 7 and caught.value.usage_complete is True


def test_transport_failure_does_not_claim_known_zero(provider, monkeypatch):
	import requests

	def fail(*a, **kw):
		raise requests.exceptions.Timeout("fake timeout")

	monkeypatch.setattr(ai_fix.requests, "post", fail)
	monkeypatch.setattr(ai_fix, "_log_http_error", lambda *a, **kw: None)
	usage = ai_fix.Usage()
	with pytest.raises(ai_fix.AiFixError):
		ai_fix.humanize_steps([{"label": "fake"}], usage_out=usage, session_uuid="fake-session")
	assert usage.complete is False and usage.calls == 1


@pytest.mark.parametrize("sent", [False, True])
def test_failed_reask_preserves_first_spend_and_distinguishes_pre_send_failure(provider, monkeypatch, sent):
	from optimus import ai_guardrails

	monkeypatch.setattr(
		ai_fix.ai_guardrails, "verify_fix", lambda *a, **kw: [ai_guardrails.Violation("raw-sql")]
	)
	monkeypatch.setattr(ai_fix, "_reask_enabled", lambda: True)
	monkeypatch.setattr(ai_fix, "_log_reask", lambda *a, **kw: None)
	calls = []

	def dispatch(*a, usage_out, **kw):
		calls.append(True)
		if len(calls) == 1:
			usage_out.begin()
			usage_out.observe(True)
			usage_out.update(prompt_tokens=4, completion_tokens=3, total_tokens=7)
			return "fake unsafe answer"
		if sent:
			usage_out.begin()
		raise ai_fix.AiFixError("fake failure", kind="timeout" if sent else "config")

	monkeypatch.setattr(ai_fix, "_dispatch_call", dispatch)
	result = ai_fix.suggest_fix({"finding_type": "N+1 Query"}, session_uuid="fake-session")
	assert len(calls) == 2 and result["tokens"]["total_tokens"] == 7
	assert result["usage_complete"] is (not sent)
