# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""export_session carries the deterministic index advice the report shows, never the
analyzer's stored raw DDL or the retired index AI output, and its permission gates are
unchanged (owner decision D6)."""

import json
from types import SimpleNamespace

import pytest

from optimus import api
from optimus.renderer import index_recipes, recipe_enrichment
from optimus.renderer.recipe_enrichment import FieldEvidence, TableEvidence
from optimus.tests.gate_fakes import (
	DOCNAME,
	OWNER,
	SESSION_UUID,
	FakePermissionError,
	fake_session_doc,
	install,
	make_fake_frappe,
	session_row,
)

_CALLSITE = {"filename": "apps/myapp/myapp/api.py", "lineno": 12, "function": "load"}
_RAW_DDL = "ALTER TABLE `tabSales Invoice` ADD INDEX IF NOT EXISTS `po_no_index` (`po_no`);"
_INDEX_AI = {"suggestion": "**Fix**\n\nold-index-ai: run ALTER TABLE by hand", "model": "old-model"}


def _finding(idx, ftype, detail, *, llm=None):
	return SimpleNamespace(
		idx=idx, finding_type=ftype, severity="High", title=f"{ftype} finding", customer_description="desc",
		technical_detail_json=json.dumps(detail), estimated_impact_ms=300.0, affected_count=5, action_ref="",
		llm_fix_json=json.dumps(llm) if llm else None,
	)


def _missing_index(idx=1):
	detail = {"table": "tabSales Invoice", "column": "po_no", "callsite": _CALLSITE, "suggested_ddl": _RAW_DDL}
	return _finding(idx, "Missing Index", detail, llm=_INDEX_AI)


def _table(**kw):
	base = {
		"table": "tabSales Invoice", "queries": 5, "read_count": 4,
		"recommended_index": {"columns": ["customer", "posting_date"], "doctype": "Sales Invoice"},
		"ai_index": {"suggestion": "**Recommendation**\n\nold-index-advice", "tokens": {"total_tokens": 20}},
	}
	base.update(kw)
	return base


def _evidence():
	types = {"name": "varchar", "creation": "datetime", "po_no": "varchar", "customer": "varchar", "posting_date": "date"}
	fields = {
		"po_no": FieldEvidence("Data", 0, False, False, False),
		"customer": FieldEvidence("Link", 0, False, False, False),
		"posting_date": FieldEvidence("Date", 0, False, False, False),
	}
	return TableEvidence(
		table="tabSales Invoice", doctype="Sales Invoice", app="erpnext", is_custom_doctype=False,
		dialect="mariadb", fields=fields, column_types=types, text_columns=frozenset(),
		unindexable_columns=frozenset(), indexes=(),
	)


@pytest.fixture
def env(monkeypatch):
	"""``export_session`` on a fake frappe: the session's findings and tables, real
	permission gates, and per-table evidence for tabSales Invoice."""
	seen = SimpleNamespace(lookups=0)

	def _make(*, findings=(), tables=(), user=OWNER, roles=("Optimus User",), can_read=True, tracked=()):
		doc = fake_session_doc(
			user=OWNER, title="Checkout flow", status="Ready", started_at=None, stopped_at=None,
			total_duration_ms=100, total_requests=1, total_queries=5, total_query_time_ms=80,
			analyzer_warnings=None, top_queries_json="[]", table_breakdown_json=json.dumps(list(tables)),
			findings=list(findings),
		)
		# api calls frappe.has_permission without a user: the doc read check of the caller.
		fake = make_fake_frappe(
			user=user, roles=roles, sessions={SESSION_UUID: session_row()}, docs={DOCNAME: doc},
			has_permission=lambda doctype, ptype, name, _user: can_read and (doctype, ptype, name) == (
				"Optimus Session", "read", DOCNAME,
			),
		)
		lookup = fake.db.get_value
		# Real frappe returns a dict for one fieldname with as_dict=True; the shared fake a string.
		fake.db.get_value = lambda *a, as_dict=False, **k: session_row() if as_dict else lookup(*a, **k)
		install(monkeypatch, fake)
		monkeypatch.setattr(api.ratelimit, "enforce_user_rate_limit", lambda *a, **k: None)
		monkeypatch.setattr("optimus.settings.get_config", lambda: SimpleNamespace(tracked_apps=tracked))
		evidence = {"tabSales Invoice": _evidence()}

		def read(table):
			seen.lookups += 1
			return evidence.get(table)

		monkeypatch.setattr(recipe_enrichment, "_read_table_evidence", read)
		return fake

	_make.seen = seen
	return _make


def _report_advice(finding_row, tracked=()):
	"""The advice the report computes for the same finding, with the same evidence."""
	f = {"finding_type": finding_row.finding_type, "technical_detail": json.loads(finding_row.technical_detail_json)}
	return index_recipes.advise_finding(f, evidence_lookup=lambda table: _evidence(), tracked_apps=tracked)


def test_export_drops_raw_ddl_and_retired_index_ai(env):
	env(findings=[_missing_index()], tables=[_table()])
	out = api.export_session(session_uuid=SESSION_UUID)
	blob = json.dumps(out)
	assert "suggested_ddl" not in blob
	assert "ai_index" not in blob and "old-index-advice" not in blob
	assert "llm_fix" not in blob and "old-index-ai" not in blob
	assert "ALTER TABLE" not in blob


def test_export_carries_the_report_advice_for_an_index_finding(env):
	row = _missing_index()
	env(findings=[row])
	(finding,) = api.export_session(session_uuid=SESSION_UUID)["findings"]
	advice = _report_advice(row)
	assert finding["index_advice"] == {
		"route": advice.route,
		"doctype": "Sales Invoice",
		"table": "tabSales Invoice",
		"columns": list(advice.columns),
		"index_name": (advice.entry or {}).get("index_name"),
		"text": index_recipes.finding_text(advice),
		"code": advice.code,
		"unknown": False,
	}
	assert finding["index_advice"]["route"] == index_recipes.ROUTE_ENSURE_INDEXES
	assert "make_property_setter(" in finding["index_advice"]["code"]
	assert finding["technical_detail"]["column"] == "po_no"  # the rest of the detail is kept


def test_export_runs_the_table_card_through_the_same_advisor(env):
	env(tables=[_table()])
	(table,) = api.export_session(session_uuid=SESSION_UUID)["table_breakdown"]
	rec = table["recommended_index"]
	name = index_recipes.optimus_index_name("Sales Invoice", ("customer", "posting_date"))
	assert rec["route"] == index_recipes.ROUTE_ENSURE_INDEXES and rec["index_name"] == name
	assert "frappe.db.add_index(" in rec["code"] and "index_name=" in rec["code"]
	assert "ai_index" not in table


def test_one_evidence_read_per_table(env):
	env(findings=[_missing_index(1), _missing_index(2)], tables=[_table()])
	api.export_session(session_uuid=SESSION_UUID)
	assert env.seen.lookups == 1


def test_advice_failure_exports_what_the_report_shows(env, monkeypatch):
	"""Task 7 fix round 1: the export uses the report's failure note, never the stored hint
	or raw DDL, and the failure is logged once like a render's."""
	import frappe

	def boom(*a, **kw):
		raise RuntimeError("recipe bug")

	lines = []
	monkeypatch.setattr(frappe, "logger", lambda *a, **k: SimpleNamespace(warning=lines.append), raising=False)
	monkeypatch.setattr(index_recipes, "advise_finding", boom)
	row = _missing_index()
	env(findings=[row])
	(finding,) = api.export_session(session_uuid=SESSION_UUID)["findings"]
	report = {"finding_type": row.finding_type, "technical_detail": json.loads(row.technical_detail_json)}
	recipe_enrichment.apply_finding_recipes([report], evidence_lookup=lambda table: _evidence())
	assert finding["technical_detail"]["fix_hint"] == report["technical_detail"]["fix_hint"]
	assert finding["technical_detail"]["fix_hint"] == recipe_enrichment.RECIPE_FAILED_HINT
	assert finding["index_advice"] == {
		"route": index_recipes.ROUTE_NO_CODE, "doctype": "Sales Invoice", "table": "tabSales Invoice",
		"columns": [], "index_name": None, "text": recipe_enrichment.RECIPE_FAILED_HINT, "code": None,
		"unknown": True,
	}
	assert "suggested_ddl" not in finding["technical_detail"] and "suggested_ddl" not in report["technical_detail"]
	assert lines == ["optimus: index advice failed for 1 finding(s) or table(s) in one export"]


