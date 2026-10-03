"""Exercise the real HTTP/retry path with fake responses and fake credentials."""

import inspect
from types import SimpleNamespace

import pytest
import requests

from optimus import ai_fix

pytestmark = pytest.mark.rq

PROMPT = "PRIVATE-PROMPT-CANARY"
KEY = "Mq7Rt2Vx" * 4


class Response:
	def __init__(self, status=200, message="", location=None):
		self.status_code = status
		self.text = message
		self.message = message
		self.headers = {"location": location} if location is not None else {}

	def json(self):
		return {"error": {"message": self.message}, "choices": [{"message": {"content": "OK"}}]}


@pytest.fixture
def wire(monkeypatch):
	posts, logs = [], []
	monkeypatch.setattr(ai_fix, "_allow_key_over_http", lambda: False)
	monkeypatch.setattr(ai_fix, "_current_key_or_empty", lambda: KEY)
	monkeypatch.setattr(ai_fix, "_log_http_error", lambda *a, **k: logs.append((a, k)))
	def install(*responses):
		responses = iter(responses)
		def post(url, **kwargs):
			prepared = requests.Request("POST", url, headers=kwargs["headers"], auth=kwargs["auth"]).prepare()
			posts.append((url, dict(prepared.headers), dict(kwargs["json"])))
			assert kwargs["allow_redirects"] is False
			return next(responses)
		monkeypatch.setattr(ai_fix.requests, "post", post)
	return install, posts, logs


def send(url="https://provider.invalid/v1/chat/completions"):
	return ai_fix._http_post(url, {}, {"prompt": PROMPT}, provider="openai", where="chat/completions", timeout=30, auth=ai_fix._ApiKeyAuth("authorization", KEY, "Bearer "))


def test_http_interface_is_unchanged():
	assert tuple(inspect.signature(ai_fix._http_post).parameters) == ("url", "headers", "body", "provider", "where", "timeout", "auth", "quiet_statuses", "session_uuid")


@pytest.mark.parametrize("host", ["10.0.0.5", "provider.invalid", "2130706433"])
def test_plain_http_withholds_key_but_keyless_request_succeeds(wire, host):
	install, posts, logs = wire
	install(Response())
	assert send(f"http://{host}/v1/chat/completions")["choices"]
	assert "authorization" not in {key.lower() for key in posts[0][1]}
	assert logs == []


def test_explicit_site_opt_in_sends_key(wire, monkeypatch):
	install, posts, _ = wire
	monkeypatch.setattr(ai_fix, "_allow_key_over_http", lambda: True)
	install(Response())
	send("http://10.0.0.5/v1/chat/completions")
	assert posts[0][1]["authorization"] == "Bearer " + KEY


def test_loopback_sends_key_without_opt_in(wire):
	install, posts, _ = wire
	install(Response())
	send("http://localhost:11434/v1/chat/completions")
	assert posts[0][1]["authorization"] == "Bearer " + KEY


def test_withheld_key_auth_error_explains_configuration(wire):
	install, _, _ = wire
	install(Response(401, PROMPT))
	with pytest.raises(ai_fix.AiFixError) as caught:
		send("http://10.0.0.5/v1/chat/completions")
	assert caught.value.kind == "auth"
	assert "optimus_ai_allow_key_over_http" in str(caught.value)
	assert PROMPT not in str(caught.value)


@pytest.mark.parametrize("status", [400, 402, 404, 422, 429, 500])
def test_provider_body_is_only_in_admin_detail_never_public_error_or_log(wire, status):
	install, _, logs = wire
	install(Response(status, PROMPT + " " + KEY))
	with pytest.raises(ai_fix.AiFixError) as caught:
		send()
	error = caught.value
	assert PROMPT not in str(error) and "provider.invalid" not in str(error)
	assert PROMPT in error.detail and KEY not in error.detail
	assert PROMPT not in repr(logs) and KEY not in repr(logs)
	if status == 404:
		assert "https://provider.invalid/v1/chat/completions" in error.detail


def test_body_only_parameter_error_still_uses_bounded_retry(wire):
	install, posts, logs = wire
	install(Response(400, "temperature is not supported"), Response())
	result = ai_fix._post_with_param_ladder("https://provider.invalid/v1/chat/completions", {}, {"temperature": 0.1, "prompt": PROMPT}, timeout=30)
	assert result["choices"] and len(posts) == 2
	assert "temperature" in posts[0][2] and "temperature" not in posts[1][2]
	assert logs == []


def test_context_error_remains_actionable_without_echo(wire):
	install, posts, _ = wire
	install(Response(400, "maximum context length exceeded " + PROMPT))
	with pytest.raises(ai_fix.AiFixError) as caught:
		send()
	assert caught.value.kind == "config" and "OLLAMA_CONTEXT_LENGTH" in str(caught.value)
	assert PROMPT not in str(caught.value) and len(posts) == 1


@pytest.mark.parametrize("location", ["http://provider.invalid/v1", "https://other.invalid/v1", "https://provider.invalid/v1?secret=canary", "https://provider.invalid/v1#private", "?", "#", "/v1/\nnext"])
def test_unsafe_redirect_does_not_get_a_second_request(wire, location):
	install, posts, _ = wire
	install(Response(307, location=location), Response())
	with pytest.raises(ai_fix.AiFixError) as caught:
		send()
	assert caught.value.kind == "bad_response" and len(posts) == 1


