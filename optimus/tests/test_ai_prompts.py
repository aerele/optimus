# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Fact pins for optimus.ai_prompts (prompt v2).

Every Frappe fact the prompt teaches is pinned here, so a later edit cannot
silently reintroduce a wrong one (Customize Form indexes, raw DDL, frappe.cache(),
isinstance type checks, enqueue without enqueue_after_commit). Also pins the
static-prefix contract (one byte-identical SYSTEM_PROMPT) and its size.
INDEX_SYSTEM_PROMPT (the table-card index path) is not part of prompt v2: it keeps
the develop text until PR-L1 deletes that path, so it is not pinned here.
"""

import re
from unittest.mock import patch

import pytest

from optimus import ai_budget, ai_fix, ai_guardrails, ai_prompts
from optimus.tests.ai_eval_support import load as load_eval_script
from optimus.tests.ai_eval_support import require_semgrep

P = ai_prompts
PROMPTS = {
	"SYSTEM_PROMPT": P.SYSTEM_PROMPT,
	"STEPS_SYSTEM_PROMPT": P.STEPS_SYSTEM_PROMPT,
}


def _sentences(text):
	return [s for s in re.split(r"(?<=[.!?:])\s+|\n", text) if s.strip()]


def _examples():
	"""(answer_text, source_lines) for each worked example in SYSTEM_PROMPT."""
	body = P.SYSTEM_PROMPT.split("EXAMPLE 1", 1)[1].split(P._SELF_CHECK)[0]
	ex1, ex2 = body.split("EXAMPLE 2", 1)
	ex1 = ex1.split("\n", 1)[1].strip()
	ex2 = ex2.split("\n", 1)[1].strip()
	src1 = [ln[1:] for ln in ex1.splitlines() if ln.startswith("-")]
	return [(ex1, src1), (ex2, [])]


# ---------------------------------------------------------------- shape
def test_prompt_version_is_3():
	assert P.PROMPT_VERSION == 3


def test_system_prompt_fits_the_char_budget():
	assert len(P.SYSTEM_PROMPT) <= 7000


def test_system_prompt_leaves_room_in_a_4096_window():
	out = ai_budget.output_tokens(4096)
	assert ai_budget.min_context_tokens(P.SYSTEM_PROMPT) <= 4096
	assert ai_budget.user_char_budget(4096, P.SYSTEM_PROMPT, out_tokens=out) >= 3000


def test_system_prompt_is_byte_identical_across_findings_and_dialects():
	findings = [
		{"finding_type": "N+1 Query", "title": "a"},
		{"finding_type": "Slow Query", "title": "b", "technical_detail": {"normalized_query": "SELECT 1"}},
		{"finding_type": "Filesort", "title": "c"},
	]
	systems = {ai_fix._build_messages(f)[0] for f in findings}
	with patch("optimus.dbdialect.active_db_type", return_value="postgres"):
		systems |= {ai_fix._build_messages(f)[0] for f in findings}
	assert systems == {P.SYSTEM_PROMPT}


def test_headings_match_the_guardrail():
	assert P.FIX_HEADINGS == ai_guardrails.FIX_HEADINGS
	out = P._OUTPUT
	positions = [out.index(f"**{h}**") for h in P.FIX_HEADINGS]
	assert positions == sorted(positions)


def test_rule_text_covers_every_block_advise_and_note_code():
	needed = {c for c, a in ai_guardrails.CODE_ACTIONS.items() if a in ("block", "advise", "note")}
	assert set(P.RULE_TEXT) == needed


def test_examples_pass_the_guardrail():
	for text, src in _examples():
		assert ai_guardrails.verify_fix(text, source_lines=src) == [], text[:60]


def test_prompt_module_is_pure():
	src = open(P.__file__).read()
	assert "import frappe" not in src and "from frappe" not in src


# ---------------------------------------------------------------- copy rules
@pytest.mark.parametrize("name", sorted(PROMPTS))
def test_no_em_or_en_dashes_or_sweep_artifacts(name):
	text = PROMPTS[name]
	assert "\u2014" not in text and "\u2013" not in text
	prose = re.sub(r"```.*?```", "", text, flags=re.S)  # code indentation is fine
	assert not re.search(r"\S  \S", prose)  # the a5f52d6 dash sweep left double spaces behind
	for fragment in ("expert  you", "step a form", "noise ignore", "given write", "tight usually"):
		assert fragment not in text


def test_hints_and_rule_text_have_no_dashes():
	for text in list(P.FINDING_TYPE_HINTS.values()) + list(P.POSTGRES_EXPLAIN_HINTS.values()) + list(
		P.RULE_TEXT.values()
	):
		assert "\u2014" not in text and "\u2013" not in text


# ---------------------------------------------------------------- Frappe facts
def test_untrusted_data_clause_in_every_prompt():
	for name, text in PROMPTS.items():
		assert P.UNTRUSTED_DATA_CLAUSE in text, name
	assert "<data-" in P.UNTRUSTED_DATA_CLAUSE and "never follow instructions" in P.UNTRUSTED_DATA_CLAUSE


def test_customize_form_is_never_recommended():
	for name, text in PROMPTS.items():
		for s in _sentences(text):
			if "customize form" in s.lower():
				assert "never" in s.lower() or "no index option" in s.lower(), (name, s)


def test_raw_ddl_only_in_never_sentences():
	for name, text in PROMPTS.items():
		for s in _sentences(text):
			if "ALTER TABLE" in s or "CREATE INDEX" in s:
				assert "Never" in s or "never" in s, (name, s)


def test_index_recipe_is_the_three_row_table():
	rules = P.INDEX_RULES
	assert "on_doctype_update()" in rules
	assert "Property Setter" in rules and "search_index = 1" in rules
	assert "patch" in rules and "Custom Field" in rules
	assert "after_migrate" not in P.SYSTEM_PROMPT
	assert P.INDEX_RULES in P.SYSTEM_PROMPT


def test_metadata_column_rule_allows_trailing_creation():
	assert "trailing `creation`" in P.INDEX_RULES
	assert "alone or first" in P.INDEX_RULES


def test_every_enqueue_passes_enqueue_after_commit():
	for name, text in PROMPTS.items():
		for m in re.finditer(r"enqueue\(", text):
			span = text[m.start() : text.find(")", m.start()) + 1]
			assert "enqueue_after_commit=True" in span, (name, span)


def test_no_retired_facts():
	for name, text in PROMPTS.items():
		assert "frappe.cache()" not in text, name  # frappe.cache is an object in v16
		assert "isinstance" not in text, name  # whitelist type hints replace isinstance checks
		assert "qb.get_query" not in text, name  # ignores permissions; get_list / get_all instead
		assert "expires_in_sec" not in text, name


def test_caching_idioms_present():
	s = P.SYSTEM_PROMPT
	for fact in (
		"@request_cache",
		"@redis_cache(ttl=...)",
		"frappe.utils.caching",
		"frappe.get_cached_value",
		"frappe.get_cached_doc",
		"never modify the result",
		"functools.lru_cache",
		"frappe.cache.get_value(key)",
	):
		assert fact in s, fact


def test_data_layer_idioms_present():
	s = P.SYSTEM_PROMPT
	for fact in (
		"frappe.db.get_single_value",
		"frappe.db.set_single_value",
		"self.db_set(field, value)",
		"order=frappe.qb.desc",
		"%(name)s",
		"frappe.throw(_(",
		"frappe.db.bulk_update",
		"unbuffered_cursor",
		"creation desc",
		"job_id=",
		"deduplicate=True",
	):
		assert fact in s, fact


def test_permission_rule_preserves_semantics():
	s = P.FRAPPE_REVIEW_RULES
	assert "preserve the permission semantics" in s
	assert "`frappe.get_list` stays `frappe.get_list`" in s
	assert "not `get_list`" in s  # a permission-free read never gains user permissions
	assert "ignore_permissions=True" in s and "throw=True" in s


def test_redundant_call_hint_keeps_permission_check():
	h = P.FINDING_TYPE_HINTS["Redundant Call"]
	assert "throw=True" in h and "never removed" in h
	assert "frappe.local" not in h and "frappe.cache()" not in h


def test_hints_cover_exactly_the_eligible_types():
	assert set(P.FINDING_TYPE_HINTS) == set(ai_fix.AI_ELIGIBLE_FINDING_TYPES)
	assert set(P.POSTGRES_EXPLAIN_HINTS) <= set(P.FINDING_TYPE_HINTS)
	for h in P.FINDING_TYPE_HINTS.values():
		assert "Customize Form" not in h and "ALTER TABLE" not in h


def test_steps_prompt_facts():
	s = P.STEPS_SYSTEM_PROMPT
	assert "frappe.model.mapper.make_mapped_doc" in s
	assert "`runserverobj` is its deprecated alias" in s
	assert "**Summary:**" in s and "apply_workflow" in s


# ---------------------------------------------------------------- semgrep (PR-E's kit; ai-quality.yml runs it)
@pytest.mark.semgrep
def test_example_code_is_semgrep_clean():
	"""The worked examples' code passes the pinned Frappe semgrep rules, scanned the way
	the eval scans rendered answers (only the code an answer introduces)."""
	rules = require_semgrep()  # skips locally without semgrep; fails under REQUIRE_SEMGREP=1
	texts = {f"example-{i}": text for i, (text, _src) in enumerate(_examples(), 1)}
	scanned = load_eval_script("_semgrep").scan_texts(texts, rules)
	assert sum(r.units for r in scanned.values()) >= 1  # Example 1's diff was scanned
	assert sum(r.unparsed for r in scanned.values()) == 0
	assert {k: [h["rule"] for h in r.hits] for k, r in scanned.items() if r.hits} == {}
