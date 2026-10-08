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

	def _make(*, findings=(), tables=(), user=OWNER, roles=("Optimus User",), can_read=True):
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
		monkeypatch.setattr("optimus.settings.get_config", lambda: SimpleNamespace(tracked_apps=()))
		evidence = {"tabSales Invoice": _evidence()}

		def read(table):
			seen.lookups += 1
			return evidence.get(table)

		monkeypatch.setattr(recipe_enrichment, "_read_table_evidence", read)
		return fake

	_make.seen = seen
	return _make


def _report_advice(finding_row):
	"""The advice the report computes for the same finding, with the same evidence."""
	f = {"finding_type": finding_row.finding_type, "technical_detail": json.loads(finding_row.technical_detail_json)}
	return index_recipes.advise_finding(f, evidence_lookup=lambda table: _evidence(), tracked_apps=())


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


def test_advice_failure_exports_null_and_never_the_raw_ddl(env, monkeypatch):
	def boom(*a, **kw):
		raise RuntimeError("recipe bug")

	monkeypatch.setattr(index_recipes, "advise_finding", boom)
	env(findings=[_missing_index()])
	(finding,) = api.export_session(session_uuid=SESSION_UUID)["findings"]
	assert finding["index_advice"] is None
	assert "suggested_ddl" not in finding["technical_detail"]


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
