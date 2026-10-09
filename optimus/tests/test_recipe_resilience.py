# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Recipe failures are visible and counted (O-I1), a broken Hot Line gate fails closed
(in the renderer and in ai_fix.llm_gate_note, so analyze and Refresh never abort on it),
the recipes are idempotent, the query parser is memoised and capped, and table cards
are advised after the hide-framework-tables filter."""

import copy
import html as _html
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from optimus import ai_fix, ai_grounding, analyze, renderer, safe_call
from optimus.renderer import index_recipes, recipe_enrichment


def _boom(*args, **kwargs):
	raise RuntimeError("recipe bug")


def test_a_failed_finding_recipe_leaves_a_neutral_hint_and_is_counted(monkeypatch):
	monkeypatch.setattr(index_recipes, "advise_finding", _boom)
	finding = {"finding_type": "Missing Index", "technical_detail": {
		"table": "tabSales Invoice", "column": "po_no", "suggested_ddl": "ALTER TABLE `tabSales Invoice` ADD INDEX (po_no);",
	}}
	stats = recipe_enrichment.apply_finding_recipes([finding], evidence_lookup=lambda table: None)
	assert stats == {"failed": 1}
	assert finding["technical_detail"]["fix_hint"] == recipe_enrichment.RECIPE_FAILED_HINT
	assert "suggested_ddl" not in finding["technical_detail"]


def test_a_failed_card_recipe_keeps_the_card_with_a_neutral_note(monkeypatch):
	monkeypatch.setattr(index_recipes, "advise_table", _boom)
	table = {"table": "tabSales Invoice", "recommended_index": {
		"columns": ["customer", "status"], "route": "ensure_indexes", "code": "stale code", "index_name": "idx_stale",
	}}
	stats = recipe_enrichment.apply_table_recipes([table], evidence_lookup=lambda t: None)
	assert stats == {"failed": 1}
	rec = table["recommended_index"]
	assert rec["route_note"] == recipe_enrichment.RECIPE_FAILED_CARD_NOTE
	assert (rec["route"], rec["code"], rec["index_name"]) == (index_recipes.ROUTE_NO_CODE, None, None)
	assert rec["columns"] == ["customer", "status"]


def test_failures_are_logged_once_after_the_recipes_ran(monkeypatch):
	import frappe

	lines = []
	monkeypatch.setattr(frappe, "logger", lambda *a, **k: SimpleNamespace(error=lines.append), raising=False)
	monkeypatch.setattr(index_recipes, "advise_finding", _boom)
	row = SimpleNamespace(  # shaped like test_index_recipes_render's rows, so the card renders
		finding_type="Missing Index", severity="High", title="Missing Index finding", customer_description="d",
		estimated_impact_ms=300.0, affected_count=5, action_ref="", llm_fix_json=None,
		technical_detail_json=json.dumps({
			"table": "tabSales Invoice", "column": "po_no",
			"callsite": {"filename": "apps/myapp/myapp/api.py", "lineno": 12, "function": "load"},
		}),
	)
	doc = SimpleNamespace(
		name="PS-1", session_uuid="u", title="t", user="a@example.com", status="Ready",
		started_at="2026-10-08T00:00:00", stopped_at="2026-10-08T00:00:05", notes=None, top_severity="High",
		summary_html=None, total_duration_ms=1, total_query_time_ms=1, total_queries=1, total_requests=1,
		top_queries_json="[]", table_breakdown_json="[]", hot_frames_json=None, session_time_breakdown_json=None,
		total_python_ms=None, total_sql_ms=None, analyzer_warnings=None, v5_aggregate_json="{}", actions=[],
		findings=[row], phase_2_runs=[],
	)
	with patch("optimus.settings.get_ignored_apps", return_value=()):
		html = renderer.render_raw(doc, recordings=[])
	assert recipe_enrichment.RECIPE_FAILED_HINT in _html.unescape(html)  # the note quotes the log line
	assert lines.count("optimus: index advice failed for 1 finding(s) or table(s) in one render (Missing Index: RuntimeError)") == 1


def test_a_broken_hot_line_gate_fails_closed(monkeypatch):
	monkeypatch.setattr(ai_grounding, "hot_line_gate", _boom)
	finding = {"finding_type": "Hot Line", "llm_fix": {"suggestion_html": "x"}, "technical_detail": {"file": "apps/myapp/myapp/x.py"}}
	recipe_enrichment.apply_finding_recipes([finding], evidence_lookup=lambda table: None)
	assert finding["llm_fix"] is None
	assert finding["technical_detail"]["fix_hint"] == ai_grounding.GATE_CHECK_FAILED_NOTE
	with patch("optimus.settings.get_config", return_value=SimpleNamespace(tracked_apps=())):
		assert ai_fix.llm_gate_note({"finding_type": "Hot Line", "technical_detail": {}}) == ai_grounding.GATE_CHECK_FAILED_NOTE


def test_finding_recipes_are_idempotent():
	findings = [
		{"finding_type": "Missing Index", "llm_fix": {"suggestion_html": "x"},
		 "technical_detail": {"table": "tabSales Invoice", "column": "po_no", "suggested_ddl": "ALTER TABLE x"}},
		{"finding_type": "Redundant Call", "llm_fix": {"suggestion_html": "x"}, "technical_detail": {"validation_note": "n"}},
		{"finding_type": "Framework N+1", "technical_detail": {"fix_hint": "Batch the calls."}},
		{"finding_type": "Hot Line", "llm_fix": {"suggestion_html": "x"}, "technical_detail": {
			"file": "/home/b/apps/erpnext/erpnext/x.py", "line_content": "x = 1",
		}},
	]
	recipe_enrichment.apply_finding_recipes(findings, evidence_lookup=lambda table: None)
	once = copy.deepcopy(findings)
	recipe_enrichment.apply_finding_recipes(findings, evidence_lookup=lambda table: None)
	assert findings == once


def test_the_query_parser_is_memoised_and_capped(monkeypatch):
	calls = []
	monkeypatch.setattr(index_recipes, "parse_query", lambda q: calls.append(q) or {"tables": [], "index_cols": {}})
	parse = recipe_enrichment.make_query_parser()
	assert parse("SELECT 1") == parse("SELECT 1") and calls == ["SELECT 1"]
	long_query = "SELECT name FROM `tabSales Invoice` WHERE " + " AND ".join(f"c{i} = ?" for i in range(600))
	assert len(long_query) > index_recipes.MAX_QUERY_CHARS
	assert parse(long_query) == {} and calls == ["SELECT 1"]


def _query_of_length(n):
	head, tail = "SELECT name FROM `tabSales Invoice`", " WHERE customer = ?"
	query = head + " " * (n - len(head) - len(tail)) + tail
	assert len(query) == n
	return query


@pytest.mark.parametrize("length,parsed", [(index_recipes.MAX_QUERY_CHARS, True), (index_recipes.MAX_QUERY_CHARS + 1, False)])
def test_the_parser_cap_boundary_is_exact(monkeypatch, length, parsed):
	calls = []
	monkeypatch.setattr(index_recipes, "parse_query", lambda q: calls.append(q) or {"tables": ["tabX"], "index_cols": {}})
	query = _query_of_length(length)
	result = recipe_enrichment.make_query_parser()(query)
	assert (calls == [query]) is parsed
	assert result == ({"tables": ["tabX"], "index_cols": {}} if parsed else {})


@pytest.mark.parametrize("length,capped", [(index_recipes.MAX_QUERY_CHARS, False), (index_recipes.MAX_QUERY_CHARS + 1, True)])
def test_the_advisor_cap_boundary_is_exact(length, capped):
	looked_up = []
	finding = {"finding_type": "Full Table Scan", "technical_detail": {
		"table": "tabSales Invoice", "normalized_query": _query_of_length(length),
	}}
	advice = index_recipes.advise_finding(finding, evidence_lookup=lambda table: looked_up.append(table))
	assert (f"longer than {index_recipes.MAX_QUERY_CHARS} characters" in index_recipes.finding_text(advice)) is capped
	assert looked_up == ([] if capped else ["tabSales Invoice"])
	if not capped:
		assert advice.columns == ("customer",)


def test_a_query_over_the_cap_gets_an_explanation_not_a_recipe():
	query = "SELECT name FROM `tabSales Invoice` WHERE " + " AND ".join(f"c{i} = ?" for i in range(600))
	finding = {"finding_type": "Full Table Scan", "technical_detail": {"table": "tabSales Invoice", "normalized_query": query}}
	advice = index_recipes.advise_finding(finding, evidence_lookup=lambda table: pytest.fail("no evidence read"))
	assert advice.route == index_recipes.ROUTE_NO_CODE
	assert f"longer than {index_recipes.MAX_QUERY_CHARS} characters" in index_recipes.finding_text(advice)


def test_a_missing_index_with_a_long_query_keeps_its_recipe():
	"""Missing Index names its column itself, so a long query never stops its advice."""
	query = "SELECT name FROM `tabSales Invoice` WHERE " + " AND ".join(f"c{i} = ?" for i in range(600))
	finding = {"finding_type": "Missing Index", "technical_detail": {
		"table": "tabSales Invoice", "column": "po_no", "normalized_query": query,
	}}
	advice = index_recipes.advise_finding(finding, evidence_lookup=lambda table: None)
	assert advice.columns == ("po_no",)
	assert "longer than" not in index_recipes.finding_text(advice)


def test_a_long_query_on_a_non_doctype_table_gets_no_advice():
	query = "SELECT x FROM information_schema.columns WHERE " + " AND ".join(f"c{i} = ?" for i in range(600))
	finding = {"finding_type": "Full Table Scan", "technical_detail": {"table": "information_schema.columns", "normalized_query": query}}
	assert index_recipes.advise_finding(finding, evidence_lookup=lambda table: None) is None


def _render_doc(findings=(), tables=()):
	return SimpleNamespace(
		name="PS-1", session_uuid="u", title="t", user="a@example.com", status="Ready",
		started_at="2026-10-08T00:00:00", stopped_at="2026-10-08T00:00:05", notes=None, top_severity="High",
		summary_html=None, total_duration_ms=1, total_query_time_ms=1, total_queries=1, total_requests=1,
		top_queries_json="[]", table_breakdown_json=json.dumps(list(tables)), hot_frames_json=None,
		session_time_breakdown_json=None, total_python_ms=None, total_sql_ms=None, analyzer_warnings=None,
		v5_aggregate_json="{}", actions=[], findings=list(findings), phase_2_runs=[],
	)


def test_the_render_parses_each_query_once(monkeypatch):
	parsed = []
	real = index_recipes.parse_query
	monkeypatch.setattr(index_recipes, "parse_query", lambda q: parsed.append(q) or real(q))
	query = "SELECT name FROM `tabSales Invoice` WHERE customer = ?"
	rows = [
		SimpleNamespace(
			finding_type="Full Table Scan", severity="High", title=f"scan {i}", customer_description="d",
			estimated_impact_ms=300.0 - i, affected_count=5, action_ref="", llm_fix_json=None,
			technical_detail_json=json.dumps({
				"table": "tabSales Invoice", "normalized_query": query,
				"callsite": {"filename": "apps/myapp/myapp/api.py", "lineno": 12 + i, "function": "load"},
			}),
		)
		for i in range(3)
	]
	with patch("optimus.settings.get_ignored_apps", return_value=()):
		renderer.render_raw(_render_doc(findings=rows), recordings=[])
	assert parsed == [query]


def test_a_failed_card_recipe_is_shown_and_logged(monkeypatch):
	import frappe

	lines = []
	monkeypatch.setattr(frappe, "logger", lambda *a, **k: SimpleNamespace(error=lines.append), raising=False)
	monkeypatch.setattr(index_recipes, "advise_table", _boom)
	table = {
		"table": "tabSales Invoice", "consolidated_time_ms": 50.0, "queries": 5, "read_count": 4, "write_count": 0,
		"read_time_ms": 50.0, "write_time_ms": 0.0, "index_candidates": [],
		"recommended_index": {"columns": ["customer", "status"], "doctype": "Sales Invoice", "together_count": 3, "read_count": 4},
		"framework_cols_filtered": [], "is_meta_table": False, "is_write_hot": False,
	}
	with patch("optimus.settings.get_ignored_apps", return_value=()):
		html = renderer.render_raw(_render_doc(tables=[table]), recordings=[])
	assert recipe_enrichment.RECIPE_FAILED_CARD_NOTE in _html.unescape(html)  # the note quotes the log line
	assert lines == ["optimus: index advice failed for 1 finding(s) or table(s) in one render (tabSales Invoice: RuntimeError)"]


def test_no_failure_writes_no_log_line(monkeypatch):
	import frappe

	lines = []
	monkeypatch.setattr(frappe, "logger", lambda *a, **k: SimpleNamespace(error=lines.append), raising=False)
	recipe_enrichment.log_recipe_failures(0)
	assert lines == []
	recipe_enrichment.log_recipe_failures(2)
	assert lines == ["optimus: index advice failed for 2 finding(s) or table(s) in one render"]


def test_table_recipes_skip_tables_the_report_hides(monkeypatch):
	looked_up = []
	monkeypatch.setattr(recipe_enrichment, "_read_table_evidence", lambda table: looked_up.append(table))
	tables = [
		{"table": "tabDocType", "recommended_index": {"columns": ["module", "custom"]}, "queries": 1},
		{"table": "tabSales Invoice", "recommended_index": {"columns": ["customer", "status"]}, "queries": 1},
	]
	doc = SimpleNamespace(
		name="PS-1", session_uuid="u", title="t", user="a@example.com", status="Ready",
		started_at="2026-10-08T00:00:00", stopped_at="2026-10-08T00:00:05", notes=None, top_severity="Low",
		summary_html=None, total_duration_ms=1, total_query_time_ms=1, total_queries=1, total_requests=1,
		top_queries_json="[]", table_breakdown_json=json.dumps(tables), hot_frames_json=None,
		session_time_breakdown_json=None, total_python_ms=None, total_sql_ms=None, analyzer_warnings=None,
		v5_aggregate_json="{}", actions=[], findings=[], phase_2_runs=[],
	)
	with patch("optimus.settings.get_ignored_apps", return_value=()):
		renderer.render_raw(doc, recordings=[])
	assert "tabDocType" not in looked_up and "tabSales Invoice" in looked_up


# Carried from Task 4: a gate that raises must fail CLOSED everywhere it is asked.


class _JobTimeout(Exception):
	"""Stands in for rq's JobTimeoutException (rq is not importable on the CI stub run)."""


