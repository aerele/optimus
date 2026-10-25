# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the subscription (local CLI) API surface: the cheap
``ai_fix.subscription_mode_active`` predicate, the ``check_cli_session``
Connect-button endpoint, the bulk endpoints (no longer blocked in subscription
mode) and the connection-aware "AI isn't ready" message. The frappe boundary is replaced with a MagicMock
(same pattern as ``test_api_refill_ai_suggestions``)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from optimus import ai_fix, api

SUBSCRIPTION = "Subscription (local CLI)"


@pytest.fixture
def fake_frappe(monkeypatch):
	"""Patch the auth + lookup boundary: a Ready session owned by the caller."""
	monkeypatch.setattr(api, "_require_profiler_user", lambda: "user@example.com")
	fake = MagicMock()
	fake.db.get_value.return_value = {
		"name": "opt-xxx", "user": "user@example.com", "status": "Ready", "title": "t",
	}
	fake.get_roles.return_value = ["System Manager"]
	fake.utils.cint = int

	def _throw(msg, exc=Exception):
		raise exc(msg)

	fake.throw.side_effect = _throw
	fake.PermissionError = PermissionError
	monkeypatch.setattr(api, "frappe", fake)
	monkeypatch.setattr(api, "_", lambda s: s)
	return fake


def _set_cfg(monkeypatch, **attrs):
	monkeypatch.setattr("optimus.settings.get_config", lambda: SimpleNamespace(**attrs))


# ---------------------------------------------------------------- predicate


def test_subscription_mode_active_true_for_subscription(monkeypatch):
	_set_cfg(monkeypatch, ai_connection_type=SUBSCRIPTION)
	assert ai_fix.subscription_mode_active() is True


def test_subscription_mode_active_tolerates_whitespace(monkeypatch):
	_set_cfg(monkeypatch, ai_connection_type="  " + SUBSCRIPTION + " ")
	assert ai_fix.subscription_mode_active() is True


@pytest.mark.parametrize("value", ["API key", "", None, "Subscription"])
def test_subscription_mode_active_false_otherwise(monkeypatch, value):
	_set_cfg(monkeypatch, ai_connection_type=value)
	assert ai_fix.subscription_mode_active() is False


def test_subscription_mode_active_false_when_attr_missing(monkeypatch):
	_set_cfg(monkeypatch)
	assert ai_fix.subscription_mode_active() is False


def test_subscription_mode_active_false_on_error(monkeypatch):
	def boom():
		raise RuntimeError("settings unreadable")

	monkeypatch.setattr("optimus.settings.get_config", boom)
	assert ai_fix.subscription_mode_active() is False


# ---------------------------------------------------------- check_cli_session


def test_check_cli_session_denies_non_system_manager(fake_frappe, monkeypatch):
	fake_frappe.get_roles.return_value = ["Optimus User"]
	status = MagicMock()
	monkeypatch.setattr(ai_fix, "_cli_session_status", status)

	with pytest.raises(PermissionError):
		api.check_cli_session()

	status.assert_not_called()
	fake_frappe.db.set_value.assert_not_called()


def test_check_cli_session_returns_status_and_persists_truncated_message(fake_frappe, monkeypatch):
	long_msg = "x" * 300
	result = {"ok": True, "state": "connected", "message": long_msg, "cli": "/bin/codex", "version": "1"}
	monkeypatch.setattr(ai_fix, "_cli_session_status", lambda: result)
	commit = MagicMock()
	monkeypatch.setattr(api, "safe_commit", commit)

	out = api.check_cli_session()

	assert out is result
	fake_frappe.db.set_value.assert_called_once_with(
		"Optimus Settings", "Optimus Settings", "ai_subscription_status", "x" * 140
	)
	commit.assert_called_once()


def test_check_cli_session_allows_administrator_without_role(fake_frappe, monkeypatch):
	monkeypatch.setattr(api, "_require_profiler_user", lambda: "Administrator")
	fake_frappe.get_roles.return_value = []
	monkeypatch.setattr(
		ai_fix, "_cli_session_status",
		lambda: {"ok": False, "state": "missing", "message": "not installed"},
	)
	monkeypatch.setattr(api, "safe_commit", MagicMock())

	assert api.check_cli_session()["state"] == "missing"


def test_check_cli_session_persist_failure_still_returns_status(fake_frappe, monkeypatch):
	result = {"ok": True, "state": "connected", "message": "ok"}
	monkeypatch.setattr(ai_fix, "_cli_session_status", lambda: result)
	fake_frappe.db.set_value.side_effect = RuntimeError("db down")

	assert api.check_cli_session() is result
	fake_frappe.log_error.assert_called_once()


# ------------------------------------------- bulk endpoints (no guard)


