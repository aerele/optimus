# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Provider failure kinds, the parameter-retry ladder's rules and the failure
messages, with real provider error bodies and no network or site.

``AiFixError.fatal`` is the one answer to "does this need an operator before
another call": the background refresh's breaker reads it and keeps no list of
its own. A ``context`` failure is about one prompt, so it is not fatal."""

import json
from types import SimpleNamespace

import pytest

from optimus import ai_fix, ai_prompts

_LOCAL = "https://provider.invalid/v1/chat/completions"
_HOSTED = "https://api.openai.com/v1/chat/completions"


class Reply:
	def __init__(self, status=200, payload=None, headers=None):
		self.status_code = status
		self.payload = payload if payload is not None else {
			"choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
			"usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
		}
		self.text = json.dumps(self.payload)
		self.headers = headers or {}

	def json(self):
		return self.payload


def _rejected(message, status=400):
	return Reply(status, {"error": {"message": message, "type": "invalid_request_error"}})


def _answer(content):
	return Reply(payload={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})


@pytest.fixture
def wire(monkeypatch):
	posts, logs, clock = [], [], [1000.0]
	monkeypatch.setattr(ai_fix, "_current_key_or_empty", lambda: "")
	monkeypatch.setattr(ai_fix, "_log_http_error", lambda *a, **kw: logs.append((a, kw)))
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **kw: logs.append((a, kw)))
	monkeypatch.setattr(ai_fix, "_record_session_spend", lambda *_: None)

	def install(*replies, cost=0.0):
		"""Each post takes ``cost`` seconds on the fake clock; a ``(reply, seconds)`` pair overrides it."""
		pending = iter(replies)

		def post(url, **kw):
			posts.append((url, {**kw, "json": dict(kw["json"])}))
			reply = next(pending)
			reply, spent = reply if isinstance(reply, tuple) else (reply, cost)
			clock[0] += spent
			if isinstance(reply, BaseException):
				raise reply
			return reply

		monkeypatch.setattr(ai_fix.requests, "post", post)

	def use_clock():
		monkeypatch.setattr(ai_fix.time, "monotonic", lambda: clock[0])

	return SimpleNamespace(install=install, posts=posts, logs=logs, use_clock=use_clock)


@pytest.fixture
def marked(monkeypatch):
	"""``frappe._`` that marks the text it translates with a leading [t]."""
	import frappe

	monkeypatch.setattr(frappe, "_", lambda message, *a, **kw: "[t]" + message, raising=False)


def _fake_provider(monkeypatch, **overrides):
	provider = {
		"name": "fake", "protocol": "openai", "base_url": "https://fake.invalid/v1", "model": "fake",
		"needs_key": False, "context_tokens": 128000, **overrides,
	}
	monkeypatch.setattr(ai_fix, "_provider_config", lambda: dict(provider))
	monkeypatch.setattr(ai_fix, "_resolve_display_threshold_ms", lambda: 1000)
	monkeypatch.setattr(ai_fix, "_get_api_key", lambda *a: "")
	monkeypatch.setattr(ai_fix, "is_finding_type_excluded", lambda *a, **k: False)
	monkeypatch.setattr(ai_fix, "_reask_enabled", lambda: True)


# ---------------------------------------------------------------------------
# Kinds: real provider error bodies (rl72 c1corr s1_classify)
# ---------------------------------------------------------------------------

_REAL_BODIES = [
	# (label, status, body, kind, fatal)
	("openai 401 bad key", 401, {"error": {"message": "Incorrect API key provided: sk-abc. You can find your API key at https://platform.openai.com/account/api-keys.", "type": "invalid_request_error", "param": None, "code": "invalid_api_key"}}, "auth", True),
	("openai 429 rate", 429, {"error": {"message": "Rate limit reached for gpt-4.1-mini in organization org-x on tokens per min (TPM): Limit 200000, Used 199000, Requested 2000. Please try again in 300ms. Visit https://platform.openai.com/account/rate-limits to learn more.", "type": "tokens", "param": None, "code": "rate_limit_exceeded"}}, "rate_limited", False),
	("openai 429 quota", 429, {"error": {"message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, read the docs: https://platform.openai.com/docs/guides/error-codes/api-errors.", "type": "insufficient_quota", "param": None, "code": "insufficient_quota"}}, "quota", True),
	("openai 429 billing_not_active", 429, {"error": {"message": "Your account is not active, please check your billing details on our website.", "type": "billing_not_active", "param": None, "code": "billing_not_active"}}, "quota", True),
	("openai 400 context", 400, {"error": {"message": "This model's maximum context length is 128000 tokens. However, your messages resulted in 130000 tokens. Please reduce the length of the messages.", "type": "invalid_request_error", "param": "messages", "code": "context_length_exceeded"}}, "context", False),
	("openai 400 temperature", 400, {"error": {"message": "Unsupported value: 'temperature' does not support 0.1 with this model. Only the default (1) value is supported.", "type": "invalid_request_error", "param": "temperature", "code": "unsupported_value"}}, "bad_request", False),
	("openai 404 model", 404, {"error": {"message": "The model `gpt-9` does not exist or you do not have access to it.", "type": "invalid_request_error", "param": None, "code": "model_not_found"}}, "not_found", True),
	("anthropic 401", 401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}, "auth", True),
	("anthropic 429", 429, {"type": "error", "error": {"type": "rate_limit_error", "message": "Number of request tokens has exceeded your per-minute rate limit (https://docs.anthropic.com/en/api/rate-limits); see the response headers for current usage. Please reduce the prompt length or the maximum tokens requested, or try again later."}}, "rate_limited", False),
	("anthropic 400 credit", 400, {"type": "error", "error": {"type": "invalid_request_error", "message": "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits."}}, "quota", True),
	("anthropic 400 prompt too long", 400, {"type": "error", "error": {"type": "invalid_request_error", "message": "prompt is too long: 215000 tokens > 200000 maximum"}}, "context", False),
	("anthropic 400 exceed ctx limit", 400, {"type": "error", "error": {"type": "invalid_request_error", "message": "input length and `max_tokens` exceed context limit: 198000 + 21333 > 200000, decrease input length or `max_tokens` and try again"}}, "context", False),
	("anthropic 529", 529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}, "server", False),
	("azure 429", 429, {"error": {"code": "429", "message": "Requests to the ChatCompletions_Create Operation under Azure OpenAI API version 2024-02-01 have exceeded token rate limit of your current OpenAI S0 pricing tier. Please retry after 6 seconds. Please go here: https://aka.ms/oai/quotaincrease if you would like to further increase the default rate limit."}}, "rate_limited", False),
	("azure 404 deployment", 404, {"error": {"code": "DeploymentNotFound", "message": "The API deployment for this resource does not exist."}}, "not_found", True),
	("azure 400 content_filter", 400, {"error": {"message": "The response was filtered due to the prompt triggering Azure OpenAI's content management policy.", "type": None, "param": "prompt", "code": "content_filter", "status": 400}}, "bad_request", False),
	("moonshot 429 quota", 429, {"error": {"message": "Your account org-x<ak-y> is suspended, please check your plan and billing details", "type": "exceeded_current_quota_error"}}, "quota", True),
	("moonshot 429 token quota", 429, {"error": {"message": "You exceeded your current token quota: 0, please check your account balance", "type": "exceeded_current_quota_error"}}, "quota", True),
	("moonshot 429 rate", 429, {"error": {"message": "Your account org-x<ak-y> request reached organization max RPM: 3, please try again after 1 seconds", "type": "rate_limit_reached_error"}}, "rate_limited", False),
	("moonshot 400 token limit", 400, {"error": {"message": "Invalid request: Your request exceeded model token limit: 131072", "type": "invalid_request_error"}}, "context", False),
	("moonshot 400 temperature", 400, {"error": {"message": "invalid temperature: only 1 is allowed for this model", "type": "invalid_request_error"}}, "bad_request", False),
	("deepseek 402", 402, {"error": {"message": "Insufficient Balance", "type": "unknown_error", "param": None, "code": "invalid_request_error"}}, "quota", True),
	("deepseek 400 ctx", 400, {"error": {"message": "This model's maximum context length is 65536 tokens. However, you requested 70000 tokens (68000 in the messages, 2000 in the completion). Please reduce the length of the messages or completion.", "type": "invalid_request_error", "param": None, "code": "invalid_request_error"}}, "context", False),
	("ollama 404", 404, {"error": {"message": "model \"llama3\" not found, try pulling it first", "type": "api_error", "param": None, "code": None}}, "not_found", True),
	("llamacpp 400 ctx", 400, {"error": {"code": 400, "message": "the request exceeds the available context size, try increasing it", "type": "exceed_context_size_error"}}, "context", False),
	("vllm 400 ctx", 400, {"object": "error", "message": "This model's maximum context length is 4096 tokens. However, you requested 5000 tokens (3000 in the messages, 2000 in the completion).", "type": "BadRequestError", "param": None, "code": 400}, "context", False),
	("openrouter 402", 402, {"error": {"code": 402, "message": "This request requires more credits, or fewer max_tokens. You requested up to 2000 tokens, but can only afford 500."}}, "quota", True),
	("aerele 402 pack", 402, {"error": {"message": "Token pack exhausted. Top up.", "type": "pack_exhausted"}, "balance_tokens": 0}, "quota", True),
	("aerele 429", 429, {"error": {"message": "Rate limit exceeded: max 60 calls per minute per API key. Pace your calls.", "type": "rate_limit_exceeded"}}, "rate_limited", False),
]


@pytest.mark.parametrize("label,status,body,kind,fatal", _REAL_BODIES, ids=[case[0] for case in _REAL_BODIES])
def test_real_provider_bodies_get_their_kind(wire, label, status, body, kind, fatal):
	wire.install(Reply(status, body))
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._http_post(_LOCAL, {}, {}, provider="openai", where="test")
	assert (caught.value.kind, caught.value.fatal) == (kind, fatal)
	assert len(wire.posts) == len(wire.logs) == 1
	assert ".:" not in str(caught.value)
	if kind == "context":
		assert str(caught.value).startswith("The prompt did not fit the model's context window.")


@pytest.mark.parametrize("field,code", [("code", "context_length_exceeded"), ("type", "exceed_context_size_error")])
def test_a_context_error_code_alone_is_enough(wire, field, code):
	"""The reply shown is cut at 300 characters, so a long message hides a code
	that follows it: the validated machine code still says what happened."""
	wire.install(Reply(400, {"error": {"message": "Request rejected. " + "fake filler " * 40, field: code}}))
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._http_post(_LOCAL, {}, {}, provider="openai", where="test")
	assert caught.value.kind == "context"


@pytest.mark.parametrize("kind,fatal", [
	("auth", True), ("quota", True), ("not_found", True), ("config", True),
	("context", False), ("rate_limited", False), ("server", False), ("bad_request", False),
	("transport", False), ("timeout", False), ("bad_response", False), ("internal", False),
	("unknown", False), ("not_eligible", False),
])
def test_fatal_is_the_one_answer_for_every_kind(kind, fatal):
	assert ai_fix.AiFixError("x", kind=kind).fatal is fatal
	assert ai_fix.AI_SKIP_KINDS == frozenset({"not_eligible"})


def test_an_unknown_provider_is_a_fatal_config_error(monkeypatch, marked):
	monkeypatch.setattr("optimus.settings.get_config", lambda: SimpleNamespace(ai_provider="Aerele"))
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._provider_config()
	assert caught.value.kind == "config" and caught.value.fatal
	assert str(caught.value).startswith("[t]Unknown AI provider") and "Aerele" in str(caught.value)
	assert "Optimus Settings" in str(caught.value)


# ---------------------------------------------------------------------------
# Context: one prompt that does not fit is `context`; a window no prompt fits is `config`
# ---------------------------------------------------------------------------

def test_a_window_too_small_for_any_prompt_is_config():
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._check_context_fits(ai_prompts.SYSTEM_PROMPT, 2048, messages=[{"content": "x"}], out_tokens=512)
	assert caught.value.kind == "config" and caught.value.fatal
	assert "too small for the Optimus prompt" in str(caught.value)


def test_an_answer_reserve_no_prompt_fits_beside_is_config():
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._check_context_fits(ai_prompts.SYSTEM_PROMPT, 4096, messages=[{"content": "x"}], out_tokens=4000)
	assert caught.value.kind == "config"


def test_one_prompt_too_big_for_a_large_enough_window_is_context(marked):
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._check_context_fits(
			ai_prompts.SYSTEM_PROMPT, 3000, messages=[{"content": "x " * 4000}], out_tokens=600,
		)
	assert caught.value.kind == "context" and not caught.value.fatal
	assert str(caught.value).startswith("[t]The prompt did not fit the model's context window (3000 tokens).")


@pytest.mark.parametrize("hosted", [False, True])
def test_the_context_advice_fits_the_provider(hosted):
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._check_context_fits(
			ai_prompts.SYSTEM_PROMPT, 3000, messages=[{"content": "x " * 4000}], out_tokens=600, hosted=hosted,
		)
	assert ("OLLAMA_CONTEXT_LENGTH" in str(caught.value)) is not hosted
	assert ("Ollama" in str(caught.value)) is not hosted
	assert "Model" in str(caught.value) if hosted else "Context window (tokens)" in str(caught.value)


@pytest.mark.parametrize("url,local", [(_LOCAL, True), (_HOSTED, False), ("https://api.anthropic.com/v1/messages", False)])
def test_a_provider_context_rejection_names_the_cause_and_fits_the_provider(wire, marked, url, local):
	wire.install(Reply(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "prompt is too long: 215000 tokens > 200000 maximum"}}))
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._http_post(url, {}, {}, provider="openai", where="test")
	message = str(caught.value)
	assert caught.value.kind == "context" and not caught.value.fatal
	assert message.startswith("[t]The prompt did not fit the model's context window.")
	assert ("Ollama" in message) is local
	assert "prompt is too long: 215000 tokens" in message


@pytest.mark.parametrize("entry", ["fix", "steps"])
def test_a_hosted_window_too_small_never_points_at_ollama(monkeypatch, entry):
	_fake_provider(monkeypatch, base_url="https://api.openai.com/v1", context_tokens=2048)
	monkeypatch.setattr(ai_fix, "_dispatch_call", lambda *a, **k: pytest.fail("no request may be sent"))
	with pytest.raises(ai_fix.AiFixError) as caught:
		if entry == "fix":
			ai_fix.suggest_fix({"finding_type": "N+1 Query", "title": "x"})
		else:
			ai_fix.humanize_steps([{"label": "Save"}])
	assert caught.value.kind == "config" and "Ollama" not in str(caught.value)


# ---------------------------------------------------------------------------
# The parameter ladder (rl72 c1corr s2_ladder)
# ---------------------------------------------------------------------------

def test_a_context_rejection_through_a_proxy_is_not_renamed(wire):
	wire.install(
		_rejected("input length and `max_tokens` exceed context limit: 198000 + 21333 > 200000, decrease input length or `max_tokens` and try again"),
		_rejected("input length and `max_completion_tokens` exceed context limit: 198000 + 21333 > 200000"),
	)
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._call_openai_chat("https://p.invalid/v1", "", "custom", "system", [], max_tokens=512)
	assert caught.value.kind == "context"
	assert len(wire.posts) == len(wire.logs) == 1


def test_a_max_tokens_value_error_is_not_renamed(wire):
	wire.install(
		_rejected("max_tokens is too large: 512. This model supports at most 256 completion tokens, whereas you provided 512."),
		_rejected("max_completion_tokens is too large: 512. This model supports at most 256 completion tokens"),
	)
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._call_openai_chat("https://p.invalid/v1", "", "custom", "system", [], max_tokens=512)
	assert caught.value.kind == "bad_request"
	assert len(wire.posts) == len(wire.logs) == 1


@pytest.mark.parametrize("message", [
	"Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead.",
	"Unsupported parameter: max_tokens",
	"'max_tokens' is not supported with this model.",
	"max_tokens unsupported for reasoning models",
	"Invalid request: use max_completion_tokens, not max_tokens",
])
def test_unsupported_parameter_wording_renames_max_tokens(wire, message):
	wire.install(_rejected(message), Reply())
	assert ai_fix._call_openai_chat("https://p.invalid/v1", "", "custom", "system", [], max_tokens=512) == "answer"
	assert len(wire.posts) == 2 and wire.logs == []
	assert wire.posts[1][1]["json"]["max_completion_tokens"] == 512
	assert "max_tokens" not in wire.posts[1][1]["json"]


def test_parameter_names_echoed_in_another_error_do_not_rename(wire):
	wire.install(
		_rejected("Invalid request: temperature=0.1 max_tokens=512 tools not supported"),
		_rejected("Invalid request: max_tokens=512 tools not supported"),
	)
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._call_openai_chat("https://p.invalid/v1", "", "custom", "system", [], max_tokens=512)
	assert caught.value.kind == "bad_request"
	assert len(wire.posts) == 2 and len(wire.logs) == 1
	assert "max_tokens" in wire.posts[1][1]["json"] and "temperature" not in wire.posts[1][1]["json"]


def test_temperature_is_dropped_before_max_tokens_is_renamed(wire):
	wire.install(
		_rejected("Unsupported parameters: 'temperature' and 'max_tokens' are not supported with this model."),
		Reply(),
	)
	assert ai_fix._call_openai_chat("https://p.invalid/v1", "", "custom", "system", [], max_tokens=512) == "answer"
	second = wire.posts[1][1]["json"]
	assert "temperature" not in second and second["max_tokens"] == 512 and "max_completion_tokens" not in second


def test_each_post_of_the_ladder_gets_the_remaining_budget(wire):
	wire.use_clock()
	wire.install(
		(_rejected("Unsupported value: 'temperature' does not support 0.1"), 25),
		(_rejected("Unsupported parameter: 'max_tokens' is not supported with this model."), 30),
		(Reply(), 1),
	)
	assert ai_fix._call_openai_chat("https://p.invalid/v1", "", "custom", "system", [], timeout=60) == "answer"
	assert [post[1]["timeout"] for post in wire.posts] == [(10, 60), (10, 35), (5, 5)]


def test_each_redirect_hop_gets_the_remaining_budget(wire):
	wire.use_clock()
	wire.install((Reply(307, {}, headers={"location": "/v2/chat/completions"}), 20), (Reply(), 0))
	assert ai_fix._http_post(_LOCAL, {}, {}, provider="openai", where="test", timeout=60)["choices"]
	assert [post[1]["timeout"] for post in wire.posts] == [(10, 60), (10, 40)]
	assert wire.posts[1][0] == "https://provider.invalid/v2/chat/completions"


@pytest.mark.parametrize("message,kind", [
	("Your credit balance is too low to access the Anthropic API.", "quota"),
	("Invalid request: see your credit balance at https://console.example/settings/billing", "bad_request"),
])
def test_a_credit_balance_mention_is_quota_only_when_too_low(wire, message, kind):
	wire.install(_rejected(message))
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._http_post(_LOCAL, {}, {}, provider="openai", where="test")
	assert caught.value.kind == kind


def test_the_reask_reuses_the_first_calls_parameter_changes(wire, monkeypatch):
	from optimus.tests import test_ai_fix as base

	g = base.TestGuardedCompletion
	_fake_provider(monkeypatch)
	wire.install(
		_rejected("Unsupported value: 'temperature' does not support 0.1 with this model."),
		_answer(g._RAW),
		_answer(g._GOOD),
	)
	out = ai_fix.suggest_fix(dict(g._FINDING))
	assert out["guardrail"]["reasked"] is True and out["suggestion"] == g._GOOD
	assert len(wire.posts) == 3
	reask = wire.posts[2][1]["json"]
	assert reask["messages"][-1]["content"].startswith(ai_prompts.REASK_HEADER)
	assert "temperature" not in reask
	# per call chain only: the next call starts from the full request again
	wire.install(_answer(g._GOOD))
	ai_fix.suggest_fix(dict(g._FINDING))
	assert wire.posts[3][1]["json"]["temperature"] == ai_fix._TEMPERATURE


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("first_post_seconds,left", [(0.3, "59."), (25, "35 seconds")])
def test_a_timeout_names_the_whole_budget_not_what_was_left(wire, marked, first_post_seconds, left):
	wire.use_clock()
	wire.install(
		(_rejected("Unsupported value: 'temperature' does not support 0.1"), first_post_seconds),
		ai_fix.requests.exceptions.ReadTimeout("fake"),
	)
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._call_openai_chat("https://p.invalid/v1", "", "custom", "system", [], timeout=60)
	message = str(caught.value)
	assert caught.value.kind == "timeout" and caught.value.__context__ is None
	assert message.startswith("[t]The AI provider didn't respond within 60 seconds.")
	assert left not in message and "Request timeout (seconds)" in message


def test_a_spent_budget_names_the_whole_budget(wire, marked):
	wire.use_clock()
	wire.install((_rejected("Unsupported value: 'temperature' does not support 0.1"), 61))
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._call_openai_chat("https://p.invalid/v1", "", "custom", "system", [], timeout=60)
	assert caught.value.kind == "timeout"
	assert str(caught.value).startswith("[t]The AI provider didn't respond within 60 seconds.")


@pytest.mark.parametrize("budget,shown", [(59.6, "60"), (60, "60"), (0.2, "1")])
def test_a_timeout_budget_is_shown_in_whole_seconds(budget, shown):
	failure = ai_fix._timeout_failure(budget)
	assert failure.kind == "timeout"
	assert f"didn't respond within {shown} seconds." in str(failure)


def test_a_transport_failure_is_translated_and_names_only_the_error_type(wire, marked):
	wire.install(ai_fix.requests.exceptions.ConnectionError("boom fake-secret-detail"))
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._http_post(_LOCAL, {}, {}, provider="openai", where="test")
	message = str(caught.value)
	assert caught.value.kind == "transport" and caught.value.__context__ is None
	assert message.startswith("[t]Couldn't reach the AI provider (ConnectionError).")
	assert "fake-secret-detail" not in message and "try again" in message


@pytest.mark.parametrize("status,payload", [
	(402, {"error": {"message": "fake-reply"}}),
	(429, {"error": {"message": "fake-reply", "type": "insufficient_quota"}}),
	(429, {"error": {"message": "fake-reply"}}),
	(400, {"error": {"message": "fake-reply"}}),
	(500, {"error": {"message": "fake-reply"}}),
	(400, {"error": {"message": "maximum context length; fake-reply"}}),
])
def test_the_provider_reply_follows_the_message_without_a_stray_colon(wire, marked, status, payload):
	wire.install(Reply(status, payload))
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._http_post(_LOCAL, {}, {}, provider="openai", where="test")
	message = str(caught.value)
	assert ".:" not in message and message.count("fake-reply") == 1
	assert "[t]The provider replied: " in message


def test_an_internal_failure_records_where_it_happened_but_not_what_it_said(wire, monkeypatch, marked):
	_fake_provider(monkeypatch)

	def fake_guardrail_bug(*a, **k):
		return {}["fake-secret-detail"]

	monkeypatch.setattr(ai_fix.ai_guardrails, "verify_fix", fake_guardrail_bug)
	wire.install(_answer("answer"))
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix.suggest_fix({"finding_type": "N+1 Query", "title": "x"})
	failure = caught.value
	assert failure.kind == "internal" and failure.__context__ is None and failure.__cause__ is None
	assert str(failure).startswith("[t]The AI response could not be processed (KeyError).")
	row = ai_fix._exception_text(failure)
	assert ":fake_guardrail_bug" in row and ":_complete_with_guardrails" in row
	assert __file__ in row
	for text in (str(failure), row):
		assert "fake-secret-detail" not in text


def test_billed_usage_is_filtered_once_for_every_failure():
	usage = {"prompt_tokens": True, "completion_tokens": -1, "total_tokens": 5, "cost": 3}

	def failed():
		raise ai_fix.AiFixError("fake", kind="bad_response")

	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._with_usage_on_failure(failed, usage)
	assert caught.value.usage == ai_fix.AiFixError("x", usage=usage).usage == {"total_tokens": 5}
