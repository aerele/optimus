"""Bound saved-answer validation without changing storage or paid refresh eligibility."""

import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from optimus import ai_guardrails as G
from optimus import ai_prompts as P
from optimus.renderer import _internal, finding_enrichment, recipe_enrichment
from optimus.tests.test_ai_selection_contracts import pick, row
from optimus.tests.test_renderer_structure_snapshot import _finding
from optimus.tests.test_renderer_v3 import _fake_doc

UNSAFE = '**Diagnosis**: Repeated reads.\n**Fix**\n```python\nfrappe.db.sql(query)\n```\n**Why it works**: Fewer reads.\n**Verify**: Re-profile.'


def saved(text=UNSAFE, **fields):
	return dict(suggestion=text, prompt_version=P.PROMPT_VERSION, **fields)


def test_first_fit_caps_memo_and_version_short_circuit(monkeypatch):
	monkeypatch.setattr(G, "RENDER_VALIDATION_CHARS", 8)
	monkeypatch.setattr(G, "RENDER_VALIDATION_ANSWERS", 2)
	checked = []
	monkeypatch.setattr(G, "_stored_policy_violations", lambda text: checked.append(text) or [])
	with G.render_validation_budget():
		assert P.fix_state(saved("old"), current_version=P.PROMPT_VERSION + 1)[0] == P.FIX_OUTDATED
		assert G.render_limited(G.stored_policy_violations("too large"))
		assert not G.stored_policy_violations("hello")
		assert not G.stored_policy_violations("hello")
		assert not G.stored_policy_violations("yes")
		assert G.render_limited(G.stored_policy_violations(""))
		budget = G._render_budget.get()
		assert budget.chars == budget.answers == 0
		assert checked == ["hello", "yes"]
		assert all(re.fullmatch("[a-f0-9]{64}", key) for key in budget.memo)
		assert "hello" not in repr(budget.memo) and "too large" not in repr(budget.memo)
		# Callers receive a fresh list, not a mutable memo value.
		G.stored_policy_violations("hello").append(G.Violation("raw-sql"))
		assert not G.stored_policy_violations("hello")
	assert G._render_budget.get() is None


def test_eligibility_and_display_reuse_the_same_check(monkeypatch):
	monkeypatch.setattr(G, "RENDER_VALIDATION_ANSWERS", 1)
	checked = []
	check = G._stored_policy_violations
	monkeypatch.setattr(G, "_stored_policy_violations", lambda text: checked.append(text) or check(text))
	with G.render_validation_budget():
		assert P.fix_state(saved())[0] == P.FIX_OUTDATED
		for _ in range(2):
			finding = finding_enrichment._finding_to_dict(_finding(llm_fix_json=json.dumps(saved())))
			assert finding["llm_fix"]["policy_codes"] == ["raw-sql"]
		assert checked == [UNSAFE]


def test_limited_is_withheld_without_outdated_or_automatic_retry(monkeypatch):
	monkeypatch.setattr(G, "RENDER_VALIDATION_ANSWERS", 0)
	child = _finding(llm_fix_json=json.dumps(saved()))
	before = child.llm_fix_json
	with G.render_validation_budget():
		assert P.fix_state(child.llm_fix_json)[0] == P.FIX_PROCESSING_LIMITED
		assert pick([row("f", child.llm_fix_json)]) == []
		assert pick([row("f", child.llm_fix_json)], include_outdated=False) == []
		finding = finding_enrichment._finding_to_dict(child)
		recipe_enrichment.mark_outdated_ai_fixes([finding], refresh_check=lambda f: True)
		fix = finding["llm_fix"]
		assert fix["completion_status"] == P.FIX_PROCESSING_LIMITED
		assert fix["review_status"] == "Response needs local review"
		assert not fix["outdated"] and not fix["policy_outdated"] and not fix["refreshable"]
		assert "could not be checked safely" in fix["suggestion_html"]
		assert "frappe.db.sql" not in fix["suggestion_html"]
	assert child.llm_fix_json == before
	# No persisted receipt or eligibility change escapes the render context.
	assert P.fix_state(child.llm_fix_json)[0] == P.FIX_OUTDATED
	assert pick([row("f", child.llm_fix_json)]) == ["f"]
	assert pick([row("f", child.llm_fix_json)], include_outdated=False) == []


