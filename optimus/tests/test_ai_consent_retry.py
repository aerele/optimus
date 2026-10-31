"""Revocation between HTTP sends stops reuse of the raw prompt and keeps billed usage."""

import copy
import json
from types import SimpleNamespace

import pytest
import requests

from optimus import ai_fix, settings
from optimus.tests.test_ai_fix import TestGuardedCompletion as Replies

pytestmark = pytest.mark.rq


@pytest.fixture(autouse=True)
def current_sensitive_rules(monkeypatch):
	# A successful current SQL settings read; failure/refresh semantics are
	# covered by test_production_review_fixes, independently of consent.
	monkeypatch.setattr("optimus.ai_privacy._fresh_sensitive_columns", lambda: "")


@pytest.fixture
def consent(monkeypatch, bound_provider_credentials):
	import frappe

	state = SimpleNamespace(raw=True, read_error=False, opened=0, closed=0, sent=[])
	class Cursor:
		def execute(self, query, values):
			assert query.startswith("SELECT") and values == ("Optimus Settings", "ai_send_raw_values")
			if state.read_error:
				raise RuntimeError("fake consent read unavailable")
		def fetchone(self):
			return (int(state.raw),)
		def close(self):
			pass
	class Connection:
		def __init__(self):
			state.opened += 1
		def cursor(self):
			return Cursor()
		def close(self):
			state.closed += 1
	# Ordinary reads keep the caller's pre-provider snapshot. Only a new
	# connection can observe the administrator's committed revocation.
	monkeypatch.setattr(frappe, "db", SimpleNamespace(db_type="mariadb",
		get_single_value=lambda *a, **kw: True, get_connection=Connection), raising=False)
	monkeypatch.setattr(settings, "get_config", lambda: SimpleNamespace(
		ai_enabled=True, ai_provider="OpenAI-compatible", ai_model="fake",
		ai_base_url="https://provider.invalid/v1", ai_context_tokens=128000, ai_send_raw_values=True))
	monkeypatch.setattr(ai_fix, "_current_key_or_empty", lambda: "")
	monkeypatch.setattr(ai_fix, "_reask_enabled", lambda: True)
	return state


def finding():
	value = copy.deepcopy(Replies._FINDING)
	value["technical_detail"]["normalized_query"] = "SELECT name FROM customer WHERE name='PRIVATE-RETRY-CANARY'"
	return value


