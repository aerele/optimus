# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Render-time recipes (recipe_enrichment wired into render_raw): one advisor and one
evidence lookup fill the finding's fix-hint / code slots and the table card, retired AI
output is gone and raw DDL never reaches the report."""

import html as _html
import json
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from optimus import renderer
from optimus.renderer import index_recipes, recipe_enrichment
from optimus.renderer.recipe_enrichment import FieldEvidence, TableEvidence

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


def _field(fieldtype):
	return FieldEvidence(fieldtype, 0, False, False, False)


def _evidence_for(doctype, fields):
	types = {"name": "varchar", "creation": "datetime"}
	for name, field in fields.items():
		types[name] = {"Small Text": "text", "Date": "date"}.get(field.fieldtype, "varchar")
	return TableEvidence(
		table=f"tab{doctype}", doctype=doctype, app="erpnext", is_custom_doctype=False, dialect="mariadb",
		fields=fields, column_types=types,
		text_columns=frozenset(c for c, t in types.items() if t == "text"), unindexable_columns=frozenset(),
		indexes=(),
	)


@pytest.fixture
def evidence(monkeypatch):
	"""Per-table evidence: Sales Invoice and GL Entry belong to erpnext; every
	listed column exists with its Frappe type and no index yet."""
	tables = {
		"tabSales Invoice": _evidence_for("Sales Invoice", {
			"po_no": _field("Data"), "remarks": _field("Small Text"), "customer": _field("Link"),
			"posting_date": _field("Date"), "status": _field("Select"),
		}),
		"tabGL Entry": _evidence_for("GL Entry", {
			"against_voucher_type": _field("Link"), "against_voucher": _field("Dynamic Link"),
		}),
	}
	monkeypatch.setattr(recipe_enrichment, "_read_table_evidence", lambda table: tables.get(table))
	return tables


def _missing_index(ddl=_RAW_DDL, **kw):
	detail = {"table": "tabSales Invoice", "column": "po_no", "callsite": _CALLSITE, "suggested_ddl": ddl}
	return _row("Missing Index", detail, **kw)


def test_missing_index_on_another_apps_field_gets_its_ensure_indexes_entry(evidence):
	out = _render(_doc([_missing_index()]))
	assert "ALTER TABLE" not in out
	assert '{"doctype": "Sales Invoice", "search_index_field": "po_no", "db": "mariadb"}' in out
	assert 'make_property_setter(doctype, field, "search_index", 1, "Check", validate_fields_for_doctype=False)' in out
	assert 'belongs to the "erpnext" app, so do not edit it' in out


def test_postgres_ddl_never_rendered(evidence):
	pg = 'CREATE INDEX IF NOT EXISTS "tabSales Invoice_po_no_index" ON "public"."tabSales Invoice" ("po_no");'
	out = _render(_doc([_missing_index(ddl=pg)]))
	assert "CREATE INDEX" not in out


def test_legacy_session_regenerates_clean(evidence):
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


def test_table_card_composite_with_text_column_gets_the_prefix_and_the_name(evidence):
	"""The card shows the advisor's own ensure_indexes() code, entry and name included."""
	table = _table(recommended_index={
		"columns": ["customer", "remarks"], "doctype": "Sales Invoice",
		"together_count": 3, "read_count": 4, "also_filtered": [],
	})
	out = _render(_doc([], [table]))
	name = index_recipes.optimus_index_name("Sales Invoice", ("customer", "remarks"))
	entry = {"doctype": "Sales Invoice", "columns": ["customer", "remarks(255)"], "index_name": name, "db": "mariadb"}
	assert json.dumps(entry) in out
	assert 'frappe.db.add_index(doctype, columns, index_name=entry["index_name"])' in out


def test_single_column_card_keeps_its_recommendation(evidence):
	"""The single-column recommendation is kept, so the card keeps together_count,
	its SHOW INDEX hint and its route note."""
	table = _table(recommended_index={
		"columns": ["customer"], "doctype": "Sales Invoice",
		"together_count": 3, "read_count": 4, "also_filtered": [],
	})
	out = _render(_doc([], [table]))
	assert "Index candidate - to speed up reads" in out
	assert "filtered together in <strong>3</strong> of 4 reads" in out
	assert "SHOW INDEX FROM `tabSales Invoice`" in out
	assert "One column: bench migrate drops a single-column index" in out
	assert '{"doctype": "Sales Invoice", "search_index_field": "customer", "db": "mariadb"}' in out


