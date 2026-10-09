"""Provider failures, bounded retries and billed failures, without network or a site."""

import json
from types import SimpleNamespace

import pytest

from optimus import ai_fix

pytestmark = pytest.mark.rq


@pytest.mark.parametrize("helper", ["_resolve_timeout_seconds", "is_available", "is_finding_type_excluded"])
def test_settings_failure_does_not_swallow_rq_timeout(monkeypatch, helper):
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = Timeout("fake timeout")

	def failed():
		raise original

	monkeypatch.setattr("optimus.settings.get_config", failed)
	with pytest.raises(Timeout) as caught:
		getattr(ai_fix, helper)("N+1 Query") if helper == "is_finding_type_excluded" else getattr(
			ai_fix, helper
		)()
	assert caught.value is not original and caught.value.__context__ is None


class Reply:
	def __init__(self, status=200, payload=None):
		self.status_code = status
		self.payload = (
			payload
			if payload is not None
			else {
				"choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
				"usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
			}
		)
		self.text = json.dumps(self.payload)
		self.headers = {}

	def json(self):
		return self.payload


@pytest.fixture
def wire(monkeypatch):
	posts, logs = [], []
	monkeypatch.setattr(ai_fix, "_current_key_or_empty", lambda: "")
	monkeypatch.setattr(ai_fix, "_log_http_error", lambda *a, **kw: logs.append((a, kw)))
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **kw: logs.append((a, kw)))
	monkeypatch.setattr(ai_fix, "_record_session_spend", lambda *_: None)

	def install(*replies):
		pending = iter(replies)

		def post(url, **kw):
			posts.append((url, {**kw, "json": dict(kw["json"])}))
			return next(pending)

		monkeypatch.setattr(ai_fix.requests, "post", post)

	return SimpleNamespace(install=install, posts=posts, logs=logs)


@pytest.mark.parametrize(
	"status,payload,kind,fatal",
	[
		(401, {"error": {"message": "do not show this"}}, "auth", True),
		(403, {}, "auth", True),
		(404, {}, "not_found", True),
		(402, {}, "quota", True),
		(429, {"error": {"code": "insufficient_quota"}}, "quota", True),
		(429, {"error": {"message": "rate limit; see /account/billing"}}, "rate_limited", False),
		(400, {"error": {"message": "Your credit balance is too low"}}, "quota", True),
		(400, {"error": {"message": "maximum context length; max_tokens"}}, "config", True),
		(422, {"error": {"message": "maximum context length; max_tokens"}}, "config", True),
		(400, {"error": {"message": "invalid option"}}, "bad_request", False),
		(422, {}, "bad_request", False),
		(500, {}, "server", False),
		(503, {"error": "busy"}, "server", False),
	],
)
def test_failure_kinds_reach_the_caller_once(wire, status, payload, kind, fatal):
	wire.install(Reply(status, payload))
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._http_post("https://provider.invalid/v1", {}, {}, provider="openai", where="test")
	assert caught.value.kind == kind
	assert caught.value.fatal is fatal
	assert len(wire.posts) == len(wire.logs) == 1
	if status in (401, 403):
		assert "do not show this" not in str(caught.value)


@pytest.mark.parametrize("model", ["o3-mini", "openai/o4-mini", "router/openai/gpt-5-mini"])
def test_reasoning_wire_budget_is_not_silently_increased(wire, model):
	wire.install(Reply())
	assert (
		ai_fix._call_openai_chat("https://provider.invalid/v1", "", model, "system", [], max_tokens=512)
		== "answer"
	)
	body = wire.posts[0][1]["json"]
	assert body["max_completion_tokens"] == 512
	assert "max_tokens" not in body and "temperature" not in body


def test_validation_retry_ladder_is_bounded_and_only_logs_final_failure(wire):
	wire.install(
		Reply(400, {"error": {"message": "Unsupported parameter: max_tokens; use max_completion_tokens"}}),
		Reply(422, {"error": {"message": "Unsupported temperature"}}),
		Reply(),
	)
	assert ai_fix._call_openai_chat("https://provider.invalid/v1", "", "custom", "system", []) == "answer"
	assert len(wire.posts) == 3 and wire.logs == []
	assert "max_tokens" in wire.posts[0][1]["json"]
	assert "max_completion_tokens" in wire.posts[1][1]["json"]
	assert "temperature" not in wire.posts[2][1]["json"]


