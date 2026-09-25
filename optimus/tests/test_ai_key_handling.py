# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The API key never sits in a printable container (PR-0a).

It may live only in ``__Auth`` (encrypted), a local named ``api_key`` and an
``ai_fix._ApiKeyAuth``. These tests pin the primitives and the request shape;
``test_ai_secret_canary.py`` proves it end to end on every failure path.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

from optimus import ai_fix
from optimus import settings as _settings

KEY = "sk-live-0123456789abcdefXYZ"
_OPENAI_OK = {"choices": [{"message": {"content": "**Fix**\n\nuse a join"}}]}
_ANTHROPIC_OK = {"content": [{"type": "text", "text": "**Fix**\n\nadd an index"}]}


def _store_key(monkeypatch, value):
	"""Make ``get_decrypted_password`` return ``value`` for ai_api_key."""
	monkeypatch.setattr(
		"frappe.utils.password.get_decrypted_password", lambda *a, **k: value, raising=False
	)


def _cfg(**kw):
	base = {"ai_enabled": True, "ai_provider": "OpenAI", "ai_base_url": "", "ai_model": ""}
	base.update(kw)
	return _settings.OptimusConfig(**base)


class _Resp:
	def __init__(self, status_code=200, payload=None, text=""):
		self.status_code = status_code
		self._payload = payload if payload is not None else {}
		self.text = text

	def json(self):
		return self._payload


def _capture(resp):
	"""A ``requests.post`` fake that records what was passed and the headers
	requests would really send after the ``auth`` object ran."""
	calls = []

	def _post(url, headers=None, json=None, timeout=None, auth=None):  # noqa: A002
		wire = requests.Request("POST", url, headers=dict(headers or {}), json=json, auth=auth).prepare()
		calls.append(SimpleNamespace(
			url=url, headers=headers, body=json, auth=auth, wire_headers=dict(wire.headers),
		))
		return resp
	_post.calls = calls
	return _post


_FINDING = {"finding_type": "Slow Query", "title": "slow", "technical_detail": {"normalized_query": "SELECT 1"}}


# ---------------------------------------------------------------------------
# Primitives: AiFixError shape, _get_api_key, _ApiKeyAuth
# ---------------------------------------------------------------------------

class TestAiFixErrorShape:
	def test_carries_kind_usage_and_status(self):
		e = ai_fix.AiFixError("m", status_code=429, kind="rate_limited", usage={"total_tokens": 5})
		assert (str(e), e.status_code, e.kind, e.usage) == ("m", 429, "rate_limited", {"total_tokens": 5})

	def test_defaults(self):
		e = ai_fix.AiFixError("m")
		assert (e.status_code, e.kind, e.usage) == (None, "unknown", None)


