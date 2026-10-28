# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The subscription (local CLI) API surface: the cheap ``ai_fix.subscription_mode_active``
predicate, the ``check_cli_session`` Connect-button endpoint and the connection-aware
"AI isn't ready" message."""

from types import SimpleNamespace

import pytest

from optimus import ai_fix, api
from optimus.tests.gate_fakes import (
	FakePermissionError,
	FakeRateLimitExceededError,
	install,
	make_fake_frappe,
)

SUBSCRIPTION = "Subscription (local CLI)"


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


@pytest.fixture
def connect(monkeypatch):
	def _make(roles, *, user="manager@example.com", conf=None, status=None):
		fake = make_fake_frappe(user=user, roles=roles, conf=conf)
		install(monkeypatch, fake)
		calls, commits = [], []
		result = status if status is not None else {"ok": True, "state": "connected", "message": "Connected."}
		monkeypatch.setattr(ai_fix, "_cli_session_status", lambda: calls.append(True) or result)
		monkeypatch.setattr(api, "safe_commit", lambda: commits.append(True))
		return fake, calls, commits, result

	return _make


def test_system_manager_gets_the_status_and_it_is_stored(connect):
	fake, calls, commits, result = connect(("System Manager",))
	assert api.check_cli_session() is result
	assert calls == [True] and commits == [True]
	args, kwargs = fake.spies.set_single_value[0]
	assert args == ("Optimus Settings", "ai_subscription_status", "Connected.")
	assert kwargs == {"update_modified": False}  # Connect must not bump the Settings timestamp
	assert fake.spies.set_value == []


def test_stored_message_is_truncated(connect):
	fake, *_ = connect(("System Manager",), status={"ok": False, "state": "error", "message": "x" * 300})
	api.check_cli_session()
	assert fake.spies.set_single_value[0][0][2] == "x" * 140


def test_non_manager_is_refused_without_probing_or_counting(connect):
	fake, calls, commits, _ = connect(("Optimus User",))
	with pytest.raises(FakePermissionError):
		api.check_cli_session()
	assert calls == [] and commits == [] and fake.spies.set_single_value == [] and fake.cache.calls == []


def test_administrator_passes_without_the_role(connect):
	_, calls, *_ = connect((), user="Administrator")
	api.check_cli_session()
	assert calls == [True]


def test_a_failed_store_still_returns_the_status(connect, monkeypatch):
	fake, _, _, result = connect(("System Manager",))

	def fail(*a, **kw):
		raise RuntimeError("db down")

	monkeypatch.setattr(fake.db, "set_single_value", fail)
	assert api.check_cli_session() is result


def test_connect_check_is_rate_limited_per_user(connect):
	connect(("System Manager",), conf={"optimus_rate_limits": {"check_cli_session": [1, 60]}})
	api.check_cli_session()
	with pytest.raises(FakeRateLimitExceededError):
		api.check_cli_session()


# ------------------------------------------------- connection-aware message


def test_unavailable_message_names_the_cli_in_subscription_mode(monkeypatch):
	install(monkeypatch, make_fake_frappe())
	_set_cfg(monkeypatch, ai_enabled=True, ai_connection_type=SUBSCRIPTION)
	msg = api._ai_unavailable_message(None)
	assert "Codex CLI" in msg and "optimus_allow_subscription_cli" in msg and "API key" not in msg


def test_unavailable_message_names_the_provider_in_api_mode(monkeypatch):
	install(monkeypatch, make_fake_frappe())
	_set_cfg(monkeypatch, ai_enabled=True, ai_connection_type="API Key")
	msg = api._ai_unavailable_message(None)
	assert "Codex" not in msg and "provider" in msg


@pytest.mark.parametrize("connection", [SUBSCRIPTION, "API Key"])
def test_unavailable_message_when_ai_is_off_does_not_mention_connect(monkeypatch, connection):
	install(monkeypatch, make_fake_frappe())
	_set_cfg(monkeypatch, ai_enabled=False, ai_connection_type=connection)
	msg = api._ai_unavailable_message(None)
	assert "Enable AI fix suggestions" in msg and "Connect" not in msg and "Codex" not in msg


def test_section_messages_are_unchanged_in_subscription_mode(monkeypatch):
	install(monkeypatch, make_fake_frappe())
	_set_cfg(monkeypatch, ai_connection_type=SUBSCRIPTION)
	assert "Fix suggestions on findings" in api._ai_unavailable_message("findings")
	assert "Steps to Reproduce" in api._ai_unavailable_message("humanize")