@pytest.fixture
def bulk_calls(monkeypatch):
	"""Subscription mode on; the availability check is a spy that reports not
	ready, so a bulk endpoint stops right after it and no backfill work runs."""
	def _must_not_run(*a, **k):
		raise AssertionError("bulk AI work ran although AI is not available")

	monkeypatch.setattr("optimus.analyze._run_ai_backfill", _must_not_run)
	monkeypatch.setattr(api, "_humanize_steps_core", _must_not_run)
	monkeypatch.setattr(api, "_refill_indexes_for_doc", _must_not_run)
	monkeypatch.setattr(api, "regenerate_reports", _must_not_run)
	monkeypatch.setattr(ai_fix, "subscription_mode_active", lambda: True)
	available = MagicMock(return_value=False)
	monkeypatch.setattr(ai_fix, "is_available", available)
	return available


@pytest.mark.parametrize(
	"endpoint", ["backfill_ai_fixes", "refill_ai_suggestions"]
)
def test_bulk_endpoints_do_not_short_circuit_on_subscription_mode(fake_frappe, bulk_calls, endpoint):
	with pytest.raises(Exception) as excinfo:
		getattr(api, endpoint)(session_uuid="sess-1")

	# Subscription mode no longer blocks bulk: the endpoint reaches the normal
	# availability check and fails there, not on a subscription-specific throw.
	# The message is the connection-aware one (Codex wording in subscription mode).
	assert "Bulk AI suggestions aren't available" not in str(excinfo.value)
	assert "Codex CLI isn't ready" in str(excinfo.value)
	bulk_calls.assert_called_once_with()


# ------------------------------------------------- _ai_unavailable_message


def test_unavailable_message_in_subscription_mode(monkeypatch):
	monkeypatch.setattr(api, "_", lambda s: s)
	monkeypatch.setattr(ai_fix, "subscription_mode_active", lambda: True)
	msg = api._ai_unavailable_message()
	assert "Codex CLI" in msg
	assert "optimus_allow_subscription_cli" in msg
	assert "API key" not in msg


def test_unavailable_message_in_api_key_mode(monkeypatch):
	monkeypatch.setattr(api, "_", lambda s: s)
	monkeypatch.setattr(ai_fix, "subscription_mode_active", lambda: False)
	msg = api._ai_unavailable_message()
	assert "provider, model and API key" in msg
	assert "Codex" not in msg


@pytest.mark.parametrize("endpoint,kwargs", [
	("suggest_fix", {"session_uuid": "s", "finding_ref": "0"}),
	("humanize_steps", {"session_uuid": "s"}),
	("suggest_index", {"session_uuid": "s", "table_name": "tabX"}),
])
def test_on_demand_endpoints_use_connection_aware_message(fake_frappe, monkeypatch, endpoint, kwargs):
	monkeypatch.setattr(api, "_require_session_permission", lambda *a, **k: "opt-xxx")
	monkeypatch.setattr(ai_fix, "subscription_mode_active", lambda: True)
	monkeypatch.setattr(ai_fix, "is_available", lambda *a, **k: False)

	fn = getattr(api, endpoint)
	fn = getattr(fn, "__wrapped__", fn)
	with pytest.raises(Exception) as excinfo:
		fn(**kwargs)

	assert "Codex CLI isn't ready" in str(excinfo.value)


# ---------------------------------------------- rate limits + ai_backfill


def _api_source():
	from pathlib import Path

	return (Path(api.__file__)).read_text()


@pytest.mark.parametrize(
	"key,limit,fn",
	[
		("optimus_refill_ai", 5, "refill_ai_suggestions"),
		("optimus_backfill_ai", 5, "backfill_ai_fixes"),
		("optimus_check_cli", 10, "check_cli_session"),
	],
)
def test_bulk_and_connect_endpoints_are_rate_limited(key, limit, fn):
	import re

	pattern = (
		r"@frappe\.whitelist\(\)\s*\n"
		rf'@rate_limit\(key="{key}", limit={limit}, seconds=60\)\s*\n'
		rf"def {fn}\("
	)
	assert re.search(pattern, _api_source())


def test_endpoints_that_did_ai_work_skip_the_second_findings_backfill():
	import re

	src = _api_source()
	for fn in ("backfill_ai_fixes", "humanize_steps", "suggest_index", "_refill_ai_sections"):
		start = src.index(f"def {fn}(")
		nxt = re.search(r"\n(?:def |@frappe\.whitelist|@contextlib)", src[start + 10:])
		body = src[start:start + 10 + nxt.start()]
		assert "regenerate_reports(session_uuid, ai_backfill=False)" in body, fn


def test_regenerate_reports_signature_is_backward_compatible():
	import inspect

	sig = inspect.signature(api.regenerate_reports)
	assert list(sig.parameters) == ["session_uuid", "ai_backfill"]
	assert sig.parameters["ai_backfill"].default is True
