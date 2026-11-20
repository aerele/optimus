# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The failure log lines name the error type and log at ERROR (a production site drops
lower levels), a gate or evidence-read crash leaves one line, the warnings and toasts say
where to look."""

from types import SimpleNamespace

import pytest

from optimus import ai_fix, ai_grounding, safe_call
from optimus.renderer import index_recipes, recipe_enrichment


@pytest.fixture
def lines(monkeypatch):
	import frappe

	out = []

	def logger(*a, **k):
		def only_error(message):
			out.append(message)

		def fail_other(message):
			raise AssertionError("below ERROR: dropped on a production site")

		return SimpleNamespace(error=only_error, warning=fail_other, info=fail_other)

	monkeypatch.setattr(frappe, "logger", logger, raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda *a, **k: pytest.fail("log_error"), raising=False)
	return out


def _boom(*a, **k):
	raise RuntimeError("secret detail")


def test_failure_line_names_deduped_capped_pairs_at_error(lines):
	errors = [("Missing Index", "KeyError")] * 3 + [(f"tabT{i}", "ValueError") for i in range(11)]
	recipe_enrichment.log_recipe_failures(14, errors=errors)
	(line,) = lines
	assert line.startswith("optimus: index advice failed for 14 finding(s) or table(s) in one render (")
	assert "Missing Index: KeyError" in line and line.count("Missing Index") == 1
	assert "tabT8: ValueError" in line and "tabT9" not in line and line.endswith(", and 2 more)")
	assert "secret" not in line


def test_a_gate_crash_logs_one_line_in_both_gate_sites_and_still_fails_closed(lines, monkeypatch):
	monkeypatch.setattr(ai_grounding, "hot_line_gate", _boom)
	monkeypatch.setattr(ai_fix, "_app_scope", lambda: ((), None))
	finding = {"finding_type": "Hot Line", "technical_detail": {"file": "apps/x/x.py"}, "llm_fix": {"suggestion_html": "x"}}
	assert ai_fix.llm_gate_note(finding) == ai_grounding.GATE_CHECK_FAILED_NOTE
	assert ai_fix.llm_gate_note(finding) == ai_grounding.GATE_CHECK_FAILED_NOTE
	assert lines == ["optimus: hot-line gate failed: RuntimeError"]  # deduped
	safe_call._RECENT_LINES.clear()
	lines.clear()
	recipe_enrichment.apply_finding_recipes([finding], evidence_lookup=lambda t: None)
	assert lines == ["optimus: hot-line gate failed: RuntimeError"]
	assert finding["llm_fix"] is None and finding["technical_detail"]["fix_hint"] == ai_grounding.GATE_CHECK_FAILED_NOTE


def test_an_evidence_read_crash_logs_once_per_table_and_the_advice_says_so(lines, monkeypatch):
	monkeypatch.setattr(recipe_enrichment, "_read_table_evidence", _boom)
	lookup = recipe_enrichment.make_evidence_lookup()
	finding = {"finding_type": "Full Table Scan", "technical_detail": {
		"table": "tabSales Invoice", "normalized_query": "SELECT name FROM `tabSales Invoice` WHERE customer = ?",
	}}
	advice = index_recipes.advise_finding(finding, evidence_lookup=lookup)
	advice = index_recipes.advise_finding(finding, evidence_lookup=lookup)
	assert lines == ["optimus: evidence read failed for tabSales Invoice: RuntimeError"]
	text = index_recipes.finding_text(advice)
	assert "could not read the details" in text and "evidence read failed" in text
	assert "DocType on this site" not in text and "no information" not in text
	card = index_recipes.advise_table("tabSales Invoice", ["customer"], evidence_lookup=lookup)
	assert "could not read the details" in card.reason


def test_a_table_without_a_doctype_keeps_the_no_information_text(lines, monkeypatch):
	monkeypatch.setattr(recipe_enrichment, "_read_table_evidence", lambda table: None)
	lookup = recipe_enrichment.make_evidence_lookup()
	finding = {"finding_type": "Full Table Scan", "technical_detail": {
		"table": "tabSales Invoice", "normalized_query": "SELECT name FROM `tabSales Invoice` WHERE customer = ?",
	}}
	advice = index_recipes.advise_finding(finding, evidence_lookup=lookup)
	assert "has no information about table" in advice.reason and lines == []


def test_texts_use_the_label_not_the_fieldname():
	assert "See the index advice on this finding" in ai_grounding.INDEX_TYPE_NOTE
	assert "recipe" not in ai_grounding.INDEX_TYPE_NOTE


def test_a_failed_read_does_not_rewrite_other_no_code_reasons(lines, monkeypatch):
	monkeypatch.setattr(recipe_enrichment, "_read_table_evidence", _boom)
	lookup = recipe_enrichment.make_evidence_lookup()
	lookup("tabSales Invoice")  # the read failed for this table
	query = "SELECT name FROM `tabSales Invoice` WHERE customer = ? " + "AND status = ? " * 2000
	finding = {"finding_type": "Full Table Scan", "technical_detail": {"table": "tabSales Invoice", "normalized_query": query}}
	advice = index_recipes.advise_finding(finding, evidence_lookup=lookup)
	assert "is longer than" in advice.reason and "could not read the details" not in advice.reason


def test_the_dedupe_is_per_site(lines, monkeypatch):
	import frappe

	monkeypatch.setattr(frappe, "local", SimpleNamespace(site="a.local"), raising=False)
	safe_call.log_error_line("optimus: x")
	safe_call.log_error_line("optimus: x")
	monkeypatch.setattr(frappe, "local", SimpleNamespace(site="b.local"), raising=False)
	safe_call.log_error_line("optimus: x")
	assert lines == ["optimus: x", "optimus: x"]


def test_the_dedupe_table_is_bounded(lines):
	for i in range(safe_call._RECENT_LINES_CAP + 1):
		safe_call.log_error_line(f"optimus: line {i}")
	assert len(lines) == safe_call._RECENT_LINES_CAP + 1
	assert len(safe_call._RECENT_LINES) <= safe_call._RECENT_LINES_CAP