_AI_CFG = SimpleNamespace(
	ai_enabled=True, ai_suggest_findings=True, ai_auto_suggest=True, ai_auto_suggest_max=0,
	tracked_apps=(), ai_excluded_finding_types=(),
)


def _hot_line_detail():
	return {"file": "/home/b/apps/myapp/myapp/x.py", "lineno": 3, "line_content": "x = foo(1)", "per_hit_us": 5000.0}


def _row(name, ftype, detail):
	return SimpleNamespace(
		name=name, finding_type=ftype, severity="High", title=name, customer_description="d",
		estimated_impact_ms=100.0, affected_count=1, action_ref="0", llm_fix_json=None,
		technical_detail_json=json.dumps(detail),
	)


def _record_suggest(sent):
	def suggest(payload, **kwargs):
		sent.append(payload["finding_type"])
		return {"suggestion": "x", "model": "m"}

	return suggest


def test_a_broken_gate_never_aborts_the_analyze_ai_step(monkeypatch):
	"""The Hot Line is gated and counted; the other finding still gets its suggestion."""
	monkeypatch.setattr(ai_grounding, "hot_line_gate", _boom)
	monkeypatch.setattr(analyze, "_ai_payload_for_finding", lambda row, *a, **k: {"finding_type": row.finding_type})
	monkeypatch.setattr(analyze, "_phase2_index_for", lambda *a, **k: {})
	monkeypatch.setattr(analyze, "_publish_progress", lambda *a, **k: None)
	monkeypatch.setattr(analyze, "_touch_singleflight", lambda uuid: None)
	findings = [
		{**vars(_row("hot", "Hot Line", _hot_line_detail())), "llm_fix_json": None},
		{**vars(_row("n1", "N+1 Query", {})), "llm_fix_json": None},
	]
	ctx = SimpleNamespace(session_uuid="u", docname="PS-1", findings=findings, warnings=[], actions=[])
	sent = []
	with patch("optimus.settings.get_config", return_value=_AI_CFG), \
	     patch("optimus.ai_fix.is_available", return_value=True), \
	     patch("optimus.ai_fix.suggest_fix", side_effect=_record_suggest(sent)):
		analyze._enrich_findings_with_ai_suggestions(ctx)
	assert sent == ["N+1 Query"]
	assert findings[0]["llm_fix_json"] is None and findings[1]["llm_fix_json"]
	assert any(w.startswith("AI auto-suggest: 1 finding(s) were not sent to the AI") for w in ctx.warnings)


