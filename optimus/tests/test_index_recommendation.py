# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the concrete index recommendation:

  * `table_breakdown` co-occurrence -> `recommended_index` (a composite, ordered
    by usage frequency, capped, with the doctype derived) + `is_write_hot`;
  * the report's "Index candidate" panel rendering (recommendation + the
    `frappe.db.add_index` patch + caveats);
"""

import json
import types

# --------------------------------------------------------------------------
# table_breakdown co-occurrence → recommended_index
# --------------------------------------------------------------------------

def _rec(calls):
	return {"calls": calls}


def _q(query, duration=1.0):
	return {"query": query, "duration": duration}


class TestRecommendedIndex:
	def _analyze(self, recordings):
		from optimus.analyzers import table_breakdown as tb

		res = tb.analyze(recordings, types.SimpleNamespace())
		return {t["table"]: t for t in res.aggregate.get("table_breakdown", [])}

	def test_picks_most_common_cofilter_combo(self):
		bd = self._analyze([_rec([
			_q("SELECT name FROM `tabSales Invoice` WHERE customer = ? AND posting_date = ? AND status = ?"),
			_q("SELECT name FROM `tabSales Invoice` WHERE customer = ? AND posting_date = ? AND status = ?"),
			_q("SELECT name FROM `tabSales Invoice` WHERE customer = ? AND posting_date = ? AND status = ?"),
			_q("SELECT name FROM `tabSales Invoice` WHERE customer = ? AND posting_date = ?"),
		])])
		rec = bd["tabSales Invoice"]["recommended_index"]
		assert set(rec["columns"]) == {"customer", "posting_date", "status"}
		assert rec["doctype"] == "Sales Invoice"
		assert rec["together_count"] == 3
		assert rec["read_count"] == 4

	def test_columns_ordered_by_usage_frequency(self):
		# customer in the most reads, then posting_date, then status all
		# three filtered together in 2 reads (the dominant combo). The
		# composite is ordered by per-column hit frequency.
		bd = self._analyze([_rec([
			_q("SELECT name FROM `tabSales Invoice` WHERE customer = ? AND posting_date = ? AND status = ?"),
			_q("SELECT name FROM `tabSales Invoice` WHERE customer = ? AND posting_date = ? AND status = ?"),
			_q("SELECT name FROM `tabSales Invoice` WHERE customer = ? AND posting_date = ?"),
		])])
		rec = bd["tabSales Invoice"]["recommended_index"]
		assert rec["columns"] == ["customer", "posting_date", "status"]

	def test_caps_composite_width(self):
		from optimus.analyzers.table_breakdown import MAX_RECOMMENDED_INDEX_COLS

		cols = ["c1", "c2", "c3", "c4", "c5", "c6"]
		where = " AND ".join(f"{c} = ?" for c in cols)
		bd = self._analyze([_rec([_q(f"SELECT name FROM `tabFoo` WHERE {where}")])])
		rec = bd["tabFoo"]["recommended_index"]
		assert len(rec["columns"]) == MAX_RECOMMENDED_INDEX_COLS

	def test_no_recommendation_for_meta_table(self):
		bd = self._analyze([_rec([_q("SELECT name FROM `tabDocType` WHERE module = ?")])])
		assert bd["tabDocType"]["recommended_index"] is None
		assert bd["tabDocType"]["is_meta_table"] is True

	def test_no_recommendation_for_non_doctype_table(self):
		bd = self._analyze([_rec([
			_q("SELECT column_name FROM information_schema.columns WHERE table_name = ? AND column_name = ?")
		])])
		assert bd["information_schema.columns"]["recommended_index"] is None

	def test_no_recommendation_when_only_metadata_cols_filtered(self):
		bd = self._analyze([_rec([_q("SELECT name FROM `tabFoo` WHERE parent = ? AND parenttype = ?")])])
		# parent / parenttype are Frappe metadata columns → excluded → no combo.
		assert bd["tabFoo"]["recommended_index"] is None
		assert "parent" in bd["tabFoo"]["framework_cols_filtered"]

	def test_is_write_hot_flag(self):
		bd = self._analyze([
			_rec([_q("SELECT name FROM `tabGL Entry` WHERE account = ? AND party = ?")]),
			_rec([_q("SELECT name FROM `tabSales Invoice` WHERE customer = ?")]),
		])
		assert bd["tabGL Entry"]["is_write_hot"] is True
		assert bd["tabSales Invoice"]["is_write_hot"] is False

	def test_also_filtered_lists_leftover_columns(self):
		bd = self._analyze([_rec([
			_q("SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ?"),
			_q("SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ?"),
			_q("SELECT name FROM `tabSales Invoice` WHERE territory = ? AND company = ?"),
		])])
		rec = bd["tabSales Invoice"]["recommended_index"]
		assert set(rec["columns"]) == {"customer", "status"}
		assert "territory" in rec["also_filtered"] and "company" in rec["also_filtered"]


# --------------------------------------------------------------------------
# analyzers.base write-hot table truth table
# --------------------------------------------------------------------------

def test_is_write_hot_table_truth_table():
	from optimus.analyzers.base import is_write_hot_table

	for t in ("tabGL Entry", "tabStock Ledger Entry", "tabBin", "tabVersion", "`tabGL Entry`"):
		assert is_write_hot_table(t) is True
	for t in ("tabSales Invoice", "tabItem", "tabUser", "", None, "information_schema.columns"):
		assert is_write_hot_table(t) is False


# --------------------------------------------------------------------------
# report.html the "Index candidate" panel
# --------------------------------------------------------------------------

def _doc(table_breakdown):
	return types.SimpleNamespace(
		name="PS-idx", session_uuid="idx-uuid", title="t", user="a@example.com",
		status="Ready", started_at="2026-05-12T00:00:00", stopped_at="2026-05-12T00:00:05",
		notes=None, top_severity="Low", summary_html=None, total_duration_ms=100,
		total_query_time_ms=80, total_queries=5, total_requests=1, top_queries_json="[]",
		table_breakdown_json=json.dumps(table_breakdown), hot_frames_json=None,
		session_time_breakdown_json=None, total_python_ms=None, total_sql_ms=None,
		analyzer_warnings=None, v5_aggregate_json="{}", actions=[], findings=[], phase_2_runs=[],
	)


def _table_entry(**kw):
	base = {
		"table": "tabSales Invoice", "duration_ms": 50.0, "queries": 5,
		"read_count": 4, "write_count": 1, "read_time_ms": 50.0, "write_time_ms": 1.0,
		"index_candidates": [{"column": "customer", "sources": ["WHERE"], "hits": 4}],
		"recommended_index": {
			"columns": ["customer", "posting_date"], "doctype": "Sales Invoice",
			"together_count": 3, "read_count": 4, "also_filtered": ["status"],
		},
		"framework_cols_filtered": [], "is_meta_table": False, "is_write_hot": False,
	}
	base.update(kw)
	return base


class TestRenderedIndexCandidatePanel:
	def test_renders_recommendation_and_patch(self):
		from optimus import renderer

		html = renderer.render_raw(_doc([_table_entry()]), recordings=[])
		assert "Index candidate" in html
		assert 'frappe.db.add_index("Sales Invoice", ["customer", "posting_date"])' in html
		assert "SHOW INDEX FROM" in html
		assert "A single-column index belongs on the field instead" in html
		assert "Other columns this session filtered on" in html and "status" in html

	def test_write_hot_warning(self):
		from optimus import renderer

		html = renderer.render_raw(_doc([_table_entry(
			table="tabGL Entry", write_count=1, is_write_hot=True,
			recommended_index={"columns": ["against_voucher_type", "against_voucher_no"], "doctype": "GL Entry", "together_count": 3, "read_count": 4, "also_filtered": []},
		)]), recordings=[])
		assert "write-hot core table" in html



	def test_falls_back_to_flat_list_without_recommendation(self):
		from optimus import renderer

		entry = _table_entry(recommended_index=None)
		html = renderer.render_raw(_doc([entry]), recordings=[])
		assert "Index candidates - to speed up reads" in html
		assert ">customer</code>" in html

	def test_ai_index_block_is_never_rendered(self):
		from optimus import renderer

		entry = _table_entry()
		entry["ai_index"] = {
			"suggestion": "**Recommendation**\n\nNothing `idx_customer_date` already covers it.",
			"model": "claude-sonnet-4-6", "provider": "Anthropic",
			"generated_at": "2026-05-12T00:00:00+00:00",
		}
		html = renderer.render_raw(_doc([entry]), recordings=[])
		assert "Index advice" not in html
		assert "claude-sonnet-4-6" not in html and "already covers it" not in html

	def test_single_column_recommendation_falls_back_to_candidates(self):
		from optimus import renderer

		entry = _table_entry(recommended_index={
			"columns": ["customer"], "doctype": "Sales Invoice",
			"together_count": 3, "read_count": 4, "also_filtered": [],
		})
		html = renderer.render_raw(_doc([entry]), recordings=[])
		assert "Index candidates - to speed up reads" in html
		assert 'frappe.db.add_index("Sales Invoice", ["customer"])' not in html
