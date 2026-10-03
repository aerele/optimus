"""Upgrade notices contain no stored endpoint, password or provider text."""

import importlib
from types import SimpleNamespace

import pytest

from optimus import ai_fix

pytestmark = pytest.mark.rq


@pytest.fixture
def upgrade(monkeypatch):
	import frappe

	module = importlib.import_module("optimus.patches.v0_12_0.warn_ai_endpoint_policy")
	values = {"ai_enabled": 1, "ai_provider": "OpenAI-compatible", "ai_base_url": "http://10.0.0.5:11434/v1", "ai_api_key": "********"}
	monkeypatch.setattr(frappe, "db", SimpleNamespace(get_single_value=lambda dt, field: values.get(field)))
	monkeypatch.setattr(ai_fix, "_allow_key_over_http", lambda: False)
	return module, values


def test_remote_http_with_key_warns_on_upgrade(upgrade, capsys):
	module, _ = upgrade
	module.execute()
	output = capsys.readouterr().out
	assert "will not be sent" in output and "optimus_ai_allow_key_over_http" in output
	assert "10.0.0.5" not in output and "********" not in output


@pytest.mark.parametrize("value", ["http://169.254.169.254/v1", "https://operator:URL-PASSWORD-CANARY@host/v1"])
def test_refused_endpoint_has_fixed_warning(upgrade, capsys, value):
	module, values = upgrade
	values["ai_base_url"] = value
	module.execute()
	output = capsys.readouterr().out
	assert "not allowed" in output and "Test AI connection" in output
	assert value not in output and "URL-PASSWORD-CANARY" not in output


@pytest.mark.parametrize("field,value", [("ai_enabled", 0), ("ai_provider", "OpenAI"), ("ai_api_key", ""),
	("ai_base_url", "https://model.invalid/v1"), ("ai_base_url", "http://127.0.0.1:11434/v1")])
def test_no_change_to_safe_or_keyless_config(upgrade, capsys, field, value):
	module, values = upgrade
	values[field] = value
	module.execute()
	assert capsys.readouterr().out == ""


def test_explicit_http_opt_in_needs_no_warning(upgrade, capsys, monkeypatch):
	module, _ = upgrade
	monkeypatch.setattr(ai_fix, "_allow_key_over_http", lambda: True)
	module.execute()
	assert capsys.readouterr().out == ""


def test_unavailable_settings_does_not_abort_migration(upgrade, monkeypatch, capsys):
	import frappe

	module, _ = upgrade
	def fail(*a, **k):
		raise RuntimeError("PRIVATE-DB-CANARY")
	monkeypatch.setattr(frappe, "db", SimpleNamespace(get_single_value=fail))
	module.execute()
	output = capsys.readouterr().out
	assert "could not be checked" in output and "PRIVATE-DB-CANARY" not in output


def test_upgrade_preserves_job_interrupt(upgrade, monkeypatch):
	import frappe

	module, _ = upgrade
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	interrupt = Timeout("fake timeout")
	def fail(*a, **k):
		raise interrupt
	monkeypatch.setattr(frappe, "db", SimpleNamespace(get_single_value=fail))
	with pytest.raises(Timeout) as caught:
		module.execute()
	assert caught.value is not interrupt and caught.value.__context__ is None