def test_write_hot_single_column_card_is_never_called_low_risk(evidence):
	"""GL Entry kept its recommendation, so the write-hot warning shows."""
	table = _table(
		table="tabGL Entry", is_write_hot=True, write_count=0,
		recommended_index={
			"columns": ["against_voucher"], "doctype": "GL Entry",
			"together_count": 3, "read_count": 4, "also_filtered": [],
		},
	)
	out = _render(_doc([], [table]))
	assert "normally write-hot in production" in out
	assert "adding an index here is low-risk" not in out


def test_finding_and_card_on_the_same_columns_name_the_same_index(evidence):
	"""The card no longer says "patch" while the finding says something else."""
	lookup = recipe_enrichment.make_evidence_lookup()
	finding = {"finding_type": "Full Table Scan", "technical_detail": {
		"table": "tabSales Invoice",
		"normalized_query": "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ?",
	}}
	table = {"table": "tabSales Invoice", "recommended_index": {
		"columns": ["customer", "status"], "doctype": "Sales Invoice",
		"together_count": 3, "read_count": 4, "also_filtered": [],
	}}
	recipe_enrichment.apply_finding_recipes([finding], evidence_lookup=lookup)
	recipe_enrichment.apply_table_recipes([table], evidence_lookup=lookup)
	name = index_recipes.optimus_index_name("Sales Invoice", ("customer", "status"))
	rec = table["recommended_index"]
	assert name in finding["technical_detail"]["suggested_ddl"]
	assert rec["index_name"] == name and name in rec["route_note"]
	assert rec["code"] == finding["technical_detail"]["suggested_ddl"]
	assert rec["route"] == index_recipes.ROUTE_ENSURE_INDEXES
	assert "patch" not in rec["route_note"]


def test_card_never_renders_an_add_index_call_without_index_name(evidence):
	"""The card's code line is the advisor's code or nothing; it never shows a
	nameless frappe.db.add_index(...) call, and a card with no code has no code line."""
	cards = [
		_table(),
		_table(table="tabGL Entry", recommended_index={
			"columns": ["against_voucher"], "doctype": "GL Entry",
			"together_count": 3, "read_count": 4, "also_filtered": [],
		}),
		_table(table="tabGone DocType", recommended_index={
			"columns": ["customer", "status"], "doctype": "Gone DocType",
			"together_count": 3, "read_count": 4, "also_filtered": [],
		}),
	]
	out = _render(_doc([], cards))
	calls = re.findall(r"frappe\.db\.add_index\(([^)]*)\)", out)
	# two modules, each with the columns route's call and the Property Setter route's own build
	assert len(calls) == 4
	assert all("index_name=" in args for args in calls), calls
	assert out.count('<pre class="sql-snip">') == 2
	assert "Do not add this index. Optimus has no information" not in out
	assert f'{index_recipes.NO_VERDICT} Optimus has no information about table "tabGone DocType"' in out


def test_gated_hot_line_shows_note_and_hides_stored_ai(evidence):
	hot = _row("Hot Line", {
		"file": "apps/myapp/myapp/controllers.py", "lineno": 30, "dotted_path": "myapp.controllers.X.validate",
		"line_content": "super().validate()", "per_hit_us": 132809.5,
	}, llm=dict(_OLD_AI, suggestion="**Fix**\n\nskip-the-super-call"))
	out = _render(_doc([hot]))
	assert "Most of this line's time is spent inside super().validate" in out
	assert "skip-the-super-call" not in out


def test_pure_python_hot_line_keeps_its_ai_fix(evidence):
	hot = _row("Hot Line", {
		"file": "apps/myapp/myapp/controllers.py", "lineno": 30, "dotted_path": "myapp.controllers.X.total",
		"line_content": "total = total + flt(row.qty) * flt(row.rate)",
	}, llm=dict(_OLD_AI, suggestion="**Fix**\n\nuse-a-sum"))
	out = _render(_doc([hot]))
	assert "use-a-sum" in out


