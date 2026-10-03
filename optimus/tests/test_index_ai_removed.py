# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""deleted the LLM index-suggestion path: no endpoint, no helper, no
prompt; refresh and capabilities no longer report an index section, and the
retired Settings toggle is read-only."""

import inspect
import json
import os

from optimus import ai_fix, ai_guardrails, ai_prompts, analyze, api

_GONE = {
	ai_fix: ("suggest_index", "_build_index_messages", "_MAX_INDEX_SAMPLE_QUERIES", "_MAX_INDEX_USER_CHARS"),
	ai_prompts: ("INDEX_SYSTEM_PROMPT", "INDEX_HEADINGS"),
	ai_guardrails: ("verify_index",),
	analyze: (
		"_table_index_sample_queries", "_table_existing_indexes", "_ai_payload_for_table",
		"_enrich_table_breakdown_with_ai_suggestions", "_run_table_index_ai_backfill",
		"AI_AUTO_INDEX_MAX_TABLES", "AI_AUTO_INDEX_TIME_BUDGET_SECONDS",
	),
	api: ("suggest_index", "_refill_indexes_for_doc"),
}


def test_index_ai_symbols_are_deleted():
	for module, names in _GONE.items():
		for name in names:
			assert not hasattr(module, name), f"{module.__name__}.{name} should be gone"


def test_indexes_section_flag_is_retired():
	assert "indexes" not in ai_fix._AI_SECTION_FLAGS


def test_refill_has_no_index_step():
	src = inspect.getsource(api.refill_ai_suggestions)
	assert "_refill_indexes_for_doc" not in src
	assert '"indexes"' not in src


def test_ai_capabilities_reports_no_index_section():
	assert '"indexes": False' in inspect.getsource(api.ai_capabilities)


def test_analyze_run_has_no_index_ai_step():
	assert "_enrich_table_breakdown_with_ai_suggestions" not in inspect.getsource(analyze)


def test_ai_suggest_indexes_setting_is_read_only_and_says_so():
	path = os.path.join(
		os.path.dirname(ai_fix.__file__), "optimus", "doctype", "optimus_settings", "optimus_settings.json",
	)
	with open(path, encoding="utf-8") as fh:
		fields = json.load(fh)["fields"]
	field = next(f for f in fields if f["fieldname"] == "ai_suggest_indexes")
	assert field.get("read_only") == 1
	assert field["description"].startswith("This setting no longer does anything.")
