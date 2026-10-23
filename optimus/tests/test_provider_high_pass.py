"""Captured wire requests and saved-answer behavior; no live provider calls."""

import pytest

from optimus import ai_fix, ai_prompts
from optimus.tests.test_ai_fix import TestGuardedCompletion as CompletionFixture
from optimus.tests.test_ai_fix import _cfg, _post_sequence


@pytest.mark.parametrize("model", ["claude-sonnet-4-6", "claude-opus-4-1", "custom-claude"])
def test_anthropic_model_and_probe_use_bounded_budget(monkeypatch, model):
	provider = ai_fix._provider_config(_cfg(ai_provider="Anthropic", ai_model=model))
	fake = _post_sequence(*[CompletionFixture._resp("OK") for _ in range(2)])
	# Exercise the protocol-specific Messages request, rather than replacing dispatch.
	import requests

	from optimus.tests.test_ai_fix import _FakeResp

	responses = iter(
		[
			_FakeResp(200, {"content": [{"type": "text", "text": "OK"}], "stop_reason": "end_turn"})
			for _ in range(2)
		]
	)

	def post(url, **kw):
		fake.calls.append(kw["json"])
		return next(responses)

	monkeypatch.setattr(requests, "post", post)
	monkeypatch.setattr(ai_fix, "_get_api_key", lambda *a, **k: "test-key")
	monkeypatch.setattr(ai_fix, "_provider_config", lambda: provider)
	assert ai_fix.test_connection()["ok"]
	assert (
		ai_fix._dispatch_call(provider, "system", [{"role": "user", "content": "hello"}], usage_out={})
		== "OK"
	)
	assert [b["max_tokens"] for b in fake.calls] == [4096, 4096]


@pytest.mark.parametrize("reason", [None, "eos", "COMPLETE", "vendor_complete"])
def test_valid_alternate_finish_is_current_without_reask(monkeypatch, reason):
	case = CompletionFixture()
	fake = _post_sequence(case._resp(case._GOOD, finish=reason))
	out = case._run(fake, monkeypatch)
	assert out["suggestion"] == case._GOOD
	assert out["completion_status"] == "complete"
	assert out["provider_finish_reason"] == reason
	assert out["received_responses"][0]["provider_finish_reason"] == reason
	assert ai_prompts.fix_state(out)[0] == "current"
	assert len(fake.calls) == 1


@pytest.mark.parametrize("reason", ["pause_turn", "tool_use", "tool_calls", "function_call", "abort"])
def test_explicit_partial_finish_is_withheld(monkeypatch, reason):
	case = CompletionFixture()
	fake = _post_sequence(case._resp(case._GOOD, finish=reason))
	out = case._run(fake, monkeypatch)
	assert "```" not in out["suggestion"]
	assert out["completion_status"] == "incomplete"
	assert len(fake.calls) == 1
	assert out["received_responses"][0]["text"] == case._GOOD


def test_missing_finish_at_budget_is_incomplete(monkeypatch):
	case = CompletionFixture()
	fake = _post_sequence(case._resp(case._GOOD, {"completion_tokens": 4096}, finish=None))
	out = case._run(fake, monkeypatch)
	assert out["completion_status"] == "incomplete"
	assert out["guardrail"]["fallback"]
	assert len(fake.calls) == 1


@pytest.mark.parametrize("raw", [False, 0, "False", "false", "0"])
def test_reask_false_spellings(monkeypatch, raw):
	import frappe

	monkeypatch.setattr(frappe, "conf", {"optimus_ai_reask": raw}, raising=False)
	assert ai_fix._reask_enabled() is False


def test_budget_setting_validation_and_capacity(monkeypatch):
	import frappe

	monkeypatch.setattr(frappe, "conf", {"optimus_ai_max_output_tokens": 3000}, raising=False)
	p = ai_fix._provider_config(_cfg(ai_provider="OpenAI"))
	assert ai_fix._output_tokens(p) == 3000
	assert ai_fix._output_tokens(dict(p, max_output_tokens=2000)) == 2000
	monkeypatch.setitem(frappe.conf, "optimus_ai_max_output_tokens", "False")
	with pytest.raises(ai_fix.AiFixError, match="optimus_ai_max_output_tokens"):
		ai_fix._provider_config(_cfg(ai_provider="OpenAI"))


def test_reasoning_budget_is_finite():
	p = {"name": "OpenAI", "model": "gpt-5", "context_tokens": 128000}
	assert ai_fix._output_tokens(p) == 8192


def test_empty_missing_finish_does_not_spend_a_reask(monkeypatch):
	case = CompletionFixture()
	fake = _post_sequence(case._resp("", finish=None))
	with pytest.raises(ai_fix.AiFixError, match="empty response"):
		case._run(fake, monkeypatch)
	assert len(fake.calls) == 1


def test_unclosed_answer_with_unknown_finish_is_withheld(monkeypatch):
	case = CompletionFixture()
	answer = case._GOOD.rsplit("```", 1)[0]
	fake = _post_sequence(case._resp(answer, finish=None))
	out = case._run(fake, monkeypatch)
	assert out["completion_status"] == "incomplete" and out["guardrail"]["fallback"]
	assert out["received_responses"][0]["text"] == answer
	assert len(fake.calls) == 1


def test_fluent_builder_answer_does_not_pay_for_a_reask(monkeypatch):
	case = CompletionFixture()
	case._FINDING = {
		"finding_type": "Hot Line", "title": "Repeated lookup",
		"source_window": [{"lineno": 11, "content": "old_work()", "is_target": True}],
	}
	answer = ("**Diagnosis**: Repeated lookup.\n**Fix**\n```diff\n-old_work()\n"
		"+q = frappe.qb.from_(T).select(T.name)\n+if enabled:\n+    q = q.where(T.enabled == 1)\n+q.run()\n```\n"
		"**Why it works**: Batches reads.\n**Verify**: Compare query counts.")
	fake = _post_sequence(case._resp(answer))
	out = case._run(fake, monkeypatch)
	assert len(fake.calls) == 1 and not out["guardrail"]["reasked"]
	assert not out["guardrail"]["fallback"] and "q = q.where" in out["suggestion"]
	assert ai_prompts.fix_state(out)[0] == "current"