def test_a_broken_gate_never_aborts_the_refresh(monkeypatch):
	monkeypatch.setattr(ai_grounding, "hot_line_gate", _boom)
	writes = []
	db = SimpleNamespace(set_value=lambda dt, name, *a, **k: writes.append(name), commit=lambda: None)
	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(db=db, local=SimpleNamespace(), log_error=lambda *a, **k: None))
	monkeypatch.setattr(analyze, "_ai_payload_for_finding", lambda row, *a, **k: {"finding_type": row.finding_type})
	monkeypatch.setattr(analyze, "_phase2_index_for", lambda *a, **k: {})
	rows = [_row("hot", "Hot Line", _hot_line_detail()), _row("n1", "N+1 Query", {})]
	sent = []
	with patch("optimus.settings.get_config", return_value=_AI_CFG), \
	     patch("optimus.ai_fix.is_available", return_value=True), \
	     patch("optimus.ai_fix.suggest_fix", side_effect=_record_suggest(sent)):
		out = analyze._run_ai_backfill(SimpleNamespace(findings=rows, session_uuid="u", name="PS-1"), cap=0)
	assert sent == ["N+1 Query"] and writes == ["n1"]
	assert (out["gated"], out["added"], out["failed"]) == (1, 1, 0)


def test_a_broken_gate_never_offers_a_refresh(monkeypatch):
	"""The outdated-fix footer must not promise that Refresh would redo a gated Hot Line."""
	monkeypatch.setattr(ai_grounding, "hot_line_gate", _boom)
	monkeypatch.setattr(ai_fix, "is_available", lambda section=None: True)
	with patch("optimus.settings.get_config", return_value=_AI_CFG):
		check = recipe_enrichment.make_refresh_check()
		assert check({"finding_type": "Hot Line", "technical_detail": _hot_line_detail()}) is False
		assert check({"finding_type": "N+1 Query", "technical_detail": {}}) is True