class TestGetApiKey:
	def test_get_api_key_strips_and_rejects_non_latin1(self, monkeypatch):
		# Review Focus #5: a pasted trailing newline is stripped; a smart quote
		# raises a clear config error that never carries the key.
		_store_key(monkeypatch, f"  {KEY}\n")
		assert ai_fix._get_api_key() == KEY

		_store_key(monkeypatch, "sk-live-0123’456789")
		with pytest.raises(ai_fix.AiFixError) as ei:
			ai_fix._get_api_key()
		assert ei.value.kind == "config"
		assert "sk-live" not in str(ei.value)
		# No exception that could carry the key (a UnicodeEncodeError's .object
		# is the whole header) is chained: `from None` ...
		assert ei.value.__cause__ is None
		assert ei.value.__suppress_context__ is True
		# ... and no exception is being handled at the point of the raise, so
		# __context__ itself is None, not merely suppressed for display. A
		# chain-walker (Sentry, a custom Error Log formatter) that reads
		# __context__ directly, ignoring __suppress_context__, finds nothing.
		assert ei.value.__context__ is None

	@pytest.mark.parametrize("bad_key", [
		"sk-live-0123\n456789",
		"sk-live-0123\t456789",
		"sk-live-0123\x00456789",
	])
	def test_get_api_key_rejects_control_characters(self, monkeypatch, bad_key):
		# An internal control character (CR/LF, tab, NUL) passes the latin-1
		# check but would surface the key in a requests ValueError message
		# or in http.client putheader locals. Reject it the same way as a
		# non-latin-1 character, before any HTTP call.
		_store_key(monkeypatch, bad_key)
		with pytest.raises(ai_fix.AiFixError) as ei:
			ai_fix._get_api_key()
		assert ei.value.kind == "config"
		assert "sk-live" not in str(ei.value)
		assert ei.value.__cause__ is None
		assert ei.value.__context__ is None

	@pytest.mark.parametrize("bad_key", [
		"sk-live-0123\u00a0456789",
		"sk-live-0123\u00ad456789",
		"sk-live-0123\x85456789",
		"sk-live-0123\x80456789",
		"sk-live-0123 456789",
	], ids=["no-break-space", "soft-hyphen", "C1-next-line", "C1-0x80", "internal-space"])
	def test_a_key_that_is_not_printable_ascii_fails_before_any_http(self, monkeypatch, bad_key):
		# A no-break space, a soft hyphen or a C1 control encodes in latin-1
		# and so in a header, but a provider key is plain printable ASCII:
		# any other character is a paste artefact. Reject it before any HTTP
		# call, like a smart quote or a newline.
		_store_key(monkeypatch, bad_key)
		with pytest.raises(ai_fix.AiFixError) as ei:
			ai_fix._get_api_key()
		assert ei.value.kind == "config"
		assert "sk-live" not in str(ei.value) and "not plain ASCII" in str(ei.value)
		assert ei.value.__cause__ is None and ei.value.__context__ is None
		fake = _capture(_Resp(200, _OPENAI_OK))
		monkeypatch.setattr(requests, "post", fake)
		with patch("optimus.settings.get_config", return_value=_cfg()):
			with pytest.raises(ai_fix.AiFixError):
				ai_fix.suggest_fix(dict(_FINDING))
		assert fake.calls == []

	def test_every_printable_ascii_character_is_accepted(self, monkeypatch):
		key = "sk-" + "".join(chr(c) for c in range(0x21, 0x7F))
		_store_key(monkeypatch, f" {key}\n")
		assert ai_fix._get_api_key() == key

	def test_unset_key_is_empty_string(self, monkeypatch):
		_store_key(monkeypatch, None)
		assert ai_fix._get_api_key() == ""

	def test_decrypt_failure_is_empty_string(self, monkeypatch):
		def _boom(*a, **k):
			raise RuntimeError("Encryption key is invalid")
		monkeypatch.setattr("frappe.utils.password.get_decrypted_password", _boom, raising=False)
		assert ai_fix._get_api_key() == ""
		assert ai_fix._current_key_or_empty() == ""

	def test_current_key_or_empty_does_not_validate(self, monkeypatch):
		# log_ai_failure scrubs with this, so it must return even a key that
		# _get_api_key rejects.
		_store_key(monkeypatch, " sk-live-0123’456789 ")
		assert ai_fix._current_key_or_empty() == "sk-live-0123’456789"


class TestApiKeyAuth:
	def test_repr_and_str_are_masked(self):
		auth = ai_fix._ApiKeyAuth("authorization", KEY, prefix="Bearer ")
		assert repr(auth) == "<_ApiKeyAuth authorization: ********>"
		assert str(auth) == repr(auth)
		assert KEY not in repr(auth)
		# The value lives in __slots__, not in an instance __dict__ a
		# debugger or serializer might walk.
		assert vars(auth) == {}

	def test_sets_the_header_at_send_time(self):
		auth = ai_fix._ApiKeyAuth("authorization", KEY, prefix="Bearer ")
		prepared = requests.Request("POST", "https://x.invalid/v1", headers={"a": "b"}, auth=auth).prepare()
		assert prepared.headers["authorization"] == f"Bearer {KEY}"


