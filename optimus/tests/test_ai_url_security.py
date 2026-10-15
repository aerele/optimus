"""Hostile administrator-supplied endpoints, with no DNS or provider calls."""

from types import SimpleNamespace

import pytest

from optimus import ai_fix, settings

pytestmark = pytest.mark.rq


@pytest.mark.parametrize("url", [
	None, {}, 123, "", "https:///v1", "file:///etc/passwd", "ftp://host/v1",
	"http://169.254.169.254/v1", "http://[fe80::1]/v1", "http://0.0.0.0/v1",
	"http://[::]/v1", "http://224.0.0.1/v1", "http://[ff02::1]/v1",
	"http://100.100.100.200/v1", "http://[fd00:ec2::254]/v1",
	"http://[::ffff:169.254.169.254]/v1", "http://[::ffff:0.0.0.0]/v1",
	"http://[::ffff:100.100.100.200]/v1",
	"http://metadata/v1", "https://metadata.google.internal./v1",
	"http://metadata.goog/v1", "http://instance-data.ec2.internal/v1",
	"https://operator:URL-CREDENTIAL-CANARY@host/v1", "https://@host/v1",
	"https://host/v1?token=URL-CREDENTIAL-CANARY", "https://host/v1#fragment",
	"https://host/v1?", "https://host/v1#", "http://host:0/v1",
	"http://host:65536/v1", "http://host:notaport/v1", "http://host:/v1",
	"http://[::1]suffix/v1", "http://[::1", "http://169.254.169.254\\evil/v1",
	"http://evil\\@localhost/v1", "http://%31%32%37.0.0.1/v1",
	"http://[fe80::1%25eth0]/v1", "https://hóst/v1", "https://host/a b",
	"https://host/\tsecret", "https://host/\nsecret", "https://host/\x7fsecret",
])
def test_refuses_unsafe_url_without_echo(url):
	with pytest.raises(ai_fix.AiFixError) as caught:
		ai_fix.validate_base_url(url)
	assert caught.value.kind == "config"
	assert "URL-CREDENTIAL-CANARY" not in str(caught.value)


@pytest.mark.parametrize("url,expected", [
	("  HTTPS://api.openai.com/v1/  ", "https://api.openai.com/v1"),
	("http://10.0.0.5:11434/v1/", "http://10.0.0.5:11434/v1"),
	("http://localhost:11434/v1", "http://localhost:11434/v1"),
	("http://[::1]:11434/v1", "http://[::1]:11434/v1"),
	("http://[fd12::1234]/v1", "http://[fd12::1234]/v1"),
	("https://host/private%20path/v1", "https://host/private%20path/v1"),
])
def test_allows_hosted_and_private_providers(url, expected):
	assert ai_fix.validate_base_url(url) == expected


@pytest.mark.parametrize("host", ["localhost", "model.localhost.", "127.0.0.1", "127.12.3.4", "[::1]", "[::ffff:127.0.0.1]"])
def test_loopback_can_receive_key_over_http(host):
	assert ai_fix.key_over_http_blocked(f"http://{host}:11434/v1", allow=False) is False


@pytest.mark.parametrize("host", ["10.0.0.5", "192.168.1.5", "model.internal", "[fd12::1234]", "2130706433", "0x7f000001", "127.1"])
def test_remote_and_noncanonical_hosts_require_explicit_key_opt_in(host):
	assert ai_fix.key_over_http_blocked(f"http://{host}/v1", allow=False) is True
	assert ai_fix.key_over_http_blocked(f"http://{host}/v1", allow=True) is False
	assert ai_fix.key_over_http_blocked(f"https://{host}/v1", allow=False) is False


@pytest.mark.parametrize("value,allowed", [(1, True), (True, True), ("true", True), ("1", True), (False, False), (0, False), ("false", False), ("0", False), (2, False), ([], False), ({}, False)])
def test_plain_http_opt_in_is_strict(monkeypatch, value, allowed):
	import frappe

	monkeypatch.setattr(frappe, "conf", {"optimus_ai_allow_key_over_http": value}, raising=False)
	assert ai_fix._allow_key_over_http() is allowed


def test_refused_saved_endpoint_is_unavailable_and_never_reaches_http(monkeypatch):
	monkeypatch.setattr(settings, "get_config", lambda: SimpleNamespace(ai_enabled=True, ai_provider="OpenAI-compatible", ai_base_url="http://169.254.169.254/v1", ai_model="local"))
	monkeypatch.setattr(ai_fix.requests, "post", lambda *a, **k: pytest.fail("refused URL reached transport"))
	assert ai_fix.is_available() is False
	with pytest.raises(ai_fix.AiFixError, match="not allowed"):
		ai_fix._provider_config()


def test_hosted_provider_ignores_hidden_old_base_url(monkeypatch):
	monkeypatch.setattr(settings, "get_config", lambda: SimpleNamespace(ai_provider="OpenAI", ai_base_url="http://169.254.169.254/v1", ai_model=""))
	assert ai_fix._provider_config()["base_url"] == "https://api.openai.com/v1"


def test_plain_http_config_read_preserves_job_timeout(monkeypatch):
	import frappe

	JobTimeoutException = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	interrupt = JobTimeoutException("timeout")
	class Conf:
		def get(self, key):
			raise interrupt
	monkeypatch.setattr(frappe, "conf", Conf(), raising=False)
	with pytest.raises(JobTimeoutException) as caught:
		ai_fix._allow_key_over_http()
	assert caught.value is not interrupt
	assert caught.value.__context__ is None
