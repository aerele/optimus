# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""T15: the failure log lines name the error type and log at ERROR (a production site drops
lower levels), a gate or evidence-read crash leaves one line, the warnings and toasts say
where to look, and the runbook covers the signals."""

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from optimus import ai_fix, ai_grounding, analyze, safe_call
from optimus.renderer import index_recipes, recipe_enrichment

_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _read(*parts):
	with open(os.path.join(_ROOT, *parts), encoding="utf-8") as f:
		return f.read()


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


def test_failure_line_without_pairs_is_the_old_line(lines):
	recipe_enrichment.log_recipe_failures(2)
	assert lines == ["optimus: index advice failed for 2 finding(s) or table(s) in one render"]


def test_each_advisor_site_reports_its_error_type(monkeypatch):
	monkeypatch.setattr(index_recipes, "advise_finding", _boom)
	monkeypatch.setattr(index_recipes, "advise_table", _boom)
	errors = []
	finding = {"finding_type": "Full Table Scan", "technical_detail": {"table": "tabSales Invoice"}}
	recipe_enrichment.apply_finding_recipes([finding], evidence_lookup=lambda t: None, errors=errors)
	table = {"table": "tabSales Invoice", "recommended_index": {"columns": ["a"]}}
	recipe_enrichment.apply_table_recipes([table], evidence_lookup=lambda t: None, errors=errors)
	assert errors == [("Full Table Scan", "RuntimeError"), ("tabSales Invoice", "RuntimeError")]


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


def test_evidence_read_lines_are_capped(lines, monkeypatch):
	monkeypatch.setattr(recipe_enrichment, "_read_table_evidence", _boom)
	lookup = recipe_enrichment.make_evidence_lookup()
	for i in range(25):
		lookup(f"tabT{i}")
	assert len(lines) == recipe_enrichment.MAX_LOGGED_EVIDENCE_FAILURES


def _ctx(findings):
	return SimpleNamespace(session_uuid="u", docname=None, warnings=[], actions=[], findings=findings)


def test_index_only_gating_writes_no_note():
	findings = [{"finding_type": t, "severity": "High", "technical_detail_json": "{}"} for t in ("Missing Index", "Full Table Scan")]
	ctx = _ctx(findings)
	cfg = SimpleNamespace(
		ai_enabled=True, ai_suggest_findings=True, ai_auto_suggest=True, ai_excluded_finding_types=(),
		ai_auto_suggest_max=5, tracked_apps=(), ai_max_findings=5,
	)
	with patch("optimus.settings.get_config", return_value=cfg), patch("optimus.ai_fix.is_available", return_value=True):
		analyze._enrich_findings_with_ai_suggestions(ctx)
	assert ctx.warnings == []


def test_warnings_name_the_setting_the_menu_and_the_error_log_title():
	source = _read("optimus", "analyze.py")
	assert "Generate AI fixes\" button" not in source and "use 'Generate AI fixes'" not in source
	assert "AI > Refresh AI suggestions" in source
	assert 'title \\"optimus ai auto-suggest\\"' in source
	assert "ai_excluded_finding_types)" not in source
	assert "Excluded finding types" in source


def test_refresh_toasts_say_where_to_look():
	js = _read("optimus", "optimus", "doctype", "optimus_session", "optimus_session.js")
	assert "failed, old suggestions kept (see Error Log, title optimus ai backfill)" in js
	assert "skipped (time budget). Run it again" in js
	assert "were not sent to the AI. Their own advice or note is in the report." in js
	assert "Excluded finding types" in js
	assert "get advice or a note from Optimus" not in js


def test_texts_use_the_label_not_the_fieldname():
	assert "Excluded finding types" in _read("optimus", "ai_fix.py")
	assert "See the index advice on this finding" in ai_grounding.INDEX_TYPE_NOTE
	assert "recipe" not in ai_grounding.INDEX_TYPE_NOTE


def test_app_scope_docstring_is_true_about_timeouts():
	doc = ai_fix._app_scope.__doc__
	assert "job timeouts propagate" not in doc and "swallowed" in doc


def test_the_runbook_covers_each_signal():
	text = _read("docs", "AI-FIXING.md")
	section = text[text.index("### 6.5 Troubleshooting"):text.index("## 7. Threat model")]
	for needle in (
		"| Signal | What it means | What to do |", "could not build index advice", "optimus: index advice failed",
		"optimus: evidence read failed", "was not created", "skipped on", "Optimus could not check this Hot Line",
		"optimus: hot-line gate failed", "optimus ai backfill", "gated", "excluded", "heartbeat",
	):
		assert needle in section, needle



def test_a_failed_read_does_not_rewrite_other_no_code_reasons(lines, monkeypatch):
	monkeypatch.setattr(recipe_enrichment, "_read_table_evidence", _boom)
	lookup = recipe_enrichment.make_evidence_lookup()
	lookup("tabSales Invoice")  # the read failed for this table
	query = "SELECT name FROM `tabSales Invoice` WHERE customer = ? " + "AND status = ? " * 2000
	finding = {"finding_type": "Full Table Scan", "technical_detail": {"table": "tabSales Invoice", "normalized_query": query}}
	advice = index_recipes.advise_finding(finding, evidence_lookup=lookup)
	assert "is longer than" in advice.reason and "could not read the details" not in advice.reason
