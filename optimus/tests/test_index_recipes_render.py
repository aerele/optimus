# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Render-time recipes (recipe_enrichment wired into render_raw): recipes fill
the existing fix-hint / code / table-card slots, retired AI output is gone and
raw DDL never reaches the report. Tasks 6a and 9a append the pre-fix
Redundant Call note and the report text-edit tests to this file."""

import html as _html
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from optimus import renderer
from optimus.renderer import fix_recipes, recipe_enrichment

_CALLSITE = {"filename": "apps/myapp/myapp/api.py", "lineno": 12, "function": "load"}
_RAW_DDL = "ALTER TABLE `tabSales Invoice` ADD INDEX IF NOT EXISTS `po_no_index` (`po_no`);"
_OLD_AI = {
	"suggestion": "**Fix**\n\nold-ai-advice: run ALTER TABLE by hand",
	"model": "old-model", "provider": "OpenAI", "generated_at": "2026-06-01T00:00:00+00:00",
}


def _row(ftype, detail, *, llm=None):
	return SimpleNamespace(
		finding_type=ftype, severity="High", title=f"{ftype} finding", customer_description="desc",
		estimated_impact_ms=300.0, affected_count=5, action_ref="",
		technical_detail_json=json.dumps(detail), llm_fix_json=json.dumps(llm) if llm else None,
	)


def _table(**kw):
	base = {
		"table": "tabSales Invoice", "consolidated_time_ms": 50.0, "queries": 5,
		"read_count": 4, "write_count": 0, "read_time_ms": 50.0, "write_time_ms": 0.0,
		"index_candidates": [{"column": "customer", "sources": ["WHERE"], "hits": 4}],
		"recommended_index": {
			"columns": ["customer", "posting_date"], "doctype": "Sales Invoice",
			"together_count": 3, "read_count": 4, "also_filtered": [],
		},
		"framework_cols_filtered": [], "is_meta_table": False, "is_write_hot": False,
	}
	base.update(kw)
	return base


def _doc(findings=(), tables=()):
	return SimpleNamespace(
		name="PS-rec", session_uuid="rec-uuid", title="t", user="a@example.com",
		status="Ready", started_at="2026-09-24T00:00:00", stopped_at="2026-09-24T00:00:05",
		notes=None, top_severity="High", summary_html=None, total_duration_ms=100,
		total_query_time_ms=80, total_queries=5, total_requests=1, top_queries_json="[]",
		table_breakdown_json=json.dumps(list(tables)), hot_frames_json=None,
		session_time_breakdown_json=None, total_python_ms=None, total_sql_ms=None,
		analyzer_warnings=None, v5_aggregate_json="{}", actions=[], findings=list(findings),
		phase_2_runs=[],
	)


def _render(doc):
	with patch("optimus.settings.get_ignored_apps", return_value=()):
		return _html.unescape(renderer.render_raw(doc, recordings=[]))


@pytest.fixture
def erpnext_meta(monkeypatch):
	import frappe

	meta = SimpleNamespace(custom=0, fields=[
		SimpleNamespace(fieldname="po_no", fieldtype="Data", is_custom_field=0),
		SimpleNamespace(fieldname="remarks", fieldtype="Small Text", is_custom_field=0),
		SimpleNamespace(fieldname="customer", fieldtype="Link", is_custom_field=0),
	])
	monkeypatch.setattr(frappe, "get_meta", lambda dt, *a, **kw: meta, raising=False)
	monkeypatch.setattr(frappe, "get_doctype_app", lambda dt: "erpnext", raising=False)


def _missing_index(ddl=_RAW_DDL, **kw):
	detail = {"table": "tabSales Invoice", "column": "po_no", "callsite": _CALLSITE, "suggested_ddl": ddl}
	return _row("Missing Index", detail, **kw)


def test_missing_index_shows_property_setter_recipe_not_raw_ddl(erpnext_meta):
	out = _render(_doc([_missing_index()]))
	assert "ALTER TABLE" not in out
	assert (
		'make_property_setter("Sales Invoice", "po_no", "search_index", "1", "Check", for_doctype=False)'
		in out
	)
	assert 'set search_index to 1 on its "po_no" field' in out


def test_postgres_ddl_never_rendered(erpnext_meta):
	pg = 'CREATE INDEX IF NOT EXISTS "tabSales Invoice_po_no_index" ON "public"."tabSales Invoice" ("po_no");'
	out = _render(_doc([_missing_index(ddl=pg)]))
	assert "CREATE INDEX" not in out


def test_legacy_session_regenerates_clean(erpnext_meta):
	framework_n1 = _row(
		"Framework N+1", {"callsite": _CALLSITE, "fix_hint": "Batch the calls."},
		llm=dict(_OLD_AI, suggestion="**Fix**\n\npatch-frappe-core"),
	)
	table = _table(ai_index={"suggestion": "**Recommendation**\n\nold-index-advice", "model": "old-model"})
	out = _render(_doc([_missing_index(llm=_OLD_AI), framework_n1], [table]))
	assert "old-ai-advice" not in out
	assert "patch-frappe-core" not in out
	assert "old-index-advice" not in out and "old-model" not in out
	assert "Index advice" not in out
	assert "ALTER TABLE" not in out


def test_table_card_composite_with_text_column_gets_the_prefix(erpnext_meta):
	table = _table(recommended_index={
		"columns": ["customer", "remarks"], "doctype": "Sales Invoice",
		"together_count": 3, "read_count": 4, "also_filtered": [],
	})
	out = _render(_doc([], [table]))
	assert 'frappe.db.add_index("Sales Invoice", ["customer", "remarks(255)"])' in out


def test_table_card_single_column_falls_back_to_candidates(erpnext_meta):
	table = _table(recommended_index={
		"columns": ["customer"], "doctype": "Sales Invoice",
		"together_count": 3, "read_count": 4, "also_filtered": [],
	})
	out = _render(_doc([], [table]))
	assert 'frappe.db.add_index("Sales Invoice", ["customer"])' not in out
	assert "Index candidates - to speed up reads" in out


def test_gated_hot_line_shows_note_and_hides_stored_ai(erpnext_meta):
	hot = _row("Hot Line", {
		"file": "apps/myapp/myapp/controllers.py", "lineno": 30, "dotted_path": "myapp.controllers.X.validate",
		"line_content": "super().validate()",
	}, llm=dict(_OLD_AI, suggestion="**Fix**\n\nskip-the-super-call"))
	out = _render(_doc([hot]))
	assert "Most of this line's time is spent inside super().validate" in out
	assert "skip-the-super-call" not in out


def test_pure_python_hot_line_keeps_its_ai_fix(erpnext_meta):
	hot = _row("Hot Line", {
		"file": "apps/myapp/myapp/controllers.py", "lineno": 30, "dotted_path": "myapp.controllers.X.total",
		"line_content": "total = total + flt(row.qty) * flt(row.rate)",
	}, llm=dict(_OLD_AI, suggestion="**Fix**\n\nuse-a-sum"))
	out = _render(_doc([hot]))
	assert "use-a-sum" in out


def test_recipe_failure_fails_closed(monkeypatch):
	def boom(*a, **kw):
		raise RuntimeError("recipe bug")

	monkeypatch.setattr(fix_recipes, "index_recipe", boom)
	out = _render(_doc([_missing_index()]))
	assert "ALTER TABLE" not in out


def test_meta_lookup_is_memoised_and_shaped(monkeypatch):
	import frappe

	calls = []
	meta = SimpleNamespace(custom=0, fields=[
		SimpleNamespace(fieldname="po_no", fieldtype="Data", is_custom_field=0),
		SimpleNamespace(fieldname="x_cf", fieldtype="Data", is_custom_field=1),
	])
	monkeypatch.setattr(frappe, "get_meta", lambda dt, *a, **kw: calls.append(dt) or meta, raising=False)
	monkeypatch.setattr(frappe, "get_doctype_app", lambda dt: "myapp", raising=False)
	lookup = recipe_enrichment.make_meta_lookup(installed_apps=frozenset({"frappe", "myapp"}))
	first = lookup("Sales Invoice")
	assert lookup("Sales Invoice") is first
	assert calls == ["Sales Invoice"]
	assert first == {
		"module_app": "myapp", "is_own_app": True, "is_custom_doctype": False,
		"fields": {
			"po_no": {"fieldtype": "Data", "is_custom_field": False},
			"x_cf": {"fieldtype": "Data", "is_custom_field": True},
		},
	}


def test_meta_lookup_returns_none_when_get_meta_raises(monkeypatch):
	import frappe

	def missing(dt, *a, **kw):
		raise frappe.DoesNotExistError(dt)

	monkeypatch.setattr(frappe, "get_meta", missing, raising=False)
	assert recipe_enrichment.make_meta_lookup()("Gone DocType") is None




# --- Redundant Call findings analyzed before the L5 fix (no D-STAMP stamp) ----

_RC_DETAIL = {"fn_name": "get_doc", "callsite": _CALLSITE}


def _rc_doc(detail):
	return _doc([_row("Redundant Call", dict(detail), llm=dict(_OLD_AI, suggestion="**Fix**\n\nhoist-it"))])


def test_pre_fix_redundant_call_shows_the_re_record_note(erpnext_meta):
	out = _render(_rc_doc(_RC_DETAIL))
	assert "analyzed before the callsite fix" in out and "re-record the flow" in out
	assert "hoist-it" in out  # the stored suggestion stays visible, with the caveat


def test_stamped_redundant_call_has_no_note(erpnext_meta):
	out = _render(_rc_doc(dict(_RC_DETAIL, callsite_walk="outermost_first")))
	assert "analyzed before the callsite fix" not in out




# --- report.html text edits (PR-L1's frozen-template text exception) ---------

_STALE = "Generated with an earlier version of the AI reviewer; use Refresh AI suggestions to update."


def _current_prompt_version():
	from optimus import ai_prompts

	return ai_prompts.PROMPT_VERSION


def test_table_card_never_names_customize_form(erpnext_meta):
	out = _render(_doc([], [_table()]))
	assert 'frappe.db.add_index("Sales Invoice", ["customer", "posting_date"])' in out
	assert "Customize" not in out
	assert "set search_index with a Property Setter for another app's DocType" in out


def test_finding_card_code_block_is_labelled_suggested_index(erpnext_meta):
	out = _render(_doc([_missing_index()]))
	assert "Suggested DDL" not in out
	assert "Suggested index" in out


def _ai_row(**llm):
	blob = dict(_OLD_AI, suggestion="**Fix**\n\nbatch-the-query", **llm)
	return _row("N+1 Query", {"callsite": _CALLSITE}, llm=blob)


def test_older_prompt_version_gets_the_stale_note(erpnext_meta):
	out = _render(_doc([_ai_row(prompt_version=_current_prompt_version() - 1)]))
	assert "batch-the-query" in out and _STALE in out


def test_legacy_suggestion_without_prompt_version_gets_the_stale_note(erpnext_meta):
	out = _render(_doc([_ai_row()]))
	assert "batch-the-query" in out and _STALE in out


def test_current_prompt_version_has_no_stale_note(erpnext_meta):
	out = _render(_doc([_ai_row(prompt_version=_current_prompt_version())]))
	assert "batch-the-query" in out and _STALE not in out
