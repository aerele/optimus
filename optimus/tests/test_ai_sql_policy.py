"""Offline regressions for the SQL hoist observed in the user's saved report."""

import copy
import json

import pytest

from optimus import ai_guardrails as G
from optimus import ai_prompts as P

H = "**Diagnosis**: the loop repeats unused reads.\n**Fix**\n{}\n**Why it works**: fewer reads.\n**Verify**: re-profile."
SOURCE = [
    "    for _ in range(25):",
    '        frappe.db.sql("SELECT 1")',
    '        frappe.db.sql("SELECT count(*) FROM `tabUser`")',
    "        json.loads(json.dumps(payload))",
]
HOIST = H.format("```diff\n" + "\n".join("-" + line for line in SOURCE) + '\n+    frappe.db.sql("SELECT 1")\n+    frappe.db.sql("SELECT count(*) FROM `tabUser`")\n+    json.loads(json.dumps(payload))\n```')


def stored(text=HOIST, **kw):
    return dict(suggestion=text, prompt_version=P.PROMPT_VERSION, completion_status="complete", **kw)


def test_saved_two_query_hoist_is_rejected_and_correction_is_actionable():
    violations = G.verify_fix(HOIST, source_lines=SOURCE, finish_reason="stop")
    assert "raw-sql" in {v.code for v in violations}
    correction = G.reask_message(violations)
    assert "get_value" in correction and "count" in correction
    assert "moved" in correction
    assert not G.rendered_blocks(G.apply_fallback(HOIST, violations))


@pytest.mark.parametrize("call", ["frappe.db.sql", "frappe.db.multisql", "frappe.db.sql_list", "frappe.db.sql_ddl", "frappe.local.db.sql", "frappe.local.db.sql_list", "db.sql"])
def test_moving_sql_variants_is_rejected(call):
    source = ["db = frappe.db", "for _ in range(25):", f"    {call}(query)"]
    body = f"```diff\n db = frappe.db\n-for _ in range(25):\n-    {call}(query)\n+{call}(query)\n```"
    assert "raw-sql" in {v.code for v in G.verify_fix(H.format(body), source_lines=source)}


def test_malformed_fragment_cannot_exempt_moved_sql():
    source = ["else:", '    frappe.db.sql("select 1")']
    body = '```diff\n else:\n-    frappe.db.sql("select 1")\n+frappe.db.sql("select 1")\n```'
    assert "raw-sql" in {v.code for v in G.verify_fix(H.format(body), source_lines=source)}


@pytest.mark.parametrize("call", ["frappe.db.sql", "db.sql"])
def test_unparseable_fragment_with_kept_opener_still_checks_arguments(call):
    source = ["db = frappe.db", "else:", f"    {call}(", "        query, values", "    )"]
    body = f"```diff\n db = frappe.db\n else:\n     {call}(\n-        query, values\n+        other_query, values\n     )\n```"
    assert "raw-sql" in {v.code for v in G.verify_fix(H.format(body), source_lines=source)}


def test_unparseable_fragment_keeps_unmodified_sql_context():
    source = ["else:", "    frappe.db.sql(query)", "    old_work()"]
    body = '```diff\n else:\n     frappe.db.sql(query)\n-    old_work()\n+    new_work()\n```'
    assert not G.strips_code(G.verify_fix(H.format(body), source_lines=source))


@pytest.mark.parametrize("replacement", [
    'value = frappe.db.get_value("User", name, "email")',
    'rows = frappe.db.get_values("User", {"enabled": 1}, ["name", "email"])',
    'rows = frappe.get_all("User", fields=["name", "email"], order_by="")',
    'rows = frappe.get_list("User", fields=["name"])',
    'count = frappe.db.count("User")',
    'exists = frappe.db.exists("User", name)',
    'rows = frappe.qb.from_(User).select(User.name).run()',
])
def test_frappe_apis_remain_accepted(replacement):
    body = H.format('```diff\n-frappe.db.sql(query)\n+' + replacement + '\n```')
    assert not G.verify_fix(body, source_lines=["frappe.db.sql(query)"])
    assert P.fix_state(stored(body))[0] == "current"


@pytest.mark.parametrize("body", [
    '```diff\n-frappe.db.sql(query)\n```',
    '```diff\n frappe.db.sql(query)\n-old_work()\n+new_work()\n```',
    '```python\nmessage = "Do not call frappe.db.sql(query)"\n```',
])
def test_removed_context_and_literal_mentions_stay_current(body):
    assert P.fix_state(stored(H.format(body)))[0] == "current"


def test_only_affected_saved_answers_become_outdated_without_mutation():
    fix = stored(received_responses=[{"text": HOIST}], tokens={"total_tokens": 2426})
    before = copy.deepcopy(fix)
    assert P.fix_state(fix)[0] == "outdated"
    assert P.fix_state(json.dumps(fix))[0] == "outdated"
    assert fix == before
    assert P.fix_state(stored(H.format("Use the existing index.")))[0] == "current"


def test_original_sql_quoted_in_diagnosis_does_not_invalidate_a_good_fix():
    text = H.format('```diff\n-frappe.db.sql(query)\n+value = frappe.db.get_value("User", name, "email")\n```')
    text = text.replace("**Diagnosis**:", "**Diagnosis**\n```python\nfrappe.db.sql(query)\n```\n")
    assert not G.verify_fix(text, source_lines=["frappe.db.sql(query)"])
    assert P.fix_state(stored(text))[0] == "current"


def test_plain_replacement_cannot_disguise_a_sql_hoist_as_quoted_source():
    text = H.format('```python\nfrappe.db.sql(query)\n```')
    assert "raw-sql" in {v.code for v in G.verify_fix(text, source_lines=["    frappe.db.sql(query)"])}


def test_initial_and_repair_prompts_have_no_sql_hoist_exception():
    assert "SQL you only move stays as it is" not in P.SYSTEM_PROMPT
    assert "keep raw SQL on purpose" not in P.REASK_FOOTER
    assert "Never add, move or change raw SQL" in P.SYSTEM_PROMPT
    assert "Preserve try/except" in P.SYSTEM_PROMPT
