"""Settings saves explain transport policy without echoing typed credentials."""

import pytest

from optimus import ai_fix
from optimus.tests.test_profiler_settings_validation import _fresh_controller

pytestmark = pytest.mark.rq


@pytest.fixture
def settings_doc(monkeypatch):
	Controller, frappe = _fresh_controller(monkeypatch)
	frappe.conf = {}
	frappe.throw = lambda *a, **k: pytest.fail("URL checks must warn, never throw")
	fields = dict(tracked_apps=[], ai_enabled=1, ai_provider="OpenAI-compatible",
		ai_base_url="https://model.invalid/v1", ai_api_key="********", ai_model="local")
	before = Controller(**fields)
	doc = Controller(**fields)
	doc.get_doc_before_save = lambda: before
	return doc, before, frappe


@pytest.mark.parametrize("raw,expected", [
	("http://operator:URL-PASSWORD-CANARY@host/v1", "http://host/v1"),
	("//operator:URL-PASSWORD-CANARY@host/v1", "//host/v1"),
	("operator:URL-PASSWORD-CANARY@host/v1", "host/v1"),
	("  https://operator:p@ss@host/v1", "  https://host/v1"),
	("https://host/path@part/v1", "https://host/path@part/v1"),
	(None, ""), ({}, ""),
])
def test_strip_userinfo(raw, expected):
	assert ai_fix.strip_url_userinfo(raw) == expected


@pytest.mark.parametrize("enabled,provider", [(True, "OpenAI-compatible"), (False, "OpenAI-compatible"), (True, "OpenAI")])
def test_userinfo_never_reaches_saved_value_or_version_comparison(settings_doc, enabled, provider):
	doc, before, frappe = settings_doc
	doc.ai_enabled, doc.ai_provider = enabled, provider
	doc.ai_base_url = before.ai_base_url = "https://operator:URL-PASSWORD-CANARY@host/v1"
	doc.validate()
	assert doc.ai_base_url == before.ai_base_url == "https://host/v1"
	assert frappe.msgprint_calls and "removed" in repr(frappe.msgprint_calls)
	assert "URL-PASSWORD-CANARY" not in repr(frappe.msgprint_calls)


@pytest.mark.parametrize("url", ["http://169.254.169.254/v1", "ftp://host/v1", "http://[::1", "http://host:bad", 123, {}, None])
def test_refused_base_url_still_saves_with_explanation(settings_doc, url):
	doc, _, frappe = settings_doc
	doc.ai_base_url = url
	doc.validate()
	assert frappe.msgprint_calls
	assert "not allowed" in repr(frappe.msgprint_calls)
	assert all(row["indicator"] == "orange" for row in frappe.msgprint_calls)
	assert "********" not in repr(frappe.msgprint_calls)


def test_keyless_lan_provider_saves_without_warning(settings_doc):
	doc, before, frappe = settings_doc
	doc.ai_base_url = before.ai_base_url = "http://10.0.0.5:11434/v1"
	doc.ai_api_key = ""
	doc.validate()
	assert frappe.msgprint_calls == []


def test_existing_lan_key_is_kept_but_transport_warning_is_visible(settings_doc):
	doc, before, frappe = settings_doc
	doc.ai_base_url = before.ai_base_url = "http://10.0.0.5:11434/v1"
	doc.validate()
	assert doc.ai_api_key == "********"
	assert "will not be sent" in repr(frappe.msgprint_calls)
	assert "optimus_ai_allow_key_over_http" in repr(frappe.msgprint_calls)


@pytest.mark.parametrize("field,value", [("ai_provider", "OpenAI"), ("ai_base_url", "https://other.invalid/v1"),
	("ai_base_url", "https://model.invalid/V1"), ("ai_base_url", "https://model.invalid:8443/v1")])
def test_endpoint_change_clears_unchanged_stored_key(settings_doc, field, value):
	doc, _, frappe = settings_doc
	setattr(doc, field, value)
	doc.validate()
	assert doc.ai_api_key == ""
	assert "key was cleared" in repr(frappe.msgprint_calls)


@pytest.mark.parametrize("url", ["https://model.invalid/v1/", "HTTPS://MODEL.INVALID/v1", "https://model.invalid:443/v1"])
def test_equivalent_endpoint_keeps_stored_key(settings_doc, url):
	doc, _, frappe = settings_doc
	doc.ai_base_url = url
	doc.validate()
	assert doc.ai_api_key == "********" and frappe.msgprint_calls == []


def test_hosted_provider_ignores_hidden_endpoint_change(settings_doc):
	doc, before, frappe = settings_doc
	doc.ai_provider = before.ai_provider = "OpenAI"
	doc.ai_base_url = "http://169.254.169.254/v1"
	doc.validate()
	assert doc.ai_api_key == "********" and frappe.msgprint_calls == []


@pytest.mark.parametrize("first_save", [True, False])
def test_explicit_new_key_or_first_save_is_preserved(settings_doc, first_save):
	doc, _, frappe = settings_doc
	doc.ai_base_url = "https://other.invalid/v1"
	doc.ai_api_key = "Mq7Rt2Vx" * 4
	if first_save:
		doc.get_doc_before_save = lambda: None
	doc.validate()
	assert doc.ai_api_key == "Mq7Rt2Vx" * 4
	assert doc.ai_api_key not in repr(frappe.msgprint_calls)


def test_url_check_preserves_rq_interrupt(settings_doc, monkeypatch):
	doc, _, _ = settings_doc
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	interrupt = Timeout("fake timeout")
	def fail(*a, **k):
		raise interrupt
	monkeypatch.setattr(ai_fix, "validate_base_url", fail)
	with pytest.raises(Timeout) as caught:
		doc.validate()
	assert caught.value is not interrupt and caught.value.__context__ is None