class TestScrubLiteralsFor:
	"""The literals a provider reply is scrubbed of: the stored key and the
	key the request was sent with, each raw and JSON-escaped."""

	def test_the_stored_key_then_the_in_flight_key(self, monkeypatch):
		stored, in_flight = 'sk-stored-0123"quoted', "sk-inflight-0123456789"
		_store_key(monkeypatch, f" {stored}\n")
		auth = ai_fix._ApiKeyAuth("authorization", in_flight, prefix="Bearer ")
		assert ai_fix._scrub_literals_for(auth) == (stored, 'sk-stored-0123\\"quoted', in_flight, in_flight)

	def test_no_key_and_no_auth_is_empty(self, monkeypatch):
		_store_key(monkeypatch, None)
		assert ai_fix._scrub_literals_for(None) == ()

	def test_an_auth_that_is_not_ours_adds_nothing(self, monkeypatch):
		_store_key(monkeypatch, KEY)
		assert ai_fix._scrub_literals_for(requests.auth.HTTPBasicAuth("u", "p")) == (KEY, KEY)


# ---------------------------------------------------------------------------
# The provider dict and every request
# ---------------------------------------------------------------------------

class TestProviderDict:
	def test_provider_dict_never_holds_the_key(self, monkeypatch):
		_store_key(monkeypatch, KEY)
		with patch("optimus.settings.get_config", return_value=_cfg()):
			p = ai_fix._resolve_provider()
		assert "api_key" not in p
		assert p["has_key"] is True
		assert KEY not in repr(p)

	def test_has_key_false_when_unset(self, monkeypatch):
		_store_key(monkeypatch, "")
		with patch("optimus.settings.get_config", return_value=_cfg()):
			assert ai_fix._resolve_provider()["has_key"] is False
			assert ai_fix.is_available() is False  # hosted provider needs a key


class TestRequestsCarryTheKeyOnlyInAuth:
	def test_no_key_provider_sends_no_auth(self, monkeypatch):
		# Review Focus #1: LAN Ollama over plain http with no key keeps working
		# and sends no auth header at all.
		_store_key(monkeypatch, "")
		fake = _capture(_Resp(200, _OPENAI_OK))
		monkeypatch.setattr(requests, "post", fake)
		cfg = _cfg(ai_provider="OpenAI-compatible", ai_base_url="http://10.0.0.5:11434/v1", ai_model="qwen3-coder:30b")
		with patch("optimus.settings.get_config", return_value=cfg):
			assert ai_fix.is_available() is True
			out = ai_fix.suggest_fix(dict(_FINDING))
		assert out["suggestion"].startswith("**Fix**")
		call = fake.calls[0]
		assert call.url == "http://10.0.0.5:11434/v1/chat/completions"
		assert call.auth is None
		assert "authorization" not in {k.lower() for k in call.wire_headers}

	def test_openai_key_rides_only_in_the_auth_object(self, monkeypatch):
		_store_key(monkeypatch, f"{KEY}\n")
		fake = _capture(_Resp(200, _OPENAI_OK))
		monkeypatch.setattr(requests, "post", fake)
		with patch("optimus.settings.get_config", return_value=_cfg()):
			ai_fix.suggest_fix(dict(_FINDING))
		call = fake.calls[0]
		assert KEY not in repr(call.headers) and KEY not in repr(call.body)
		assert isinstance(call.auth, ai_fix._ApiKeyAuth) and KEY not in repr(call.auth)
		assert call.wire_headers["authorization"] == f"Bearer {KEY}"

	def test_anthropic_key_rides_only_in_the_auth_object(self, monkeypatch):
		_store_key(monkeypatch, KEY)
		fake = _capture(_Resp(200, _ANTHROPIC_OK))
		monkeypatch.setattr(requests, "post", fake)
		with patch("optimus.settings.get_config", return_value=_cfg(ai_provider="Anthropic")):
			ai_fix.suggest_fix(dict(_FINDING))
		call = fake.calls[0]
		assert KEY not in repr(call.headers)
		assert call.wire_headers["x-api-key"] == KEY

	def test_non_latin_key_fails_before_any_http(self, monkeypatch):
		_store_key(monkeypatch, "sk-live-0123’456789")
		fake = _capture(_Resp(200, _OPENAI_OK))
		monkeypatch.setattr(requests, "post", fake)
		with patch("optimus.settings.get_config", return_value=_cfg()):
			with pytest.raises(ai_fix.AiFixError) as ei:
				ai_fix.suggest_fix(dict(_FINDING))
			probe = ai_fix.test_connection()
		assert ei.value.kind == "config"
		assert fake.calls == []
		assert probe["ok"] is False and "smart quote" in probe["message"]
		assert "sk-live" not in probe["message"]