def test_a_failing_card_advisor_is_counted_in_the_export_log(env, monkeypatch):
	import frappe

	def boom(*a, **kw):
		raise RuntimeError("card bug")

	lines = []
	monkeypatch.setattr(frappe, "logger", lambda *a, **k: SimpleNamespace(warning=lines.append), raising=False)
	monkeypatch.setattr(index_recipes, "advise_table", boom)
	env(tables=[_table()])
	(table,) = api.export_session(session_uuid=SESSION_UUID)["table_breakdown"]
	assert table["recommended_index"]["route_note"] == recipe_enrichment.RECIPE_FAILED_CARD_NOTE
	assert lines == ["optimus: index advice failed for 1 finding(s) or table(s) in one export"]


def test_an_export_without_failures_logs_nothing(env, monkeypatch):
	import frappe

	lines = []
	monkeypatch.setattr(frappe, "logger", lambda *a, **k: SimpleNamespace(warning=lines.append), raising=False)
	env(findings=[_missing_index()], tables=[_table()])
	api.export_session(session_uuid=SESSION_UUID)
	assert lines == []


def test_a_failing_table_advisor_still_drops_ai_index(env, monkeypatch):
	def boom(*a, **kw):
		raise RuntimeError("card bug")

	monkeypatch.setattr(recipe_enrichment, "apply_table_recipes", boom)
	env(tables=[_table()])
	(table,) = api.export_session(session_uuid=SESSION_UUID)["table_breakdown"]
	assert "ai_index" not in table