def test_context_failure_never_enters_parameter_retry(wire):
	wire.install(Reply(400, {"error": {"message": "maximum context length: max_tokens temperature"}}))
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._call_openai_chat("https://provider.invalid/v1", "", "custom", "system", [])
	assert caught.value.kind == "config"
	assert len(wire.posts) == len(wire.logs) == 1


def test_repeated_rejected_parameter_stops_instead_of_looping(wire):
	wire.install(
		*[Reply(400, {"error": {"message": "Unsupported max_tokens; use max_completion_tokens"}})] * 4
	)
	with pytest.raises(ai_fix.AiFixError):
		ai_fix._call_openai_chat("https://provider.invalid/v1", "", "custom", "system", [])
	assert len(wire.posts) == 2 and len(wire.logs) == 1


@pytest.mark.parametrize(
	"content,finish,expected",
	[
		("<think>private reasoning</think>answer", "stop", "answer"),
		(" <think>a</think>\n<think>b</think>answer", "stop", "answer"),
		("answer <think>literal</think>", "stop", "answer <think>literal</think>"),
	],
)
def test_inline_thinking_is_not_reported_as_the_answer(wire, content, finish, expected):
	wire.install(Reply(payload={"choices": [{"message": {"content": content}, "finish_reason": finish}]}))
	assert ai_fix._call_openai_chat("https://provider.invalid/v1", "", "custom", "system", []) == expected


def test_truncated_thinking_preserves_reported_usage_on_error(wire):
	wire.install(
		Reply(
			payload={
				"choices": [{"message": {"content": "<think>unfinished"}, "finish_reason": "length"}],
				"usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
			}
		)
	)
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._call_openai_chat("https://provider.invalid/v1", "", "custom", "system", [])
	assert caught.value.kind == "bad_response"
	assert caught.value.usage["total_tokens"] == 7
	assert "unfinished" not in str(caught.value)


@pytest.mark.parametrize(
	"payload",
	[
		{"choices": {}},
		{"choices": [None]},
		{"choices": [{"message": []}]},
		{"choices": [{"message": {"content": {"bad": "shape"}}}]},
	],
)
def test_malformed_success_is_typed_and_keeps_usage(wire, payload):
	payload["usage"] = {"total_tokens": 7}
	wire.install(Reply(payload=payload))
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._call_openai_chat("https://provider.invalid/v1", "", "custom", "system", [])
	assert caught.value.kind == "bad_response"
	assert caught.value.usage["total_tokens"] == 7


def test_rq_timeout_during_json_decode_escapes_fresh(wire):
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = Timeout("fake timeout")
	reply = Reply()

	def broken_json():
		raise original

	reply.json = broken_json
	wire.install(reply)
	with pytest.raises(Timeout) as caught:
		ai_fix._http_post("https://provider.invalid/v1", {}, {}, provider="openai", where="test")
	assert caught.value is not original
	assert caught.value.__context__ is None
	assert wire.logs == []


def test_explicit_session_reaches_http_logging(wire):
	wire.install(Reply(503))
	with pytest.raises(ai_fix.AiFixError):
		ai_fix._call_openai_chat(
			"https://provider.invalid/v1", "", "custom", "system", [], session_uuid="fake-session"
		)
	assert wire.logs[0][1]["session_uuid"] == "fake-session"


def test_retry_uses_remaining_budget_and_never_retries_after_deadline(wire, monkeypatch):
	now = [0.0]
	monkeypatch.setattr(ai_fix.time, "monotonic", lambda: now[0])
	posts = []

	def post(url, **kw):
		posts.append(kw["timeout"])
		now[0] += 12
		return Reply(400, {"error": {"message": "Unsupported temperature"}})

	monkeypatch.setattr(ai_fix.requests, "post", post)
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._call_openai_chat("https://provider.invalid/v1", "", "custom", "system", [], timeout=10)
	assert caught.value.kind == "timeout"
	assert posts == [(10, 10)]


def test_connect_timeout_is_bounded_separately(wire):
	wire.install(Reply())
	ai_fix._http_post("https://provider.invalid/v1", {}, {}, provider="openai", where="test", timeout=180)
	connect, read = wire.posts[0][1]["timeout"]
	assert 0 < connect <= 10 and 0 < read <= 180


@pytest.mark.parametrize("content", ["<think>unfinished", "<think>a</think>" * 4 + "answer"])
def test_misleading_stop_does_not_expose_unfinished_or_excessive_thinking(wire, content):
	wire.install(
		Reply(
			payload={
				"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
				"usage": {"total_tokens": 7},
			}
		)
	)
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._call_openai_chat("https://provider.invalid/v1", "", "custom", "system", [])
	assert caught.value.usage["total_tokens"] == 7


