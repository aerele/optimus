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
		# `from None`: the UnicodeEncodeError (whose .object is the key) is
		# not printed by any traceback formatter or Sentry chain walk.
		assert ei.value.__cause__ is None
		assert ei.value.__suppress_context__ is True

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
