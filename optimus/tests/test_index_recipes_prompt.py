# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The system prompt no longer teaches its own index recipe; a Slow Query prompt
carries the profiler's deterministic advice instead, inside a data block."""

import json
from types import SimpleNamespace

from optimus import ai_fix, ai_prompts, analyze
from optimus.renderer.recipe_enrichment import FieldEvidence, TableEvidence

_QUERY = "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ?"


def test_index_advice_comes_from_the_profiler_not_the_system_prompt():
	assert not hasattr(ai_prompts, "INDEX_RULES")
	for gone in ("INDEXES", "on_doctype_update", "in a patch", "search_index = 1"):
		assert gone not in ai_prompts.SYSTEM_PROMPT, gone
	assert "profiler's index advice" in ai_prompts._OUTPUT
	assert "profiler's index advice" in ai_prompts.FINDING_TYPE_HINTS["Slow Query"]


def test_slow_query_prompt_carries_the_advice_inside_a_data_block():
	finding = {
		"finding_type": "Slow Query", "title": "Slow query", "technical_detail": {"normalized_query": _QUERY},
		"index_advice": {
			"route": "ensure_indexes", "doctype": "Sales Invoice", "columns": ["customer", "status"],
			"text": "Your app's ensure_indexes() function creates the index \"idx_sales_invoice_1a2b3c4d\".",
		},
	}
	content = ai_fix._build_messages(finding)[1][-1]["content"]
	assert ai_fix._INDEX_ADVICE_HEAD in content
	after = content.split(ai_fix._INDEX_ADVICE_HEAD, 1)[1]
	assert after.split("\n", 2)[1].startswith("<data-")
	assert "Route: ensure_indexes. DocType: Sales Invoice. Columns: customer, status." in after
	assert "idx_sales_invoice_1a2b3c4d" in after
	other = dict(finding, finding_type="N+1 Query")
	assert ai_fix._INDEX_ADVICE_HEAD not in ai_fix._build_messages(other)[1][-1]["content"]


def test_the_payload_builder_attaches_the_advice_for_slow_query():
	field = FieldEvidence("Link", 0, False, False, False)
	evidence = TableEvidence(
		table="tabSales Invoice", doctype="Sales Invoice", app="erpnext", is_custom_doctype=False,
		dialect="mariadb", fields={"customer": field, "status": field},
		column_types={"name": "varchar", "customer": "varchar", "status": "varchar"},
		text_columns=frozenset(), unindexable_columns=frozenset(), indexes=(),
	)
	row = SimpleNamespace(
		finding_type="Slow Query", severity="High", title="Slow query", customer_description="d",
		estimated_impact_ms=900.0, affected_count=1, action_ref="", llm_fix_json=None,
		technical_detail_json=json.dumps({"normalized_query": _QUERY}),
	)
	payload = analyze._ai_payload_for_finding(row, {}, evidence_lookup=lambda table: evidence)
	assert payload["index_advice"]["route"] == "ensure_indexes"
	assert payload["index_advice"]["columns"] == ["customer", "status"]
	text = payload["index_advice"]["text"]
	assert "never drops an index that spans several columns" in text
	# the prompt carries no code, so it gets no instruction to save it
	assert "Save the code" not in text and "If that file already exists" not in text
