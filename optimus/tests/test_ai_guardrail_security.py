# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Guardrail retries preserve the AI client's security and usage contracts."""

from types import SimpleNamespace

import pytest
import requests

from optimus import ai_fix, settings
from optimus.tests.test_ai_fix import TestGuardedCompletion as _Replies
from optimus.tests.test_ai_fix import _post_sequence

pytestmark = [pytest.mark.rq, pytest.mark.usefixtures("bound_provider_credentials")]


@pytest.mark.parametrize("keyless", [False, True])
def test_guarded_reask_reads_key_once_per_completion(monkeypatch, keyless):
    import frappe

    reads = []
    def read_key():
        reads.append(True)
        return "invalid\nkey" if keyless else "test-key-" + "Mq7Rt2Vx" * 4

    monkeypatch.setattr(ai_fix, "_current_key_or_empty", read_key)
    monkeypatch.setattr(frappe, "db", SimpleNamespace(get_single_value=lambda *a: "********"))
    monkeypatch.setattr(settings, "get_config", lambda: SimpleNamespace(
        ai_provider="OpenAI-compatible" if keyless else "OpenAI",
        ai_model="test-model", ai_base_url="https://fake.invalid/v1", ai_context_tokens=128000,
    ))
    monkeypatch.setattr(ai_fix, "_reask_enabled", lambda: True)
    fake = _post_sequence(_Replies._resp(_Replies._RAW), _Replies._resp(_Replies._GOOD))
    monkeypatch.setattr(requests, "post", fake)
    result = ai_fix.suggest_fix(dict(_Replies._FINDING), timeout=47)
    assert result["guardrail"] == {"violations": [], "reasked": True, "fallback": False}
    assert result["finish_reason"] == "stop"
    assert len(fake.calls) == len(reads) == 2
    assert all(0 < call.timeout[0] <= 10 and 0 < call.timeout[1] <= 47 for call in fake.calls)
    assert all((call.auth is None) == keyless for call in fake.calls)


def test_anthropic_cached_usage_does_not_reclamp_valid_sum():
    count = ai_fix._MAX_TOKEN_COUNT
    usage = ai_fix._usage_from_anthropic({"usage": {
        "input_tokens": count, "cache_creation_input_tokens": count,
        "cache_read_input_tokens": count, "output_tokens": 4,
    }})
    assert usage == {"prompt_tokens": 3 * count, "completion_tokens": 4, "total_tokens": 3 * count + 4}


def test_empty_reply_error_carries_only_numeric_usage():
    error = ai_fix.AiFixError("empty", usage={
        "prompt_tokens": 10, "completion_tokens": "unsafe text", "total_tokens": 10,
        "unexpected": "unsafe text",
    })
    assert error.usage == {"prompt_tokens": 10, "total_tokens": 10}


@pytest.mark.rq
def test_logged_reask_timeout_keeps_marker_without_failed_frames(monkeypatch):
    timeouts = pytest.importorskip("rq.timeouts", exc_type=ImportError)
    failure = timeouts.JobTimeoutException("fake timeout")
    ai_fix._mark_logged(failure, "fake-log-row")
    replies = iter([_Replies._RAW, failure])
    def dispatch(*args, **kwargs):
        reply = next(replies)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(ai_fix, "_dispatch_call", dispatch)
    monkeypatch.setattr(ai_fix, "_reask_enabled", lambda: True)
    with pytest.raises(timeouts.JobTimeoutException) as caught:
        ai_fix._complete_with_guardrails(
            dict(_Replies._PROVIDER), "system", [], shown_lines=[
                line["content"] for line in _Replies._FINDING["source_window"]
            ], usage={}, started_at=ai_fix.time.monotonic(), timeout=60,
        )
    assert caught.value is not failure
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert getattr(caught.value, ai_fix._LOGGED_ATTR, False)
    assert getattr(caught.value, ai_fix._LOGGED_ROW_ATTR, None) == "fake-log-row"
    tb = caught.value.__traceback__
    while tb:
        assert tb.tb_frame.f_code.co_name != "dispatch"
        if tb.tb_frame.f_code.co_name == "_complete_with_guardrails":
            assert all(value is not failure for value in tb.tb_frame.f_locals.values())
        tb = tb.tb_next