@pytest.mark.parametrize("entry", ["fix", "steps"])
def test_post_response_failures_preserve_usage_without_raw_exception_text(wire, monkeypatch, entry):
	provider = {
		"name": "fake",
		"protocol": "openai",
		"base_url": "https://fake.invalid/v1",
		"model": "fake",
		"needs_key": False,
		"context_tokens": 128000,
	}
	monkeypatch.setattr(ai_fix, "_provider_config", lambda: provider)
	monkeypatch.setattr(ai_fix, "_resolve_display_threshold_ms", lambda: 1000)
	monkeypatch.setattr(ai_fix, "_get_api_key", lambda *a: "")
	monkeypatch.setattr(ai_fix, "is_finding_type_excluded", lambda *a: False)

	def failed(*a, **kw):
		raise RuntimeError("fake-sensitive-reply")

	monkeypatch.setattr(ai_fix.ai_guardrails, "verify_fix", failed)
	wire.install(
		Reply(
			payload={
				"choices": [{"message": {"content": "answer" if entry == "fix" else "  "}}],
				"usage": {"total_tokens": 7},
			}
		)
	)
	with pytest.raises(ai_fix.AiFixError) as caught:
		if entry == "fix":
			ai_fix.suggest_fix({"finding_type": "N+1 Query"})
		else:
			ai_fix.humanize_steps([{"label": "fake"}])
	assert caught.value.usage["total_tokens"] == 7
	assert "fake-sensitive-reply" not in str(caught.value)
	assert caught.value.__context__ is None


def _use_fake_provider(monkeypatch):
	monkeypatch.setattr(ai_fix, "_provider_config", lambda: {
		"name": "fake", "protocol": "openai", "base_url": "https://fake.invalid/v1", "model": "fake",
		"needs_key": False, "context_tokens": 128000,
	})
	monkeypatch.setattr(ai_fix, "_resolve_display_threshold_ms", lambda: 1000)
	monkeypatch.setattr(ai_fix, "_get_api_key", lambda *a: "")
	monkeypatch.setattr(ai_fix, "is_finding_type_excluded", lambda *a: False)


def test_a_typed_failure_after_a_billed_reply_still_carries_the_usage(wire, monkeypatch):
	_use_fake_provider(monkeypatch)

	def failed(*a, **kw):
		raise ai_fix.AiFixError("fake typed failure", kind="bad_response")  # raised with no usage

	monkeypatch.setattr(ai_fix.ai_guardrails, "verify_fix", failed)
	wire.install(Reply(payload={"choices": [{"message": {"content": "answer"}}], "usage": {"total_tokens": 7}}))
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix.suggest_fix({"finding_type": "N+1 Query"})
	assert caught.value.kind == "bad_response" and str(caught.value) == "fake typed failure"
	assert caught.value.usage["total_tokens"] == 7


@pytest.mark.parametrize("content", [[], {"type": "text", "text": "not a list"}, None])
def test_an_anthropic_reply_without_text_keeps_its_usage(wire, content):
	wire.install(Reply(payload={"content": content, "usage": {"input_tokens": 5, "output_tokens": 2}}))
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix._call_anthropic("https://provider.invalid", "", "claude", "system", [])
	assert caught.value.kind == "bad_response"
	assert caught.value.usage == {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}


@pytest.mark.parametrize("entry", ["fix", "steps"])
def test_a_logged_rq_timeout_keeps_its_log_marker_through_usage_handling(wire, monkeypatch, entry):
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = Timeout("fake timeout")
	ai_fix._mark_logged(original, "fake-log-row")
	_use_fake_provider(monkeypatch)

	def dispatch(*a, **kw):
		raise original

	monkeypatch.setattr(ai_fix, "_dispatch_call", dispatch)
	with pytest.raises(Timeout) as caught:
		if entry == "fix":
			ai_fix.suggest_fix({"finding_type": "N+1 Query"})
		else:
			ai_fix.humanize_steps([{"label": "fake"}])
	assert caught.value is not original and caught.value.__context__ is None
	# the row is already written: a caller's log_ai_failure appends to it instead of a second row
	assert getattr(caught.value, ai_fix._LOGGED_ATTR, False) is True
	assert getattr(caught.value, ai_fix._LOGGED_ROW_ATTR, None) == "fake-log-row"