def test_recipe_failure_fails_closed(monkeypatch):
	def boom(*a, **kw):
		raise RuntimeError("recipe bug")

	monkeypatch.setattr(index_recipes, "advise_finding", boom)
	out = _render(_doc([_missing_index()]))
	assert "ALTER TABLE" not in out




# --- Redundant Call findings analyzed before the L5 fix (no D-STAMP stamp) ----

_RC_DETAIL = {"fn_name": "get_doc", "callsite": _CALLSITE}


def _rc_doc(detail):
	return _doc([_row("Redundant Call", dict(detail), llm=dict(_OLD_AI, suggestion="**Fix**\n\nhoist-it"))])


def test_pre_fix_redundant_call_shows_the_re_record_note(evidence):
	out = _render(_rc_doc(_RC_DETAIL))
	assert "analyzed before the callsite fix" in out and "re-record the flow" in out
	assert "hoist-it" in out  # the stored suggestion stays visible, with the caveat


def test_stamped_redundant_call_has_no_note(evidence):
	out = _render(_rc_doc(dict(_RC_DETAIL, callsite_walk="outermost_first")))
	assert "analyzed before the callsite fix" not in out




# --- report.html text edits (the frozen template's text-only exception) ---------


def test_table_card_never_names_customize_form(evidence):
	out = _render(_doc([], [_table()]))
	name = index_recipes.optimus_index_name("Sales Invoice", ("customer", "posting_date"))
	assert json.dumps({"doctype": "Sales Invoice", "columns": ["customer", "posting_date"], "index_name": name}) in out
	assert "Customize" not in out
	assert f"Your app's ensure_indexes() function creates the index \"{name}\"" in out


def test_finding_card_code_block_is_labelled_suggested_index(evidence):
	out = _render(_doc([_missing_index()]))
	assert "Suggested DDL" not in out
	assert "Suggested index" in out


# --- report.html text edits (the sanctioned footer and notes) -----------------

_STALE_NEUTRAL = "Generated with an earlier version of the AI reviewer."
_STALE_REFRESH = "Generated with an earlier version of the AI reviewer; use Refresh AI suggestions to update."


def _current_prompt_version():
	from optimus import ai_prompts

	return ai_prompts.PROMPT_VERSION


def _ai_row(**llm):
	blob = dict(_OLD_AI, suggestion="**Fix**\n\nbatch-the-query", **llm)
	return _row("N+1 Query", {"callsite": _CALLSITE}, llm=blob)


@pytest.fixture
def ai_available(monkeypatch):
	from optimus import ai_fix

	monkeypatch.setattr(ai_fix, "is_available", lambda section=None: True)


def test_older_prompt_version_says_refresh_when_refresh_would_redo_it(evidence, ai_available):
	out = _render(_doc([_ai_row(prompt_version=_current_prompt_version() - 1)]))
	assert "batch-the-query" in out and _STALE_REFRESH in out


def test_stale_note_is_neutral_when_ai_is_not_available(evidence, monkeypatch):
	from optimus import ai_fix

	monkeypatch.setattr(ai_fix, "is_available", lambda section=None: False)
	out = _render(_doc([_ai_row()]))
	assert _STALE_NEUTRAL in out and "Refresh AI suggestions" not in out


def test_stale_note_is_neutral_for_a_gated_row(evidence, ai_available):
	out = _render(_rc_doc(_RC_DETAIL))
	assert "hoist-it" in out and _STALE_NEUTRAL in out
	assert "use Refresh AI suggestions" not in out


def test_current_prompt_version_has_no_stale_note(evidence, ai_available):
	out = _render(_doc([_ai_row(prompt_version=_current_prompt_version())]))
	assert "batch-the-query" in out and "earlier version of the AI reviewer" not in out


def test_unstamped_note_needs_a_stored_suggestion(evidence):
	row = _row("Redundant Call", dict(_RC_DETAIL))
	out = _render(_doc([row]))
	assert "analyzed before the callsite fix" not in out