def test_generation_is_never_budgeted(monkeypatch):
	monkeypatch.setattr(G, "RENDER_VALIDATION_ANSWERS", 0)
	with G.render_validation_budget():
		assert G.render_limited(G.stored_policy_violations(UNSAFE))
		codes = {v.code for v in G.verify_fix(UNSAFE, source_lines=[])}
		assert "raw-sql" in codes and G.VALIDATION_LIMITED not in codes


def test_nested_render_and_exception_cleanup(monkeypatch):
	monkeypatch.setattr(G, "RENDER_VALIDATION_ANSWERS", 1)
	with pytest.raises(ValueError, match="recordings"):
		_internal.render(_fake_doc())
	assert G._render_budget.get() is None
	with G.render_validation_budget():
		G.stored_policy_violations("one")
		outer = G._render_budget.get()
		_internal.render(_fake_doc(), [])
		assert G._render_budget.get() is outer
		assert G.render_limited(G.stored_policy_violations("two"))
	assert G._render_budget.get() is None


def test_concurrent_render_and_non_render_isolation(monkeypatch):
	monkeypatch.setattr(G, "RENDER_VALIDATION_ANSWERS", 1)
	barrier = threading.Barrier(3)

	def render_work():
		with G.render_validation_budget():
			assert not G.stored_policy_violations("first")
			barrier.wait(timeout=10)
			assert G.render_limited(G.stored_policy_violations(UNSAFE))
			barrier.wait(timeout=10)
		return G._render_budget.get()

	with ThreadPoolExecutor(max_workers=2) as pool:
		futures = [pool.submit(render_work) for _ in range(2)]
		barrier.wait(timeout=10)
		assert G._render_budget.get() is None
		assert "raw-sql" in {v.code for v in G.stored_policy_violations(UNSAFE)}
		barrier.wait(timeout=10)
		assert [f.result(timeout=10) for f in futures] == [None, None]


def test_two_thousand_answer_report_has_bounded_validation_and_no_side_effects(monkeypatch):
	import frappe

	from optimus import ai_fix

	def forbidden(*args, **kwargs):
		raise AssertionError("render attempted a write or provider call")

	for name in ("set_value", "sql", "commit", "insert", "delete"):
		monkeypatch.setattr(frappe.db, name, forbidden, raising=False)
	monkeypatch.setattr(ai_fix, "_http_post", forbidden)
	# Many AST statements approach the per-answer work envelope; every answer
	# differs and has exactly 16 KiB, so the char cap admits exactly 16.
	prefix = "**Diagnosis**: Repeated reads.\n**Fix**\n```python\n" + "frappe.db.sql(query)\n" * 300
	children = []
	for i in range(2000):
		head = prefix + f"# answer {i}\n"
		text = head + "#" * (16384 - len(head) - len("\n```")) + "\n```"
		children.append(_finding(llm_fix_json=json.dumps(saved(text))))
	before = [child.llm_fix_json for child in children]
	checked, limited = [], []
	total_seconds = 0.0
	check = G._stored_policy_violations
	gate = G.stored_policy_violations

	def measured(text):
		nonlocal total_seconds
		start = time.monotonic()
		try:
			return check(text)
		finally:
			total_seconds += time.monotonic() - start
			checked.append(hashlib.sha256(text.encode()).hexdigest())

	def observed(text):
		result = gate(text)
		if G.render_limited(result):
			limited.append(hashlib.sha256(text.encode()).hexdigest())
		return result

	monkeypatch.setattr(G, "_stored_policy_violations", measured)
	monkeypatch.setattr(G, "stored_policy_violations", observed)
	doc = _fake_doc(findings=children)
	first = _internal.render(doc, [], generated_at="2026-10-18T00:00:00")
	assert len(checked) == 16 and len(limited) == 1984
	assert total_seconds <= 2.0, total_seconds
	assert "Response needs local review" in first
	assert first.count("could not be checked safely") == 1984
	assert "Outdated AI suggestion" not in first
	assert "frappe.db.sql" not in first
	assert [child.llm_fix_json for child in children] == before
	sets = checked[:], limited[:]
	checked.clear()
	limited.clear()
	_internal.render(doc, [], generated_at="2026-10-18T00:00:00")
	assert (checked, limited) == sets
	assert G._render_budget.get() is None
