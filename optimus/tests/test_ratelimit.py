# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""optimus.ratelimit: a per-user fixed window that nothing in the request can reset (K10).

Frappe's @rate_limit(key=...) reads ``key`` as a form field name, so its bucket is per IP plus
that field's value and a caller could open a fresh bucket by sending a new value.
"""

import inspect
import sys

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


def test_frappe_rate_limit_user_based_canary():
	"""Frappe #42815 adds ``@rate_limit(user_based=True)``; Frappe 16.18 does not have it. When
	this fails, the installed Frappe has it: consider moving optimus.ratelimit onto it (keep the
	counted-after-the-gate order) and delete this canary. Skipped on the baseline CI stub."""
	rate_limiter = sys.modules.get("frappe.rate_limiter")
	if rate_limiter is None:
		rate_limiter = pytest.importorskip("frappe.rate_limiter")
	if not getattr(rate_limiter, "__file__", None):
		pytest.skip("baseline frappe stub: the canary needs a real Frappe")
	params = inspect.signature(rate_limiter.rate_limit).parameters
	assert "user_based" not in params, (
		"Frappe now ships @rate_limit(user_based=True) (frappe/frappe#42815); see the "
		"optimus.ratelimit module docstring"
	)
