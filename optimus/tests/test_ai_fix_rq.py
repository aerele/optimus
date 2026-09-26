# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""suggest_fix's guardrail re-ask under an RQ job timeout. Needs rq: the module skips
without it, and the ai-quality workflow (which installs rq) selects it by its name."""

from unittest.mock import patch

import pytest

from optimus import ai_fix
from optimus.tests import test_ai_fix as base

timeouts = pytest.importorskip("rq.timeouts", exc_type=ImportError)


def test_reask_rq_job_timeout_propagates_fresh(monkeypatch):
	# The job hit its timeout during the re-ask: the timeout must stop the job as a
	# fresh instance raised outside the except (no frames of the failed call, no
	# chain) and must not be logged as a failed re-ask.
	g = base.TestGuardedCompletion
	calls = {"n": 0}

	def fake_dispatch(provider, system, messages, **kw):
		calls["n"] += 1
		if calls["n"] == 1:
			kw["usage_out"].update({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
			return g._RAW
		raise timeouts.JobTimeoutException("Task exceeded maximum timeout value (180 seconds)")

	monkeypatch.setattr(ai_fix, "_dispatch_call", fake_dispatch)
	logged = []
	monkeypatch.setattr(ai_fix, "_log_reask", lambda *a, **k: logged.append(a))
	monkeypatch.setattr(ai_fix, "_reask_enabled", lambda: True)
	with patch("optimus.ai_fix._resolve_provider", return_value=dict(g._PROVIDER)):
		with pytest.raises(timeouts.JobTimeoutException) as ei:
			ai_fix.suggest_fix(dict(g._FINDING))
	assert ei.value.__context__ is None and ei.value.__cause__ is None
	frames, tb = [], ei.value.__traceback__
	while tb:
		frames.append(tb.tb_frame.f_code.co_name)
		tb = tb.tb_next
	assert frames[-1] == "_complete_with_guardrails" and "fake_dispatch" not in frames  # a fresh instance
	assert not any(a and a[0] == "failed" for a in logged)


@pytest.mark.parametrize("stage", ["knob", "logger"])
def test_guardrail_helpers_propagate_fresh_timeout(monkeypatch, stage):
    from types import SimpleNamespace

    import frappe

    failure = timeouts.JobTimeoutException("fake timeout")
    def fail(*args, **kwargs):
        raise failure
    if stage == "knob":
        monkeypatch.setattr(frappe, "conf", SimpleNamespace(get=fail), raising=False)
        call = ai_fix._reask_enabled
    else:
        monkeypatch.setattr(frappe, "logger", fail)
        def call():
            ai_fix._log_reask("skipped-fit")
    with pytest.raises(timeouts.JobTimeoutException) as exc:
        call()
    assert exc.value is not failure
    assert exc.value.__context__ is None and exc.value.__cause__ is None
    tb = exc.value.__traceback__
    while tb:
        assert tb.tb_frame.f_code.co_name != "fail"
        tb = tb.tb_next
