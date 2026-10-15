"""Reported zero differs from missing usage; explicit jobs own their accounting."""

from types import SimpleNamespace

import pytest

from optimus import ai_fix
from optimus.tests.test_ai_reliability_core import Reply


@pytest.fixture
def provider(monkeypatch):
	monkeypatch.setattr(ai_fix, "_current_key_or_empty", lambda: "")
	monkeypatch.setattr(ai_fix, "_get_api_key", lambda *a, **kw: "")
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
		# T8: Anthropic sends a cache field it did not use as null; that is no missing count.
		(
			{
				"input_tokens": 4,
				"output_tokens": 3,
				"cache_creation_input_tokens": None,
				"cache_read_input_tokens": None,
			},
			True,
			7,
		),
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


def test_no_completion_attempted_is_a_known_zero():
	"""C3: a refusal before any HTTP call cost nothing, which is known."""
	usage = ai_fix.Usage()
	assert usage.complete is True and usage.calls == 0
	usage.begin()
	assert usage.complete is False


def test_a_refusal_before_sending_reports_a_known_zero_through_usage_out(provider, monkeypatch):
	"""C3: the caller's own Usage proves nothing was sent (no call begun). The error alone
	proves nothing, so on its own it stays unknown usage (F2)."""

	def refuse(*a, **kw):
		raise ai_fix.AiFixError("fake: the prompt does not fit", kind="context")

	sent = []
	monkeypatch.setattr(ai_fix, "_check_context_fits", refuse)
	monkeypatch.setattr(ai_fix.requests, "post", lambda *a, **kw: sent.append(True))
	usage = ai_fix.Usage()
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix.suggest_fix({"finding_type": "N+1 Query"}, session_uuid="fake-session", usage_out=usage)
	assert not sent and usage.calls == 0 and usage.complete is True
	assert caught.value.usage is None and caught.value.usage_complete is False


def test_an_error_without_a_call_count_is_unknown_usage(monkeypatch):
	"""F2: the HTTP layer builds its failures before any boundary has counted the calls; a
	known zero at birth let a sent, failed call reach the journal as complete usage."""
	import frappe

	monkeypatch.setattr(frappe, "_", lambda text, *a, **kw: text)
	assert ai_fix._timeout_failure(10).usage_complete is False
	assert ai_fix.AiFixError("fake server error", kind="server").usage_complete is False


@pytest.mark.parametrize("failure", [None, "timeout"])
def test_usage_out_counts_every_call_suggest_fix_attempts(provider, monkeypatch, failure):
	"""The additive ``usage_out`` keyword hands the call's own Usage to the background engine,
	which wraps the call in its own boundary (C3)."""
	import requests

	provider.install({"total_tokens": 7})
	monkeypatch.setattr(ai_fix, "_reask_enabled", lambda: False)  # one call, counted once
	if failure:

		def timed_out(*a, **kw):
			raise requests.exceptions.Timeout("fake timeout")

		monkeypatch.setattr(ai_fix.requests, "post", timed_out)
		monkeypatch.setattr(ai_fix, "_log_http_error", lambda *a, **kw: None)
	usage = ai_fix.Usage()
	if failure:
		with pytest.raises(ai_fix.AiFixError):
			ai_fix.suggest_fix({"finding_type": "N+1 Query"}, session_uuid="fake-session", usage_out=usage)
		assert usage.calls == 1 and usage.complete is False
	else:
		result = ai_fix.suggest_fix(
			{"finding_type": "N+1 Query"}, session_uuid="fake-session", usage_out=usage
		)
		assert usage.calls == 1 and usage.complete is True and usage["total_tokens"] == 7
		assert result["tokens"] is usage and result["usage_complete"] is True


def test_a_refusal_inside_the_call_boundary_before_sending_reports_a_known_zero(provider, monkeypatch):
	def refuse(*a, **kw):
		raise ai_fix.AiFixError("fake: budget spent before sending", kind="timeout")

	monkeypatch.setattr(ai_fix, "_dispatch_call", refuse)
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix.suggest_fix({"finding_type": "N+1 Query"}, session_uuid="fake-session")
	assert caught.value.usage_complete is True