def test_index_finding_on_a_non_doctype_table_exports_null_advice(env):
	detail = {"table": "information_schema.tables", "column": "x", "suggested_ddl": _RAW_DDL}
	env(findings=[_finding(1, "Missing Index", detail)])
	(finding,) = api.export_session(session_uuid=SESSION_UUID)["findings"]
	assert finding["index_advice"] is None and "suggested_ddl" not in finding["technical_detail"]


def test_other_findings_are_exported_unchanged(env):
	detail = {"callsite": _CALLSITE, "fix_hint": "Batch the calls."}
	env(findings=[_finding(1, "N+1 Query", detail, llm={"suggestion": "x", "model": "m"})])
	(finding,) = api.export_session(session_uuid=SESSION_UUID)["findings"]
	assert "index_advice" not in finding
	assert finding["technical_detail"] == detail


# --- the permission gates are unchanged -----------------------------------------------------


def test_a_reader_without_access_is_refused_before_any_advice(env):
	env(findings=[_missing_index()], can_read=False)
	with pytest.raises(FakePermissionError):
		api.export_session(session_uuid=SESSION_UUID)
	assert env.seen.lookups == 0


def test_a_read_sharee_who_is_not_the_recording_user_is_refused(env):
	sharee = "sharee@example.com"
	fake = env(findings=[_missing_index()], user=sharee)
	with pytest.raises(FakePermissionError):
		api.export_session(session_uuid=SESSION_UUID)
	assert fake.spies.throws[-1]["msg"] == "You can only export your own sessions."
	assert env.seen.lookups == 0


def test_a_system_manager_may_export_another_users_session(env):
	admin = "admin@example.com"
	env(findings=[_missing_index()], user=admin, roles=("System Manager",))
	out = api.export_session(session_uuid=SESSION_UUID)
	assert out["findings"][0]["index_advice"] is not None


def test_index_finding_fix_hint_is_the_report_text_never_the_stored_hint(env):
	"""The export's fix_hint is the report's text, so a no_code advice never sits next to
	the analyzer's stored "Add an index" hint."""
	stored = "Add an index on the WHERE/JOIN columns of this query."
	query = "select `name` from `tabSales Invoice` where `customer` like ?"
	detail = {"table": "tabSales Invoice", "normalized_query": query, "explain_row": {"type": "ALL"}, "fix_hint": stored}
	row = _finding(1, "Full Table Scan", detail)
	env(findings=[row])
	(finding,) = api.export_session(session_uuid=SESSION_UUID)["findings"]
	report = {"finding_type": "Full Table Scan", "technical_detail": json.loads(row.technical_detail_json)}
	recipe_enrichment.apply_finding_recipes([report], evidence_lookup=lambda table: _evidence())
	assert finding["index_advice"]["route"] == index_recipes.ROUTE_NO_CODE
	assert finding["technical_detail"]["fix_hint"] == finding["index_advice"]["text"]
	assert finding["technical_detail"]["fix_hint"] == report["technical_detail"]["fix_hint"]
	assert stored not in json.dumps(finding)