def test_finding_notes_are_added_once():
	from optimus import ai_grounding

	findings = [
		{"finding_type": "Redundant Call", "llm_fix": {"suggestion_html": "x"},
		 "technical_detail": {"fn_name": "get_doc", "validation_note": ["captured note"]}},
		{"finding_type": "Framework N+1", "technical_detail": {"fix_hint": "Batch the calls."}},
	]
	for _ in range(2):
		recipe_enrichment.apply_finding_recipes(findings, evidence_lookup=lambda table: None)
	assert findings[0]["technical_detail"]["validation_note"] == (
		"captured note " + ai_grounding.UNSTAMPED_REDUNDANT_CALL_NOTE
	)
	assert findings[1]["technical_detail"]["fix_hint"] == "Batch the calls. " + ai_grounding.FRAMEWORK_N1_NOTE


def test_framework_n_plus_one_shows_why_there_is_no_ai_fix(evidence):
	"""A Framework N+1 report row says why it has no AI fix instead of showing the retired AI advice."""
	row = _row("Framework N+1", {"callsite": _CALLSITE, "fix_hint": "Batch the calls."}, llm=_OLD_AI)
	out = _render(_doc([row]))
	assert "points at a loop inside framework code" in out and "old-ai-advice" not in out


def test_token_total_counts_suggestions_the_report_hides(evidence):
	"""Deployment minor: tokens were spent even for retired index and Framework N+1 output."""
	rows = [
		_missing_index(llm=dict(_OLD_AI, tokens={"total_tokens": 100})),
		_row("Framework N+1", {"callsite": _CALLSITE}, llm=dict(_OLD_AI, tokens={"total_tokens": 50})),
		_row("N+1 Query", {"callsite": _CALLSITE}, llm=dict(_OLD_AI, tokens={"total_tokens": 30})),
	]
	table = _table(ai_index={"suggestion": "x", "tokens": {"total_tokens": 20}})
	out = _render(_doc(rows, [table]))
	assert "AI suggestions used <strong>200</strong> tokens" in out


def test_stale_note_is_neutral_for_an_excluded_type(evidence, ai_available, monkeypatch):
	"""Refresh never redoes an excluded type, so its footer must not name Refresh."""
	from optimus import ai_fix

	monkeypatch.setattr(ai_fix, "is_finding_type_excluded", lambda ftype: ftype == "N+1 Query")
	out = _render(_doc([_ai_row()]))
	assert "batch-the-query" in out and _STALE_NEUTRAL in out
	assert "use Refresh AI suggestions" not in out


def test_token_total_counts_findings_the_render_filters_out(evidence):
	"""A finding without a callsite and a Function Not Invoked finding are dropped from
	the report, but their stored suggestions' tokens were spent."""
	rows = [
		_row("N+1 Query", {}, llm=dict(_OLD_AI, tokens={"total_tokens": 40})),
		_row("Function Not Invoked", {"callsite": _CALLSITE}, llm=dict(_OLD_AI, tokens={"total_tokens": 25})),
		_row("N+1 Query", {"callsite": _CALLSITE}, llm=dict(_OLD_AI, tokens={"total_tokens": 30})),
	]
	out = _render(_doc(rows))
	assert "AI suggestions used <strong>95</strong> tokens" in out


def test_token_total_counts_findings_of_ignored_apps(evidence):
	row = _row("N+1 Query", {"callsite": _CALLSITE}, llm=dict(_OLD_AI, tokens={"total_tokens": 30}))
	with patch("optimus.settings.get_ignored_apps", return_value=("myapp",)):
		out = _html.unescape(renderer.render_raw(_doc([row]), recordings=[]))
	assert "old-ai-advice" not in out and "AI suggestions used <strong>30</strong> tokens" in out


# --- the finding's own text agrees with its advice ----------

_MI_TITLE = "Add index on tabSales Invoice(customer)"
_MI_DESC = (
	"Adding an index to the **customer** column of the **tabSales Invoice** table would speed up 5 queries in "
	"this session, saving roughly 300ms total. Ask your developer to add this index in a database migration."
)
_FTS_DESC = (
	"A query had to read every row of the **tabSales Invoice** table (1000 rows examined) because no index could "
	"help. This kind of query gets dramatically slower as the table grows. Adding an appropriate index is usually "
	"the fix."
)


