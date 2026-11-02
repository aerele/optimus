# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""optimus.ratelimit: a per-user fixed window that nothing in the request can reset (K10).

Frappe's @rate_limit(key=...) reads ``key`` as a form field name, so its bucket is per IP plus
that field's value and a caller could open a fresh bucket by sending a new value.
"""

import ast
from pathlib import Path

import pytest

from optimus import ratelimit, redis_keys
from optimus.tests.gate_fakes import FakeRateLimitExceededError, install, make_fake_frappe


def _fake(monkeypatch, *, user="a@example.com", conf=None):
	fake = make_fake_frappe(user=user, conf=conf)
	install(monkeypatch, fake)
	return fake


def _key(action, user, window):
	return "site|" + redis_keys.user_rate_limit(action, user, window)


def test_key_builder_shape_and_inventory():
	assert (
		redis_keys.user_rate_limit("refill_ai_suggestions", "a@example.com", 3600)
		== "optimus:ratelimit:refill_ai_suggestions:3600:a@example.com"
	)
	assert "optimus:ratelimit:<action>:<window>:<user>" in redis_keys.KEY_PATTERNS


def test_allows_up_to_the_limit_then_raises_429(monkeypatch):
	fake = _fake(monkeypatch)
	ratelimit.enforce_user_rate_limit("refill_ai_suggestions", limit=2, seconds=3600)
	ratelimit.enforce_user_rate_limit("refill_ai_suggestions", limit=2, seconds=3600)
	with pytest.raises(FakeRateLimitExceededError):
		ratelimit.enforce_user_rate_limit("refill_ai_suggestions", limit=2, seconds=3600)
	assert fake.spies.throws[-1]["title"] == "Optimus"
	assert fake.cache.store[_key("refill_ai_suggestions", "a@example.com", 3600)] == 3
	# Only incr / ttl / expire touch Redis: plain set/get would bypass the per-site prefix.
	assert {c[0] for c in fake.cache.calls} <= {"incr", "ttl", "expire"}


def test_first_call_opens_the_window_with_a_ttl(monkeypatch):
	fake = _fake(monkeypatch)
	ratelimit.enforce_user_rate_limit("regenerate_reports", limit=30, seconds=60)
	key = _key("regenerate_reports", "a@example.com", 60)
	assert fake.cache.calls == [("incr", key), ("expire", key, 60)]
	assert fake.cache.ttls[key] == 60


def test_later_calls_keep_the_window(monkeypatch):
	fake = _fake(monkeypatch)
	ratelimit.enforce_user_rate_limit("regenerate_reports", limit=30, seconds=60)
	ratelimit.enforce_user_rate_limit("regenerate_reports", limit=30, seconds=60)
	assert [c[0] for c in fake.cache.calls] == ["incr", "expire", "incr", "ttl"]


def test_counter_without_a_ttl_is_rearmed(monkeypatch):
	fake = _fake(monkeypatch)
	key = _key("retry_analyze", "a@example.com", 60)
	fake.cache.store[key] = 3  # INCR ran, then the worker died before EXPIRE
	ratelimit.enforce_user_rate_limit("retry_analyze", limit=5, seconds=60)
	assert fake.cache.store[key] == 4
	assert fake.cache.ttls.get(key) == 60  # without the re-arm this user is locked out forever


def test_form_dict_nonce_cannot_open_a_fresh_bucket(monkeypatch):
	fake = _fake(monkeypatch)
	for nonce in ("n1", "n2"):
		fake.form_dict["optimus_refill_ai_suggestions"] = nonce
		ratelimit.enforce_user_rate_limit("refill_ai_suggestions", limit=2, seconds=3600)
	fake.form_dict["optimus_refill_ai_suggestions"] = "n3"
	with pytest.raises(FakeRateLimitExceededError):
		ratelimit.enforce_user_rate_limit("refill_ai_suggestions", limit=2, seconds=3600)
	incr_keys = {c[1] for c in fake.cache.calls if c[0] == "incr"}
	assert incr_keys == {_key("refill_ai_suggestions", "a@example.com", 3600)}


def test_buckets_are_per_user(monkeypatch):
	fake = _fake(monkeypatch, user="a@example.com")
	ratelimit.enforce_user_rate_limit("refill_ai_suggestions", limit=1, seconds=3600)
	with pytest.raises(FakeRateLimitExceededError):
		ratelimit.enforce_user_rate_limit("refill_ai_suggestions", limit=1, seconds=3600)
	fake.session.user = "b@example.com"
	ratelimit.enforce_user_rate_limit("refill_ai_suggestions", limit=1, seconds=3600)  # b has its own bucket


def test_buckets_are_per_action(monkeypatch):
	_fake(monkeypatch)
	ratelimit.enforce_user_rate_limit("refill_ai_suggestions", limit=1, seconds=3600)
	ratelimit.enforce_user_rate_limit("regenerate_reports", limit=1, seconds=3600)


def test_site_config_override_wins(monkeypatch):
	fake = _fake(monkeypatch, conf={"optimus_rate_limits": {"refill_ai_suggestions": [1, 5]}})
	ratelimit.enforce_user_rate_limit("refill_ai_suggestions", limit=20, seconds=3600)
	with pytest.raises(FakeRateLimitExceededError):
		ratelimit.enforce_user_rate_limit("refill_ai_suggestions", limit=20, seconds=3600)
	assert _key("refill_ai_suggestions", "a@example.com", 5) in fake.cache.store


@pytest.mark.parametrize("override", ["20/600", [20], [0, 60], [5, -1], ["x", 60], {"limit": 5}, None])
def test_malformed_override_falls_back(monkeypatch, override):
	fake = _fake(monkeypatch, conf={"optimus_rate_limits": {"refill_ai_suggestions": override}})
	ratelimit.enforce_user_rate_limit("refill_ai_suggestions", limit=2, seconds=3600)
	assert _key("refill_ai_suggestions", "a@example.com", 3600) in fake.cache.store


def test_non_dict_override_block_falls_back(monkeypatch):
	fake = _fake(monkeypatch, conf={"optimus_rate_limits": "refill_ai_suggestions=1"})
	ratelimit.enforce_user_rate_limit("refill_ai_suggestions", limit=2, seconds=3600)
	assert _key("refill_ai_suggestions", "a@example.com", 3600) in fake.cache.store


def _api_tree() -> ast.Module:
	return ast.parse((Path(ratelimit.__file__).parent / "api.py").read_text(encoding="utf-8"))


def test_optimus_keeps_count_after_gate_limiter():
	"""Decision lock: Optimus keeps its own ``enforce_user_rate_limit`` and does not move onto
	Frappe's ``@rate_limit(user_based=True)`` (frappe#42815, in Frappe 16 since 16.18).

	Frappe's version is a decorator, so it runs before the function body and counts every call,
	including one the permission gate denies and one a misconfigured site rejects. Optimus calls
	``enforce_user_rate_limit`` inside each endpoint after its gate passed, so a denied call never
	uses up a user's budget. A decorator cannot keep that count-after-the-gate order; a future
	migration would silently let a denied or probing caller exhaust a legitimate user's budget.
	"""
	assert callable(ratelimit.enforce_user_rate_limit)
	tree = _api_tree()
	decorators = [
		decorator
		for node in ast.walk(tree)
		if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
		for decorator in node.decorator_list
	]
	for decorator in decorators:
		assert "rate_limit" not in ast.unparse(decorator), (
			"an endpoint is decorated with a rate limit; Optimus counts after the gate instead "
			"(see test_optimus_keeps_count_after_gate_limiter)"
		)
	assert "user_based" not in ast.unparse(tree)
	counted = {
		node.args[0].value
		for node in ast.walk(tree)
		if isinstance(node, ast.Call)
		and ast.unparse(node.func) == "ratelimit.enforce_user_rate_limit"
		and node.args
		and isinstance(node.args[0], ast.Constant)
	}
	# the AI actions are counted by _ai_session_gate (a variable action), the rest by the endpoint
	assert {"start", "stop", "download_pdf", "test_ai_connection", "check_cli_session"} <= counted
	assert "ratelimit.enforce_user_rate_limit(action, **_AI_LIMITS[action])" in ast.unparse(tree)


def test_ai_gate_counts_after_the_gate_checks():
	"""The count sits last in ``_ai_session_gate``, after the permission and availability checks."""
	gate = next(
		node for node in ast.walk(_api_tree()) if isinstance(node, ast.FunctionDef) and node.name == "_ai_session_gate"
	)
	in_order = sorted(
		(node for node in ast.walk(gate) if isinstance(node, ast.Call)), key=lambda n: (n.lineno, n.col_offset)
	)
	calls = [ast.unparse(node.func) for node in in_order]
	assert calls.index("_session_action_gate") < calls.index("ratelimit.enforce_user_rate_limit")
	assert calls.index("ai_fix.is_available") < calls.index("ratelimit.enforce_user_rate_limit")
