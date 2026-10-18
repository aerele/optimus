"""Regression evidence: paid text survives local handling without silent retries."""

import pytest

from optimus import ai_budget, ai_fix, ai_prompts
from optimus.tests.test_ai_fix import TestGuardedCompletion as _Harness
from optimus.tests.test_ai_fix import _post_sequence


def test_complete_long_answer_survives(monkeypatch):
	h = _Harness()
	answer = h._GOOD + "\n" + "Compare the recorded query count before and after the change.\n" * 400
	out = h._run(_post_sequence(h._resp(answer, usage={"completion_tokens": 7000})), monkeypatch)
	assert out["suggestion"] == answer.strip()
	assert out["completion_status"] == "complete"
	assert out["received_responses"][0]["text"] == answer
	assert out["finish_reason"] == "stop"


@pytest.mark.parametrize("reason,status", [("length", "incomplete"), (None, "unknown"), ("content_filter", "blocked"), ("aborted", "incomplete")])
def test_noncomplete_response_is_retained_without_reask(monkeypatch, reason, status):
	h = _Harness()
	fake = _post_sequence(h._resp(h._RAW, finish=reason))
	out = h._run(fake, monkeypatch)
	assert len(fake.calls) == 1
	assert out["completion_status"] == status
	assert out["received_responses"][0]["text"] == h._RAW
	assert "```" not in out["suggestion"]
	assert ai_prompts.fix_state(out)[0] != "current"


def test_deepseek_uses_model_output_limit(monkeypatch):
	h = _Harness()
	provider = dict(h._PROVIDER, name="DeepSeek", model="deepseek-v4-flash", context_tokens=1000000)
	fake = _post_sequence(h._resp(h._GOOD))
	h._run(fake, monkeypatch, provider=provider)
	assert fake.calls[0].body["max_tokens"] == 393216


def test_budget_uses_remaining_context_and_margin():
	assert ai_budget.output_tokens(4096, input_tokens=3000, model_max=8192) == 796
	assert ai_budget.output_tokens(4096, input_tokens=4000, model_max=8192) == 0
	assert ai_budget.output_tokens(1000000, input_tokens=5000, model_max=32768) == 32768


def test_reasoning_count_is_preserved_without_double_counting(monkeypatch):
	h = _Harness()
	usage = {"prompt_tokens": 100, "completion_tokens": 150, "total_tokens": 250,
	         "completion_tokens_details": {"reasoning_tokens": 75}}
	out = h._run(_post_sequence(h._resp(h._GOOD, usage=usage)), monkeypatch)
	assert out["tokens"]["total_tokens"] == 250
	assert out["received_responses"][0]["reasoning_tokens"] == 75


def test_reask_budget_uses_full_history_without_twice_first_answer_cap(monkeypatch):
	h = _Harness()
	fake = _post_sequence(h._resp(h._RAW, usage={"prompt_tokens": 2500, "completion_tokens": 150}), h._resp(h._GOOD))
	h._run(fake, monkeypatch)
	assert len(fake.calls) == 2
	assert fake.calls[1].body["max_tokens"] == 32768
	body = fake.calls[1].body
	assert ai_budget.input_tokens(body["messages"][0]["content"], body["messages"][1:]) + body["max_tokens"] < 128000


def test_unknown_hosted_model_uses_disclosed_server_default(monkeypatch):
	h = _Harness()
	fake = _post_sequence(h._resp(h._GOOD))
	out = h._run(fake, monkeypatch, provider=dict(h._PROVIDER, model="new-custom-model"))
	assert "max_tokens" not in fake.calls[0].body
	assert "max_completion_tokens" not in fake.calls[0].body
	assert out["received_responses"][0]["budget_source"] == "provider_default"
	assert out["received_responses"][0]["reasoning_tokens"] is None


def test_unknown_anthropic_model_requires_capabilities_before_http(monkeypatch):
	h = _Harness()
	fake = _post_sequence(h._resp(h._GOOD))
	with pytest.raises(ai_fix.AiFixError, match="model_limits"):
		h._run(fake, monkeypatch, provider=dict(h._PROVIDER, name="Anthropic", protocol="anthropic", model="custom-claude"))
	assert not fake.calls


def test_operator_capabilities_and_thinking_are_scoped(monkeypatch):
	from types import SimpleNamespace

	import frappe

	from optimus.tests.test_ai_fix import _cfg

	monkeypatch.setattr(frappe, "conf", {"optimus_ai_model_limits": {"DeepSeek": {"my-model": [32768, 8192]}},
	                                    "optimus_ai_thinking": {"DeepSeek": "enabled"}}, raising=False)
	provider = ai_fix._provider_config(_cfg(ai_provider="DeepSeek", ai_model="my-model"))
	assert (provider["context_tokens"], provider["max_output_tokens"]) == (32768, 8192)
	assert provider["request_options"] == {"thinking": {"type": "enabled"}}
	assert ai_fix._PROVIDER_DEFAULTS["DeepSeek"]["request_options"] == {"thinking": {"type": "disabled"}}
	assert not ai_fix._provider_config(_cfg(ai_provider="OpenAI"))["request_options"]
	monkeypatch.setattr(frappe, "conf", SimpleNamespace(get=lambda *a: {}))


def test_retention_is_bounded_and_keeps_identity(monkeypatch):
	from optimus import ai_responses

	h = _Harness()
	monkeypatch.setattr(ai_responses, "RETENTION_BYTES", 100)
	out = h._run(_post_sequence(h._resp(h._GOOD, finish="length")), monkeypatch)
	raw = out["received_responses"][0]
	assert raw["text"] == h._GOOD[:100] and raw["retention_truncated"]
	assert raw["received_chars"] == len(h._GOOD)
	assert raw["applied"] is False and raw["privacy_mode"] == "redacted"
	assert len(raw["prompt_fingerprint"]) == len(raw["text_sha256"]) == 64
	assert ai_responses.fingerprint("s", [{"content": "old source"}]) != ai_responses.fingerprint("s", [{"content": "new source"}])


def test_refusal_without_text_still_records_usage_and_blocked_status(monkeypatch):
	h = _Harness()
	response = h._resp(None, usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}, finish="content_filter")
	out = h._run(_post_sequence(response), monkeypatch)
	assert out["completion_status"] == "blocked"
	assert out["tokens"]["total_tokens"] == 110
	assert out["received_responses"][0]["text"] == ""


def test_both_guardrail_attempts_retained(monkeypatch):
	h = _Harness()
	out = h._run(_post_sequence(h._resp(h._RAW), h._resp(h._GOOD)), monkeypatch)
	assert [r["text"] for r in out["received_responses"]] == [h._RAW, h._GOOD]
	assert out["suggestion"] == h._GOOD


def test_long_code_still_checked_and_never_rendered_as_html(monkeypatch):
	from optimus.renderer.finding_enrichment import _markdown_to_safe_html

	h = _Harness()
	padding = "\n" + "+# longer explanation of the change\n" * 600
	answer = h._GOOD.replace("+emails = ", padding + "+emails = ")
	out = h._run(_post_sequence(h._resp(answer)), monkeypatch)
	assert out["suggestion"] == answer
	assert out["completion_status"] == "complete"
	assert "longer explanation" in _markdown_to_safe_html(out["suggestion"])
	hostile = answer + "\n<script>alert('oops')</script>"
	html = _markdown_to_safe_html(hostile)
	assert "<script>" not in html and "&lt;script&gt;" in html
