# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Regressions from the follow-up review of the suggestion guardrails."""

import pytest
import requests

from optimus import ai_budget, ai_fix
from optimus import ai_guardrails as guard
from optimus.tests.test_ai_fix import TestGuardedCompletion as Replies
from optimus.tests.test_ai_fix import _post_sequence, _provider
from optimus.tests.test_ai_guardrails import H


def violations(body, source=()):
    return {v.code for v in guard.verify_fix(H.format(body), source_lines=list(source))}


@pytest.mark.parametrize("label", ["sql", "text", "json", "bash", "shell", "console", "sh", "python"])
@pytest.mark.parametrize(("code", "expected"), [
    ("eval(user_input)", "eval-exec"),
    ("pickle.loads(blob)", "unsafe-deserialize"),
    ("frappe.db.commit()", "manual-commit"),
    ("doc.save(ignore_permissions=True)", "ignore-permissions"),
    ("frappe.db.sql(query)", "raw-sql"),
])
def test_fence_language_cannot_disable_python_safety(label, code, expected):
    text = H.format(f"```{label}\n{code}\n```")
    found = guard.verify_fix(text, source_lines=[])
    assert expected in {v.code for v in found}
    assert not guard.rendered_blocks(guard.apply_fallback(text, found))


@pytest.mark.parametrize("label,body", [
    ("sql", "SELECT name FROM tabUser"),
    ("json", '{"message": "eval(x) and pickle.loads(blob)"}'),
    ("text", "This explanation describes the existing query."),
    ("bash", 'echo "eval(x)"'),
])
def test_non_python_content_does_not_become_a_python_violation(label, body):
    assert violations(f"```{label}\n{body}\n```") == set()


def test_kept_sql_call_cannot_pay_for_a_new_call():
    source = ['rows = frappe.db.sql("select 1")']
    body = '```diff\n rows = frappe.db.sql("select 1")\n+more = frappe.db.sql("select 2")\n```'
    assert "raw-sql" in violations(body, source)


def test_plain_block_context_sql_cannot_pay_for_a_new_call():
    source = ['rows = frappe.db.sql("select 1")']
    assert "raw-sql" in violations('```python\n'+source[0]+'\nmore = frappe.db.sql("select 2")\n```', source)


def test_kept_sql_opener_can_still_have_its_parameters_fixed():
    source = ['rows = frappe.db.sql(', '    f"select {name}"', ')']
    body = '```diff\n rows = frappe.db.sql(\n-    f"select {name}"\n+    "select %(name)s", {"name": name}\n )\n```'
    assert violations(body, source) == set()


@pytest.mark.parametrize("heading", ["Diagnosis", "Why it works", "Verify"])
def test_fabricated_diff_is_grounded_in_every_section(heading):
    text = H.format("Use the batch lookup.").replace(
        f"**{heading}**", f"**{heading}**\n```diff\n-never_shown()\n+use_batch()\n```\n"
    )
    found = guard.verify_fix(text, source_lines=["actual_source()"])
    assert "ungrounded" in {v.code for v in found}
    assert not guard.rendered_blocks(guard.apply_fallback(text, found))


@pytest.mark.parametrize("option", ["False", "0", "None"])
def test_explicitly_disabled_after_commit_is_not_safe(option):
    assert "enqueue-without-after-commit" in violations(
        f'```python\nfrappe.enqueue("app.job", enqueue_after_commit={option})\n```'
    )


@pytest.mark.parametrize("options", ["enqueue_after_commit=True", "enqueue_after_commit=False, now=True"])
def test_safe_enqueue_options_keep_the_exemption(options):
    assert violations(f'```python\nfrappe.enqueue("app.job", {options})\n```') == set()


def test_zero_prompt_tokens_is_a_truncation_signal():
    assert ai_budget.context_truncated({"prompt_tokens": 0}, 9600)
    assert not ai_budget.context_truncated({}, 9600)
    assert not ai_budget.context_truncated({"prompt_tokens": 0}, 0)


@pytest.mark.parametrize("usage,expected", [({"prompt_tokens": 0, "completion_tokens": 10}, True),
                                            ({"completion_tokens": 10}, False), (None, False)])
def test_completion_distinguishes_zero_usage_from_missing_usage(monkeypatch, usage, expected):
    fake = _post_sequence(Replies._resp(Replies._GOOD, usage=usage))
    monkeypatch.setattr(requests, "post", fake)
    with _provider(dict(Replies._PROVIDER, context_tokens=8192)):
        result = ai_fix.suggest_fix(dict(Replies._FINDING))
    assert ("context-truncated" in result["guardrail"]["violations"]) is expected


@pytest.mark.parametrize("context,output", [(4096, None), (8192, 700)])
def test_index_request_uses_provider_output_budget(monkeypatch, context, output):
    fake = _post_sequence(Replies._resp("Use the existing index."))
    monkeypatch.setattr(requests, "post", fake)
    provider = dict(Replies._PROVIDER, context_tokens=context, max_output_tokens=output)
    with _provider(provider):
        ai_fix.suggest_index({"table": "tabItem"})
    assert fake.calls[0].body["max_tokens"] == ai_fix._output_tokens(provider)


def test_index_request_rejects_prompt_that_cannot_fit_before_http(monkeypatch):
    sent = []
    monkeypatch.setattr(requests, "post", lambda *a, **kw: sent.append(True))
    with _provider(dict(Replies._PROVIDER, context_tokens=2048, max_output_tokens=1900)):
        with pytest.raises(ai_fix.AiFixError, match="context"):
            ai_fix.suggest_index({"table": "tabItem", "sample_queries": ["x" * 5000]})
    assert sent == []


@pytest.mark.parametrize("usage,expected", [
    ({"input_tokens": 0, "output_tokens": 10}, True),
    ({"output_tokens": 10}, False),
    ({"cache_read_input_tokens": 2000, "output_tokens": 10}, False),
])
def test_anthropic_prompt_usage_distinguishes_missing_input(monkeypatch, usage, expected):
    from optimus.tests.test_ai_fix import _FakeResp

    fake = _post_sequence(_FakeResp(200, {
        "content": [{"type": "text", "text": Replies._GOOD}], "stop_reason": "end_turn", "usage": usage,
    }))
    monkeypatch.setattr(requests, "post", fake)
    with _provider(dict(Replies._PROVIDER, protocol="anthropic", name="Anthropic", context_tokens=8192)):
        result = ai_fix.suggest_fix(dict(Replies._FINDING))
    assert ("context-truncated" in result["guardrail"]["violations"]) is expected
