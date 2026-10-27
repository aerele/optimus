# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Expensive session actions count per-user limits before doing any work."""

from types import SimpleNamespace

import pytest

from optimus import api
from optimus.tests.gate_fakes import SESSION_UUID, FakeRateLimitExceededError, FakeValidationError
from optimus.tests.test_api_phase2_endpoints import PICKS, _env


@pytest.mark.parametrize("endpoint,kwargs", [
    ("start", {}), ("stop", {}),
    ("start_line_profile_pass", {"session_uuid": SESSION_UUID, "picks": PICKS}),
    ("stop_line_profile_pass", {"run_uuid": "run-1"}),
    ("retry_phase2_analyze", {"run_uuid": "run-1"}),
])
def test_rate_limited_action_has_no_side_effects(monkeypatch, endpoint, kwargs):
    fake, _, seen = _env(monkeypatch, run_status="Recording" if endpoint == "stop_line_profile_pass" else "Failed")
    monkeypatch.setattr(api, "session", SimpleNamespace(get_active_session_for=lambda user: SESSION_UUID))
    counted = []
    def denied(action, **limits):
        counted.append(action)
        raise FakeRateLimitExceededError("too many")
    monkeypatch.setattr(api.ratelimit, "enforce_user_rate_limit", denied)
    with pytest.raises(FakeRateLimitExceededError):
        getattr(api, endpoint)(**kwargs)
    assert counted == [endpoint]
    assert seen.saved == seen.capture == seen.analyzed == []
    assert fake.spies.enqueue == fake.spies.set_value == fake.spies.sql == []


@pytest.mark.parametrize("count", [6, 101])
def test_batch_limits_work_before_iterating(monkeypatch, count):
    _env(monkeypatch)
    called = []
    monkeypatch.setattr(api, "retry_phase2_analyze", lambda run_uuid: called.append(run_uuid))
    with pytest.raises(FakeValidationError):
        api.retry_phase2_analyzes_batch(["fake-run"] * count)
    assert called == []


def test_batch_deduplicates_runs(monkeypatch):
    _env(monkeypatch)
    called = []
    def retry(run_uuid):
        called.append(run_uuid)
        return {"run_uuid": run_uuid, "status": "Ready"}
    monkeypatch.setattr(api, "retry_phase2_analyze", retry)
    result = api.retry_phase2_analyzes_batch(["fake-run", "fake-run"])
    assert called == ["fake-run"] and result["count"] == 1


def test_batch_cannot_swallow_the_per_user_limit(monkeypatch):
    _env(monkeypatch)
    def denied(run_uuid):
        raise FakeRateLimitExceededError("too many")
    monkeypatch.setattr(api, "retry_phase2_analyze", denied)
    with pytest.raises(FakeRateLimitExceededError):
        api.retry_phase2_analyzes_batch(["fake-run"])


def test_batch_accepts_five_runs(monkeypatch):
    _env(monkeypatch)
    called = []
    def retry(run_uuid):
        called.append(run_uuid)
        return {"run_uuid": run_uuid, "status": "Ready"}
    monkeypatch.setattr(api, "retry_phase2_analyze", retry)
    runs = [f"fake-run-{i}" for i in range(5)]
    assert api.retry_phase2_analyzes_batch(runs)["count"] == 5
    assert called == runs


def test_batch_and_single_retry_share_the_same_limit(monkeypatch):
    fake, _, seen = _env(monkeypatch, run_status="Failed")
    fake.conf["optimus_rate_limits"] = {"retry_phase2_analyze": [1, 60]}
    api.retry_phase2_analyze("run-1")
    with pytest.raises(FakeRateLimitExceededError):
        api.retry_phase2_analyzes_batch(["run-1"])
    assert len(seen.analyzed) == len(seen.saved) == 1