@pytest.fixture
def indexed(evidence, monkeypatch):
	"""tabSales Invoice with an index that customer already leads."""
	import dataclasses

	from optimus.renderer.recipe_enrichment import IndexEvidence

	tables = dict(evidence)
	tables["tabSales Invoice"] = dataclasses.replace(
		evidence["tabSales Invoice"], indexes=(IndexEvidence("customer", ("customer",), False),),
	)
	monkeypatch.setattr(recipe_enrichment, "_read_table_evidence", lambda table: tables.get(table))
	return tables


def _titled(ftype, detail, title, desc):
	row = _row(ftype, detail)
	row.title, row.customer_description = title, desc
	return row


def _mi_customer():
	detail = {"table": "tabSales Invoice", "column": "customer", "callsite": _CALLSITE, "suggested_ddl": _RAW_DDL}
	return _titled("Missing Index", detail, _MI_TITLE, _MI_DESC)


def _plan_titles(out):
	return re.findall(r'<p class="title">(.*?)</p>', out, flags=re.S)


def test_a_no_code_missing_index_has_a_neutral_title_description_and_plan_step(indexed):
	row = _mi_customer()
	stored = (row.title, row.customer_description, row.technical_detail_json)
	out = _render(_doc([row]))
	assert "Index on tabSales Invoice(customer): no new index recommended" in out
	assert _MI_TITLE not in out and "in a database migration" not in out
	assert "Add a database index" not in out
	assert recipe_enrichment.NO_INDEX_ACTION_TITLE in _plan_titles(out)
	assert "Optimus does not recommend a new index on it" in out
	assert (row.title, row.customer_description, row.technical_detail_json) == stored  # stored JSON unchanged


def test_a_routed_missing_index_points_at_how_to_fix(evidence):
	out = _render(_doc([_mi_customer()]))
	assert _MI_TITLE in out and "Add a database index" in _plan_titles(out)
	assert "Ask your developer to add this index using the code and steps under How to fix." in out
	assert "in a database migration" not in out


def test_a_no_code_explain_finding_drops_the_index_fix_sentence(indexed):
	detail = {"table": "tabSales Invoice", "normalized_query": "SELECT name FROM `tabSales Invoice` WHERE customer = ?",
		"callsite": _CALLSITE}
	out = _render(_doc([_titled("Full Table Scan", detail, "Full table scan on tabSales Invoice", _FTS_DESC)]))
	assert "Adding an appropriate index is usually the fix." not in out
	assert recipe_enrichment.NO_INDEX_NOTE in out
	assert "This kind of query gets dramatically slower as the table grows." in out


def test_a_routed_explain_finding_keeps_its_description(evidence):
	detail = {"table": "tabSales Invoice", "normalized_query": "SELECT name FROM `tabSales Invoice` WHERE customer = ?",
		"callsite": _CALLSITE}
	out = _render(_doc([_titled("Full Table Scan", detail, "Full table scan on tabSales Invoice", _FTS_DESC)]))
	assert "Adding an appropriate index is usually the fix." in out
	assert recipe_enrichment.NO_INDEX_NOTE not in out


def test_a_failed_missing_index_is_neutral_and_says_what_to_do(evidence, monkeypatch):
	def boom(*a, **kw):
		raise RuntimeError("recipe bug")

	monkeypatch.setattr(index_recipes, "advise_finding", boom)
	out = _render(_doc([_mi_customer()]))
	# a failure is no verdict on the index
	assert "Index on tabSales Invoice(customer): Optimus cannot say" in out and "no new index recommended" not in out
	assert "Add a database index" not in out
	assert recipe_enrichment.RECIPE_FAILED_HINT in out
	assert 'send the bench log line "optimus: index advice failed" to the Optimus maintainers' in out
	assert "EXPLAIN yourself" in recipe_enrichment.RECIPE_FAILED_HINT


def test_a_failed_card_gives_a_neutral_verdict_and_a_next_step(evidence, monkeypatch):
	def boom(*a, **kw):
		raise RuntimeError("card bug")

	monkeypatch.setattr(index_recipes, "advise_table", boom)
	out = _render(_doc([], [_table()]))
	note = recipe_enrichment.RECIPE_FAILED_CARD_NOTE
	assert note.startswith(index_recipes.NO_VERDICT) and "Do not add this index" not in note
	assert 'send the bench log line "optimus: index advice failed"' in note
	assert f"Optimus index advice:</em> {note}" in out