@pytest.mark.parametrize("place", ["ai_fix", "renderer", "log"])
def test_a_job_timeout_in_the_gate_still_escapes_fresh(monkeypatch, place):
	import frappe

	original = _JobTimeout("deadline")

	def interrupted(*args, **kwargs):
		raise original

	monkeypatch.setattr(safe_call, "job_timeout_types", lambda: (_JobTimeout,))
	monkeypatch.setattr(ai_grounding, "hot_line_gate", interrupted)
	monkeypatch.setattr(frappe, "logger", interrupted, raising=False)  # the "log" place: the bench-log write
	finding = {"finding_type": "Hot Line", "llm_fix": {"suggestion_html": "x"}, "technical_detail": _hot_line_detail()}
	with patch("optimus.settings.get_config", return_value=_AI_CFG), pytest.raises(_JobTimeout) as caught:
		if place == "ai_fix":
			ai_fix.llm_gate_note(finding)
		elif place == "renderer":
			recipe_enrichment.apply_finding_recipes([finding], evidence_lookup=lambda table: None)
		else:
			recipe_enrichment.log_recipe_failures(1)
	assert caught.value is not original
	assert caught.value.__context__ is None and caught.value.__cause__ is None


def _table_evidences():
	from optimus.tests.test_index_recipes import _ALL, F, _ev

	return [
		# a tracked app: Search Index for one column, ensure_indexes otherwise
		pytest.param(_ev(app="myapp", fields=_ALL), id="tracked-app"),
		pytest.param(_ev(fields=_ALL), id="erpnext-text-prefix"),
		pytest.param(_ev(fields={**_ALL, "po_no": F("Data", custom=True)}), id="custom-field"),
		pytest.param(_ev("My Notes", app="frappe", custom_doctype=True, fields=_ALL), id="ui-doctype"),
		pytest.param(_ev(dialect="postgres", fields=_ALL), id="postgres-text-left-out"),
		pytest.param(_ev("GL Entry", fields=_ALL), id="write-hot"),
		pytest.param(_ev(fields=_ALL, indexes=[("customer_index", ["customer"], False)]), id="single-index"),
		pytest.param(_ev(fields=_ALL, indexes=[("cs", ["customer", "status"], False)]), id="composite-index"),
	]


