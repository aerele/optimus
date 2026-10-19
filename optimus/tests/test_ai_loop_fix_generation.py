"""N+1 answers must change repeated reads, not merely rename the database API."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from optimus import ai_fix
from optimus import ai_guardrails as G
from optimus import ai_prompts as P

H = "**Diagnosis**: repeated query.\n**Fix**\n{}\n**Why it works**: one query.\n**Verify**: query count drops from 50 to 1."
SOURCE = [
	"def work(deadline):",
	"    while time.monotonic() < deadline:",
	"        try:",
	'            user = frappe.get_doc("User", frappe.session.user)',
	"            if user.enabled:",
	"                for _ in range(3):",
	"                    _maybe_log_user(user, iters)",
	"            frappe.db.sql(",
	'                "SELECT name, email FROM `tabUser` LIMIT 20", as_dict=True,',
	"            )",
	"        except Exception:",
	"            pass",
]
PASTED = H.format('''```diff
         try:
             user = frappe.get_doc("User", frappe.session.user)
             if user.enabled:
                 for _ in range(3):
                     _maybe_log_user(user, iters)
-            frappe.db.sql(
-                "SELECT name, email FROM `tabUser` LIMIT 20", as_dict=True,
-            )
+            users = frappe.get_all("User", fields=["name", "email"], limit=20)
+            for user_row in users:
+                pass  # dummy loop to avoid unused var warning
         except Exception:
             pass
```''')


def answer(before, after):
	return H.format("```diff\n" + "\n".join(["-" + s for s in before] + ["+" + s for s in after]) + "\n```")


def run_answer(monkeypatch, *texts, reask=True, elapsed=0, repair_error=False):
	from optimus.ai_fix import Usage
	from optimus.tests.test_ai_fix import TestGuardedCompletion, _FakeResp, _post_sequence

	runner = TestGuardedCompletion()
	runner._FINDING = {**runner._FINDING, "source_window": [
		{"lineno": 1 + i, "content": line, "is_target": i == 7} for i, line in enumerate(SOURCE)]}
	usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
	responses = [runner._resp(text, usage=usage) for text in texts]
	if repair_error:
		responses.append(_FakeResp(503, {"error": {"message": "unavailable"}}))
	fake = _post_sequence(*responses)
	clock = [0]
	def post(*args, **kwargs):
		response = fake(*args, **kwargs)
		clock[0] += elapsed
		return response
	monkeypatch.setattr(requests, "post", post)
	monkeypatch.setattr(ai_fix.time, "monotonic", lambda: clock[0])
	monkeypatch.setattr(ai_fix, "_get_api_key", lambda needs_key=True, **kw: "sk-test")
	monkeypatch.setattr(ai_fix, "_reask_enabled", lambda: reask)
	totals = Usage()
	with patch("optimus.ai_fix._provider_config", return_value=dict(runner._PROVIDER)):
		result = ai_fix.suggest_fix(dict(runner._FINDING), usage_out=totals)
	return result, fake, totals


def test_pasted_api_rewrite_is_not_published_as_a_fix(monkeypatch):
	result, fake, totals = run_answer(monkeypatch, PASTED, PASTED)
	assert result["guardrail"]["fallback"] is True
	assert result["guardrail"]["reasked"] is True
	assert len(fake.calls) == 2 and totals["total_tokens"] == 300
	assert [r["text"] for r in result["received_responses"]] == [PASTED, PASTED]
	assert not G.rendered_blocks(result["suggestion"])
	assert "50 to 1" not in result["suggestion"]
	assert "query" in result["suggestion"].lower()


@pytest.mark.parametrize("case_name", ["3q1ln8bv2o"])
def test_captured_sql_api_rewrite_triggers_correction(case_name):
	corpus = json.loads((Path(__file__).parent / "fixtures/ai_corpus_2026-09-09.json").read_text())
	case = next(c for c in corpus["cases"] if c["name"] == case_name)
	vs = G.verify_fix(case["suggestion"], source_lines=case["source_lines"], finding_type="N+1 Query")
	assert G.reaskable(vs) and G.strips_code(vs)


@pytest.mark.parametrize("header", ["while ready:", "for name in names:", "async for name in names:"])
@pytest.mark.parametrize("old", ['frappe.db.sql("SELECT name FROM `tabUser`")', 'frappe.get_doc("User", name)'])
def test_uncached_read_still_in_same_loop_is_blocked(header, old):
	before = [header, "    " + old]
	after = [header, '    rows = frappe.get_all("User", fields=["name"], limit=20)']
	vs = G.verify_fix(answer(before, after), source_lines=before, finding_type="N+1 Query")
	assert "ineffective-loop-fix" in {v.code for v in G.reaskable(vs)}
	assert G.strips_code(vs)


@pytest.mark.parametrize("before,after", [
	(['for name in names:', '    rows = frappe.get_doc("User", name)'],
	 ['for name in names:', '    rows = frappe.get_all("User", filters={"name": ("in", names)})']),
	(['while ready:', '    frappe.db.sql(query)'],
	 ['while ready:', '    for _ in [1]:', '        frappe.get_all("User")']),
	(['while ready:', '    frappe.get_all("User")'],
	 ['while ready:', '    for _ in [1]:', '        frappe.get_all("User")']),
	(['while ready:', '    frappe.get_all("User")'],
	 ['while ready:', '    users = frappe.get_all("User")', '    for user in users:', '        pass']),
	(['from frappe import db', 'for name in names:', '    db.sql(query)'],
	 ['from frappe import db', 'for name in names:', '    rows = frappe.get_all("User")']),
	(['import frappe as f', 'for name in names:', '    f.get_doc("User", name)'],
	 ['import frappe as f', 'for name in names:', '    f.get_all("User")']),
	(['for name in names:', '    rows = frappe.get_doc("User", name)'],
	 ['rows = [frappe.get_all("User") for name in names]']),
	(['while frappe.db.exists("User", name):', '    work()'],
	 ['while frappe.get_all("User"):', '    work()']),
	(['rows = [frappe.get_doc("User", name) for name in names]'],
	 ['rows = [frappe.get_all("User") for name in names]']),
	(['rows = [r for name in names for r in frappe.db.get_values("User", name)]'],
	 ['rows = [r for name in names for r in frappe.get_all("User")]']),
	(['rows = [r for name in names if frappe.db.exists("User", name) for r in results]'],
	 ['for name in names:', '    if frappe.get_all("User"):', '        for r in results:', '            use(r)']),
	(['for name in names:', '    doc = frappe.get_doc("User", name)'],
	 ['for name in names:', '    memo = {}', '    if name not in memo:', '        memo[name] = frappe.db.get_value("User", name)']),
	(['for name in names:', '    doc = frappe.get_doc("User", name)'],
	 ['memo = {}', 'for name in names:', '    memo.clear()', '    if name not in memo:', '        memo[name] = frappe.db.get_value("User", name)']),
	(['for name in names:', '    doc = frappe.get_doc("User", name)'],
	 ['memo = {}', 'for name in names:', '    memo[name] = None', '    if name not in memo:', '        memo[name] = frappe.db.get_value("User", name)']),
	(['for name in names:', '    doc = frappe.get_doc("User", name)'],
	 ['memo = {}', 'for name in names:', '    alias = memo', '    alias.clear()', '    if name not in memo:', '        memo[name] = frappe.db.get_value("User", name)']),
	(['def work():', '    for name in names:', '        doc = frappe.get_doc("User", name)'],
	 ['def fetch(name):', '    return frappe.get_all("User", filters={"name": name})', 'def work():', '    for name in names:', '        doc = fetch(name)']),
	(['for name in names:', '    doc = frappe.get_doc("User", name)'],
	 ['docs = list(map(lambda name: frappe.get_doc("User", name), names))']),
	(['for name in names:', '    doc = frappe.get_doc("User", name)'],
	 ['docs = list(map(frappe.get_doc, names))']),
	(['for name in names:', '    doc = frappe.get_doc("User", name)'],
	 ['from frappe import get_doc as fetch', 'docs = list(map(fetch, names))']),
	(['for name in names:', '    doc = frappe.get_doc("User", name)'],
	 ['memo = {}', 'for name in names:', '    if name not in memo:', '        pass', '    else:', '        memo[name] = frappe.db.get_value("User", name)']),
])
def test_disguised_repeated_reads_are_rejected(before, after):
	vs = G.verify_fix(answer(before, after), source_lines=before, finding_type="N+1 Query")
	assert {v.code for v in vs} & {"ineffective-loop-fix", "unverified-loop-fix"}
	assert G.strips_code(vs)


@pytest.mark.parametrize("after", [
	['rows = frappe.get_all("User", fields=["name"])', 'for name in names:', '    use(rows)'],
	['for name in names:', '    use(name)'],
	['for name in names:', '    doc = frappe.get_cached_doc("User", name)'],
	['memo = {}', 'for name in names:', '    if name not in memo:', '        memo[name] = frappe.db.get_value("User", name)'],
	['memo = {}', 'for name in names:', '    if name not in memo:', '        memo[name] = frappe.db.get_value("User", name)', '    use(memo.get(name))'],
	['memo = {}', 'for name in names:', '    if name not in memo:', '        memo[name] = frappe.get_doc("User", name)'],
	['for doc in frappe.get_all("User", filters={"name": ("in", names)}):', '    use(doc)'],
	['rows = [doc for doc in frappe.get_all("User", filters={"name": ("in", names)})]'],
])
def test_structural_reductions_are_kept(after):
	before = ['for name in names:', '    doc = frappe.get_doc("User", name)']
	vs = G.verify_fix(answer(before, after), source_lines=before, finding_type="N+1 Query")
	assert not G.reaskable(vs) and not G.strips_code(vs)


def test_batch_outside_inner_loop_but_inside_outer_loop_is_kept():
	before = ['while ready:', '    for name in names:', '        doc = frappe.get_doc("User", name)']
	after = ['while ready:', '    rows = frappe.get_all("User", filters={"name": ("in", names)})', '    for row in rows:', '        use(row)']
	vs = G.verify_fix(answer(before, after), source_lines=before, finding_type="N+1 Query")
	assert not G.strips_code(vs)


def test_other_unchanged_reads_do_not_block_a_valid_hoist():
	before = ['for name in names:', '    doc = frappe.get_doc("User", name)', '    rows = frappe.get_all("Role")']
	after = ['rows = frappe.get_all("Role")', 'for name in names:', '    doc = frappe.get_doc("User", name)']
	vs = G.verify_fix(answer(before, after), source_lines=before, finding_type="N+1 Query")
	assert not G.strips_code(vs)


@pytest.mark.parametrize("header", ['for name in names:', 'while ready:'])
def test_loop_else_is_not_repeated(header):
	before = [header, '    work()', 'else:', '    doc = frappe.get_doc("User", name)']
	after = [header, '    work()', 'else:', '    doc = frappe.db.get_value("User", name)']
	vs = G.verify_fix(answer(before, after), source_lines=before)
	assert "loop-query" not in {v.code for v in vs}


def test_full_replacement_needs_a_grounded_diff():
	before = ['while ready:', '    frappe.db.sql(query)']
	text = H.format('```python\nwhile ready:\n    rows = frappe.get_all("User")\n```')
	vs = G.verify_fix(text, source_lines=before, finding_type="N+1 Query")
	assert "unverified-loop-fix" in {v.code for v in vs}


def test_full_replacement_without_source_cannot_claim_batching():
	text = H.format('```python\nwhile ready:\n    frappe.get_all("User")\n```')
	vs = G.verify_fix(text, source_lines=[], finding_type="N+1 Query")
	assert "unverified-loop-fix" in {v.code for v in vs}


def test_incomplete_python_replacement_cannot_keep_claimed_savings():
	before = ['while ready:', '    frappe.db.sql(query)']
	text = H.format('```python\nwhile ready:\n    rows = frappe.get_all("User")\n    ...rest unchanged...\n```')
	vs = G.verify_fix(text, source_lines=before, finding_type="N+1 Query")
	assert "unverified-loop-fix" in {v.code for v in G.reaskable(vs)}
	assert "50 to 1" not in G.apply_fallback(text, vs)


def test_verification_read_and_bench_snippets_do_not_reject_valid_batching():
	before = ['for name in names:', '    doc = frappe.get_doc("User", name)']
	after = ['docs = frappe.get_all("User", filters={"name": ("in", names)})', 'for doc in docs:', '    use(doc)']
	text = answer(before, after) + '\n```python\ndoc = frappe.get_doc("Sales Invoice", "SINV-1")\n```\n```bash\nbench --site test execute app.work\n```'
	vs = G.verify_fix(text, source_lines=before, finding_type="N+1 Query")
	assert not G.reaskable(vs) and not G.strips_code(vs)


def test_directional_answer_without_code_is_kept():
	text = H.format('Show the caller loop before batching these reads.')
	assert not G.reaskable(G.verify_fix(text, source_lines=[], finding_type="N+1 Query"))


def test_existing_saved_answers_are_not_invalidated_or_mutated():
	fix = dict(suggestion=PASTED, prompt_version=4, completion_status="complete")
	before = json.dumps(fix)
	assert not G.stored_policy_violations(PASTED)
	assert P.fix_state(fix)[0] == "current"
	assert json.dumps(fix) == before


@pytest.mark.parametrize("options,posts,reasked", [
	({"reask": False}, 1, False),
	({"elapsed": 35}, 1, False),
	({"repair_error": True}, 2, True),
])
def test_unavailable_repair_withholds_code_and_claims(monkeypatch, options, posts, reasked):
	result, fake, totals = run_answer(monkeypatch, PASTED, **options)
	assert len(fake.calls) == posts
	assert result["guardrail"]["reasked"] is reasked
	assert result["guardrail"]["fallback"]
	assert not G.rendered_blocks(result["suggestion"])
	assert "50 to 1" not in result["suggestion"]
	assert totals["total_tokens"] == 150


def test_successful_directional_repair_is_adopted_and_both_answers_are_retained(monkeypatch):
	good = (
		"**Diagnosis**: This appears to be deliberate profiling workload.\n"
		"**Fix**: Without the full caller contract, review the intended workload before removing repeated reads.\n"
		"**Why it works**: Changing the query API inside the loop does not reduce executions.\n"
		"**Verify**: Re-profile total database calls for the same workload."
	)
	result, fake, totals = run_answer(monkeypatch, PASTED, good)
	assert len(fake.calls) == 2 and totals["total_tokens"] == 300
	assert result["guardrail"] == {"violations": [], "reasked": True, "fallback": False}
	assert result["suggestion"] == good
	assert [r["text"] for r in result["received_responses"]] == [PASTED, good]
	assert "enclosing loop" in fake.calls[1].body["messages"][-1]["content"]


def test_repair_is_checked_again_with_n_plus_one_type(monkeypatch):
	before = SOURCE
	after = [*SOURCE[:7], '            users = frappe.get_all("User", fields=["name", "email"], limit=20)', *SOURCE[10:]]
	still_bad = answer(before, after)
	result, fake, _ = run_answer(monkeypatch, PASTED, still_bad)
	assert len(fake.calls) == 2
	assert result["guardrail"]["fallback"]
	assert not G.rendered_blocks(result["suggestion"])
	assert "50 to 1" not in result["suggestion"]


def test_report_shows_direction_without_rejected_patch_or_savings(monkeypatch):
	from optimus import renderer
	from optimus.tests.test_finding_card_ai_fix import _doc, _finding

	result, _, _ = run_answer(monkeypatch, PASTED, PASTED)
	page = renderer.render_raw(_doc([_finding(llm_fix=result)]), recordings=[])
	assert "No query-count reduction has been verified." in page
	assert "dummy loop to avoid unused var warning" not in page
	assert "50 to 1" not in page
	assert P.fix_state(result)[0] == "current"