def test_the_display_overrides_are_idempotent(indexed):
	import copy

	lookup = recipe_enrichment.make_evidence_lookup()
	findings = [
		{"finding_type": "Missing Index", "title": _MI_TITLE, "customer_description": _MI_DESC,
			"technical_detail": {"table": "tabSales Invoice", "column": "customer"}},
		{"finding_type": "Missing Index", "title": "Add index on tabSales Invoice(po_no)", "customer_description": _MI_DESC,
			"technical_detail": {"table": "tabSales Invoice", "column": "po_no"}},
		{"finding_type": "Full Table Scan", "title": "Full table scan on tabSales Invoice", "customer_description": _FTS_DESC,
			"technical_detail": {"table": "tabSales Invoice", "normalized_query": "SELECT name FROM `tabSales Invoice` WHERE customer = ?"}},
	]
	recipe_enrichment.apply_finding_recipes(findings, evidence_lookup=lookup)
	once = copy.deepcopy(findings)
	recipe_enrichment.apply_finding_recipes(findings, evidence_lookup=lookup)
	assert findings == once
	assert findings[0]["action_title"] == recipe_enrichment.NO_INDEX_ACTION_TITLE
	assert "action_title" not in findings[1] and "action_title" not in findings[2]


def test_a_render_parses_each_query_once_for_its_aliases(evidence, monkeypatch):
	"""The alias map is memoised per query in the render's parser."""
	calls = []
	real = index_recipes.table_aliases
	monkeypatch.setattr(index_recipes, "table_aliases", lambda q: calls.append(q) or real(q))
	detail = {"table": "tabSales Invoice", "normalized_query": "SELECT name FROM `tabSales Invoice` WHERE customer = ?",
		"callsite": _CALLSITE}
	rows = [_row(ftype, dict(detail)) for ftype in ("Full Table Scan", "Low Filter Ratio", "Filesort")]
	_render(_doc(rows))
	assert calls == [detail["normalized_query"]]


# --- a routed Filesort or Temporary Table whose index keeps the sort --------------------

_FS_DESC = (
	"A query against **tabSales Invoice** had to sort its results without the help of an index. For small result "
	"sets this is fine, but on large data it slows the query down significantly. Adding an index that covers the "
	"ORDER BY clause usually fixes it."
)


def _filesort(query):
	detail = {"table": "tabSales Invoice", "normalized_query": query, "callsite": _CALLSITE}
	return _titled("Filesort", detail, "Filesort on tabSales Invoice", _FS_DESC)


def test_a_routed_filesort_that_keeps_the_sort_never_promises_to_fix_it(evidence):
	row = _filesort("SELECT name FROM `tabSales Invoice` WHERE customer = ? ORDER BY lower(po_no) LIMIT 5")
	stored = (row.title, row.customer_description)
	out = _render(_doc([row]))
	assert "Adding an index that covers the ORDER BY clause usually fixes it." not in out
	assert recipe_enrichment.SORT_STAYS_NOTES["Filesort"] in out
	assert "Avoid the filesort" not in _plan_titles(out)
	assert recipe_enrichment.NO_INDEX_ACTION_TITLE in _plan_titles(out)
	assert "<pre" in out.split("Filesort on tabSales Invoice", 1)[1]  # the code is still shown
	assert (row.title, row.customer_description) == stored


def test_a_routed_filesort_that_removes_the_sort_keeps_its_text(evidence):
	query = "SELECT name FROM `tabSales Invoice` WHERE customer = ? ORDER BY posting_date LIMIT 5"
	out = _render(_doc([_filesort(query)]))
	assert "Adding an index that covers the ORDER BY clause usually fixes it." in out
	assert recipe_enrichment.SORT_STAYS_NOTES["Filesort"] not in out
	assert "Avoid the filesort" in _plan_titles(out)