def test_index_finding_without_advice_keeps_the_stored_hint_as_the_report_does(env):
	detail = {"table": "information_schema.tables", "fix_hint": "Add an index on the WHERE/JOIN columns of this query."}
	env(findings=[_finding(1, "Full Table Scan", detail)])
	(finding,) = api.export_session(session_uuid=SESSION_UUID)["findings"]
	assert finding["index_advice"] is None
	assert finding["technical_detail"]["fix_hint"] == detail["fix_hint"]


def test_export_reads_tracked_apps_like_the_report(env):
	"""With erpnext tracked, the advice differs from the untracked one; the export matches
	the report's advice for the tracked scope, finding and card alike."""
	row = _missing_index()
	tracked, untracked = _report_advice(row, ("erpnext",)), _report_advice(row)
	assert (tracked.route, tracked.code) != (untracked.route, untracked.code)
	env(findings=[row], tables=[_table()], tracked=("erpnext",))
	out = api.export_session(session_uuid=SESSION_UUID)
	assert out["findings"][0]["index_advice"]["route"] == tracked.route
	assert out["findings"][0]["index_advice"]["code"] == tracked.code
	card = index_recipes.advise_table(
		"tabSales Invoice", ["customer", "posting_date"], evidence_lookup=lambda table: _evidence(),
		tracked_apps=("erpnext",),
	)
	card_untracked = index_recipes.advise_table(
		"tabSales Invoice", ["customer", "posting_date"], evidence_lookup=lambda table: _evidence(),
	)
	assert card.code != card_untracked.code
	assert out["table_breakdown"][0]["recommended_index"]["code"] == card.code


# --- T12: one shared advice step (M4), the report's titles, one parse per query (PF2) ------


def test_export_and_report_share_one_advice_step(env):
	"""M4: the export dict is recipe_enrichment.export_advice's, the one the report uses."""
	row = _missing_index()
	env(findings=[row])
	(finding,) = api.export_session(session_uuid=SESSION_UUID)["findings"]
	report = {"finding_type": row.finding_type, "technical_detail": json.loads(row.technical_detail_json)}
	advice, failed = recipe_enrichment.export_advice(report, evidence_lookup=lambda table: _evidence())
	assert not failed and finding["index_advice"] == advice


def test_a_no_code_export_carries_the_reports_title_and_description(env, monkeypatch):
	"""U1: the export never pairs a no-code advice with the stored "Add index" title."""
	import dataclasses

	from optimus.renderer.recipe_enrichment import IndexEvidence

	indexed = dataclasses.replace(_evidence(), indexes=(IndexEvidence("po_no", ("po_no",), False),))
	row = _missing_index()
	row.title = "Add index on tabSales Invoice(po_no)"
	row.customer_description = "Ask your developer to add this index in a database migration."
	env(findings=[row])
	monkeypatch.setattr(recipe_enrichment, "_read_table_evidence", lambda table: indexed)
	(finding,) = api.export_session(session_uuid=SESSION_UUID)["findings"]
	report = {
		"finding_type": row.finding_type, "title": row.title, "customer_description": row.customer_description,
		"technical_detail": json.loads(row.technical_detail_json),
	}
	recipe_enrichment.apply_finding_recipes([report], evidence_lookup=lambda table: indexed)
	assert finding["index_advice"]["route"] == index_recipes.ROUTE_NO_CODE
	assert finding["title"] == report["title"] == "Index on tabSales Invoice(po_no): no new index recommended"
	assert finding["customer_description"] == report["customer_description"]
	assert "action_title" not in finding  # a render-only key


def test_an_export_parses_each_query_once(env, monkeypatch):
	"""PF2: the export passes a per-export parser, so two findings on one query parse it
	once, aliases included."""
	parsed, aliased = [], []
	real_parse, real_aliases = index_recipes.parse_query, index_recipes.table_aliases
	monkeypatch.setattr(index_recipes, "parse_query", lambda q: parsed.append(q) or real_parse(q))
	monkeypatch.setattr(index_recipes, "table_aliases", lambda q: aliased.append(q) or real_aliases(q))
	query = "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND posting_date > ?"
	detail = {"table": "tabSales Invoice", "normalized_query": query}
	env(findings=[_finding(1, "Full Table Scan", detail), _finding(2, "Low Filter Ratio", detail)])
	api.export_session(session_uuid=SESSION_UUID)
	assert parsed == [query] and aliased == [query]