@pytest.mark.parametrize("change", ["revoked", "read_error", "unchanged"])
def test_guardrail_retry_observes_fresh_consent_and_retains_first_usage(consent, monkeypatch, change):
	def post(url, **kwargs):
		assert consent.closed == consent.opened, "no consent connection may stay open during HTTP"
		consent.sent.append(copy.deepcopy(kwargs["json"]))
		if change == "revoked":
			consent.raw = False
		elif change == "read_error":
			consent.read_error = True
		return Replies._resp(Replies._RAW if len(consent.sent) == 1 else Replies._GOOD,
			usage={"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70})
	monkeypatch.setattr(requests, "post", post)
	out = ai_fix.suggest_fix(finding(), timeout=60)
	assert len(consent.sent) == (2 if change == "unchanged" else 1)
	assert out["guardrail"]["reasked"] is (change == "unchanged")
	assert out["guardrail"]["fallback"] is (change != "unchanged")
	assert out["tokens"]["total_tokens"] == (140 if change == "unchanged" else 70)
	assert out["usage_complete"]
	assert "PRIVATE-RETRY-CANARY" in json.dumps(consent.sent[0])
	assert consent.opened == consent.closed and consent.opened > 0


@pytest.mark.parametrize("retry", ["redirect", "parameter"])
def test_transport_retry_cannot_resend_raw_prompt_after_revocation(consent, monkeypatch, retry):
	def post(url, **kwargs):
		consent.sent.append(copy.deepcopy(kwargs["json"]))
		consent.raw = False
		response = requests.Response()
		response.status_code = 307 if retry == "redirect" else 400
		response.headers["Location"] = "https://provider.invalid/v1/moved"
		response._content = json.dumps({"error": {"message": "Unsupported parameter: 'temperature'", "param": "temperature", "code": "unsupported_parameter"}}).encode()
		return response
	monkeypatch.setattr(requests, "post", post)
	with pytest.raises(ai_fix.AiFixError):
		ai_fix.suggest_fix(finding(), timeout=60)
	assert len(consent.sent) == 1
	assert consent.opened > 0 and consent.opened == consent.closed


def test_consent_scope_is_reset_after_failure(consent, monkeypatch):
	def post(url, **kwargs):
		consent.sent.append(kwargs["json"])
		raise requests.exceptions.ConnectionError("fake")
	monkeypatch.setattr(requests, "post", post)
	with pytest.raises(ai_fix.AiFixError):
		ai_fix.suggest_fix(finding())
	consent.raw = False
	# A later private prompt in the same worker must not inherit the raw guard.
	monkeypatch.setattr(settings, "get_config", lambda: SimpleNamespace(
		ai_enabled=True, ai_provider="OpenAI-compatible", ai_model="fake",
		ai_base_url="https://provider.invalid/v1", ai_context_tokens=128000, ai_send_raw_values=False))
	monkeypatch.setattr(requests, "post", lambda *a, **kw: Replies._resp(Replies._GOOD))
	assert ai_fix.suggest_fix(finding())["suggestion"]


@pytest.mark.parametrize("entry", ["fix", "steps"])
def test_raw_transport_retry_checks_consent_for_both_public_clients(consent, monkeypatch, entry):
	def post(url, **kwargs):
		consent.sent.append(kwargs["json"])
		consent.raw = False
		response = requests.Response()
		response.status_code = 307
		response.headers["Location"] = "https://provider.invalid/v1/moved"
		return response
	monkeypatch.setattr(requests, "post", post)
	with pytest.raises(ai_fix.AiFixError, match="consent"):
		if entry == "fix":
			ai_fix.suggest_fix(finding())
		else:
			ai_fix.humanize_steps([{"label": "Open private customer", "doctype": "Customer", "duration_ms": 1}])
	assert len(consent.sent) == 1


def test_consent_read_interrupt_closes_connection_and_escapes(consent, monkeypatch):
	import frappe

	Timeout = pytest.importorskip("rq.timeouts").JobTimeoutException
	closed = []
	class Cursor:
		def execute(self, *a):
			raise Timeout("fake")
		def close(self):
			closed.append("cursor")
	class Connection:
		def cursor(self):
			return Cursor()
		def close(self):
			closed.append("connection")
	monkeypatch.setattr(frappe.db, "get_connection", Connection)
	with pytest.raises(Timeout):
		ai_fix.ai_privacy._fresh_raw_values_enabled()
	assert closed == ["cursor", "connection"]


@pytest.mark.parametrize("schema", [None, "custom_site_schema"])
def test_postgres_consent_read_matches_frappe_v15_and_v16_connection_schema(monkeypatch, schema):
	import frappe

	statements, closed = [], []
	class Cursor:
		def execute(self, query, values):
			statements.append((query, values))
		def fetchone(self):
			return ("1",)
		def close(self):
			closed.append("cursor")
	class Connection:
		def cursor(self):
			return Cursor()
		def close(self):
			closed.append("connection")
	db = SimpleNamespace(db_type="postgres", get_connection=Connection)
	if schema is not None:
		db.db_schema = schema
	monkeypatch.setattr(frappe, "db", db, raising=False)
	assert ai_fix.ai_privacy._fresh_raw_values_enabled() is True
	assert len(statements) == (1 if schema is None else 2)
	if schema is not None:
		assert statements[0] == ("SET search_path TO %s", (schema,))
	assert '"tabSingles"' in statements[-1][0]
	assert closed == ["cursor", "connection"]