def test_a_routed_temporary_table_that_keeps_it_says_so(evidence):
	q = "SELECT customer, COUNT(name) FROM `tabSales Invoice` WHERE status = ? GROUP BY customer ORDER BY COUNT(name)"
	detail = {"table": "tabSales Invoice", "normalized_query": q, "callsite": _CALLSITE}
	out = _render(_doc([_titled("Temporary Table", detail, "Temporary table on tabSales Invoice", "d")]))
	assert recipe_enrichment.SORT_STAYS_NOTES["Temporary Table"] in out
	assert "Avoid the temporary table" not in _plan_titles(out)


def test_the_export_carries_whether_the_sort_stays(evidence):
	lookup = recipe_enrichment.make_evidence_lookup()
	stays, _ = recipe_enrichment.export_advice({"finding_type": "Filesort", "technical_detail": {
		"table": "tabSales Invoice",
		"normalized_query": "SELECT name FROM `tabSales Invoice` WHERE customer = ? ORDER BY lower(po_no) LIMIT 5",
	}}, evidence_lookup=lookup)
	removes, _ = recipe_enrichment.export_advice({"finding_type": "Filesort", "technical_detail": {
		"table": "tabSales Invoice",
		"normalized_query": "SELECT name FROM `tabSales Invoice` WHERE customer = ? ORDER BY posting_date LIMIT 5",
	}}, evidence_lookup=lookup)
	assert stays["sort_stays"] == "stays" and removes["sort_stays"] == ""


def test_the_sort_stays_overrides_are_idempotent(evidence):
	import copy

	finding = {"finding_type": "Filesort", "title": "Filesort on tabSales Invoice", "customer_description": _FS_DESC,
		"technical_detail": {"table": "tabSales Invoice",
			"normalized_query": "SELECT name FROM `tabSales Invoice` WHERE customer = ? ORDER BY lower(po_no) LIMIT 5"}}
	lookup = recipe_enrichment.make_evidence_lookup()
	recipe_enrichment.apply_finding_recipes([finding], evidence_lookup=lookup)
	once = copy.deepcopy(finding)
	recipe_enrichment.apply_finding_recipes([finding], evidence_lookup=lookup)
	assert finding == once and finding["action_title"] == recipe_enrichment.NO_INDEX_ACTION_TITLE
	assert finding["customer_description"].count(recipe_enrichment.SORT_STAYS_NOTES["Filesort"]) == 1


# --- a Missing Index Optimus cannot judge has a neutral title ---------------------------


def test_a_missing_index_with_no_verdict_says_optimus_cannot_say(evidence):
	detail = {"table": "tabSessions", "column": "user", "callsite": _CALLSITE}
	out = _render(_doc([_titled("Missing Index", detail, "Add index on tabSessions(user)", _MI_DESC)]))
	assert "Index on tabSessions(user): Optimus cannot say" in out
	assert "no new index recommended" not in out and "Add index on tabSessions(user)" not in out


def test_a_sort_that_only_may_stay_gets_a_hedged_note(evidence):
	"""No ORDER BY on this table: the sort comes from elsewhere and may stay, so the note
	never says the index does not remove it."""
	q = "SELECT customer, COUNT(name) FROM `tabSales Invoice` WHERE status = ? GROUP BY customer"
	out = _render(_doc([_filesort(q)]))
	assert recipe_enrichment.SORT_MAY_STAY_NOTES["Filesort"] in out
	assert recipe_enrichment.SORT_STAYS_NOTES["Filesort"] not in out
	assert "Adding an index that covers the ORDER BY clause usually fixes it." not in out


class _JobTimeout(Exception):
	"""Stands in for rq's JobTimeoutException."""


def test_a_job_timeout_in_an_evidence_read_escapes_the_render(evidence, monkeypatch):
	"""render_raw lets a job deadline out, fresh; analyze's own report step catches every
	exception around it (frozen), which the docs say."""
	from optimus import safe_call

	original = _JobTimeout("deadline")

	def interrupted(table):
		raise original

	monkeypatch.setattr(safe_call, "job_timeout_types", lambda: (_JobTimeout,))
	monkeypatch.setattr(recipe_enrichment, "_read_table_evidence", interrupted)
	with pytest.raises(_JobTimeout) as caught:
		_render(_doc([_mi_customer()]))
	assert caught.value is not original and caught.value.__context__ is None