def test_same_origin_upgrade_preserves_key_transport_policy(wire):
	install, posts, _ = wire
	install(Response(308, location="https://provider.invalid/v1/chat/completions/"), Response())
	send("http://provider.invalid/v1/chat/completions")
	assert len(posts) == 2 and "authorization" not in posts[0][1]
	assert posts[1][1]["authorization"] == "Bearer " + KEY
	assert posts[1][0].endswith("/")


@pytest.mark.parametrize("url", ["http://169.254.169.254/v1", "https://operator:secret@provider.invalid/v1"])
def test_direct_transport_entry_also_validates_url(wire, url):
	install, posts, _ = wire
	install(Response())
	with pytest.raises(ai_fix.AiFixError):
		send(url)
	assert posts == []


@pytest.mark.parametrize("roles,expected", [([], False), (["Optimus User"], False), (["System Manager"], True)])
def test_detail_is_visible_only_to_system_manager(monkeypatch, roles, expected):
	import frappe

	monkeypatch.setattr(frappe, "session", SimpleNamespace(user="fake-reader"))
	monkeypatch.setattr(frappe, "get_roles", lambda *a: roles, raising=False)
	error = ai_fix.AiFixError("Public explanation", detail=PROMPT)
	assert (PROMPT in ai_fix.user_message(error)) is expected
	assert str(error) == "Public explanation"


def test_probe_has_shorter_budget_and_loading_hint(monkeypatch):
	monkeypatch.setattr(ai_fix, "_provider_config", lambda: {"model": "local", "base_url": "http://localhost/v1"})
	monkeypatch.setattr(ai_fix, "_resolve_timeout_seconds", lambda: 600)
	seen = []
	def dispatch(*a, **kwargs):
		seen.append(kwargs)
		raise ai_fix.AiFixError("Provider timed out", kind="timeout")
	monkeypatch.setattr(ai_fix, "_dispatch_call", dispatch)
	result = ai_fix.test_connection()
	assert result["ok"] is False and seen[0]["timeout"] == 60
	assert "loading" in result["message"].lower()


@pytest.mark.parametrize("entry", ["http", "dispatch"])
@pytest.mark.parametrize("stage", ["send", "response", "redirect", "log"])
@pytest.mark.parametrize("interrupt_type", [SystemExit, KeyboardInterrupt])
def test_interrupt_traceback_has_no_prompt_reply_or_url_locals(monkeypatch, wire, entry, stage, interrupt_type):
	interrupt = interrupt_type("interrupted")
	class InterruptedResponse(Response):
		def json(self):
			if stage == "response":
				raise interrupt
			return super().json()
	response = InterruptedResponse(500 if stage == "log" else 307 if stage == "redirect" else 200, PROMPT)
	def fail(*args, **kwargs):
		raise interrupt
	monkeypatch.setattr(ai_fix.requests, "post", fail if stage == "send" else lambda *a, **k: response)
	if stage == "redirect":
		monkeypatch.setattr(ai_fix, "_same_origin_redirect", fail)
	if stage == "log":
		monkeypatch.setattr(ai_fix, "_log_http_error", fail)
	monkeypatch.setattr(ai_fix, "_get_api_key", lambda *a, **kw: KEY)
	with pytest.raises(interrupt_type) as caught:
		if entry == "http":
			send()
		else:
			ai_fix._dispatch_call({"base_url": "https://provider.invalid/v1", "protocol": "openai", "model": "fake"}, PROMPT, [{"role": "user", "content": PROMPT}], usage_out={})
	assert caught.value is interrupt
	assert caught.value.__context__ is None and caught.value.__cause__ is None
	tb = caught.value.__traceback__
	checked = 0
	while tb:
		if tb.tb_frame.f_code.co_filename == ai_fix.__file__:
			checked += 1
			for name, value in tb.tb_frame.f_locals.items():
				if name not in {"api_key", "secret"}:
					assert PROMPT not in repr(value), f"prompt in {name}"
					assert KEY not in repr(value), f"key in {name}"
		tb = tb.tb_next
	assert checked


def test_normal_http_error_does_not_retain_prompt_frames(wire):
	install, _, _ = wire
	install(Response(500, PROMPT))
	with pytest.raises(ai_fix.AiFixError) as caught:
		send()
	tb = caught.value.__traceback__
	while tb:
		if tb.tb_frame.f_code.co_filename == ai_fix.__file__:
			assert all(PROMPT not in repr(value) for value in tb.tb_frame.f_locals.values())
		tb = tb.tb_next
	assert PROMPT in caught.value.detail


def test_private_boundary_preserves_timeout_usage_and_logged_marker():
	JobTimeoutException = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	interrupt = JobTimeoutException("timeout")
	interrupt.usage = {"prompt_tokens": 31, "completion_tokens": 2, "total_tokens": 33, "private": PROMPT}
	interrupt.usage_complete = False
	interrupt.private = PROMPT
	ai_fix._mark_logged(interrupt, "FAKE-ERROR-ROW")
	@ai_fix._private_ai_frames
	def operation():
		raise interrupt
	with pytest.raises(JobTimeoutException) as caught:
		operation()
	assert caught.value is not interrupt
	assert caught.value.usage == {"prompt_tokens": 31, "completion_tokens": 2, "total_tokens": 33}
	assert caught.value.usage_complete is False
	assert getattr(caught.value, ai_fix._LOGGED_ATTR) is True
	assert getattr(caught.value, ai_fix._LOGGED_ROW_ATTR) == "FAKE-ERROR-ROW"
	assert not hasattr(caught.value, "private")