@pytest.mark.parametrize("evidence", _table_evidences())
def test_table_recipes_are_idempotent(evidence):
	"""Running the card recipes twice leaves the same cards, text prefixes included."""
	import itertools

	from optimus.tests.test_index_recipes import _ALL, _lookup

	columns = sorted(_ALL)
	tables = [
		{"table": evidence.table, "ai_index": {"x": 1}, "recommended_index": {"columns": list(combo)}}
		for k in (1, 2, 3, 5)
		for combo in itertools.islice(itertools.permutations(columns, k), 0, 400, 7)
	]
	recipe_enrichment.apply_table_recipes(tables, evidence_lookup=_lookup(evidence), tracked_apps=("myapp",))
	assert any((t.get("recommended_index") or {}).get("code") for t in tables), "no code route exercised"
	once = copy.deepcopy(tables)
	recipe_enrichment.apply_table_recipes(tables, evidence_lookup=_lookup(evidence), tracked_apps=("myapp",))
	assert tables == once


@pytest.mark.parametrize("dialect", ["mariadb", "postgres"])
def test_advice_read_back_with_its_text_prefix_gives_the_same_advice(dialect):
	"""A card built from earlier advice (remarks(255)) is advised like its bare columns."""
	from optimus.tests.test_index_recipes import _ALL, _ev, _lookup

	evidence = _ev(dialect=dialect, fields=_ALL)
	first = index_recipes.advise_table(evidence.table, ["customer", "remarks"], evidence_lookup=_lookup(evidence))
	again = index_recipes.advise_table(evidence.table, list(first.columns), evidence_lookup=_lookup(evidence))
	assert first.columns == (("customer", "remarks(255)") if dialect == "mariadb" else ("customer",))
	if dialect == "mariadb":
		assert again == first
	assert index_recipes.advise_table(
		evidence.table, ["customer", "remarks(255)"], evidence_lookup=_lookup(evidence),
	) == index_recipes.advise_table(evidence.table, ["customer", "remarks"], evidence_lookup=_lookup(evidence))


def test_a_second_pass_still_names_a_column_the_advice_left_out():
	"""Postgres leaves a text column out of the index and says so; a second pass on the
	same card advises the analyzer's columns again, so the note keeps saying so."""
	from optimus.tests.test_index_recipes import _ALL, _ev, _lookup

	evidence = _ev(dialect="postgres", fields=_ALL)
	table = {"table": evidence.table, "recommended_index": {"columns": ["customer", "remarks", "status"]}}
	recipe_enrichment.apply_table_recipes([table], evidence_lookup=_lookup(evidence))
	rec = table["recommended_index"]
	note = rec["route_note"]
	assert rec["columns"] == ["customer", "status"] and "remarks" in note
	assert rec["requested_columns"] == ["customer", "remarks", "status"]
	recipe_enrichment.apply_table_recipes([table], evidence_lookup=_lookup(evidence))
	assert table["recommended_index"]["route_note"] == note
