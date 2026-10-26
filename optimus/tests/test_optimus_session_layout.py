"""The Optimus Session form layout change must not alter the data schema."""

import importlib
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_JSON = _ROOT / "optimus" / "doctype" / "optimus_session" / "optimus_session.json"
_LAYOUT = {"Section Break", "Column Break", "Tab Break"}
_SCHEMA_KEYS = ("options", "reqd", "unique", "length", "precision", "default", "search_index", "no_copy")

# Data fields before the layout change: fieldname -> (fieldtype, schema attributes).
_STATUS_OPTIONS = "Recording\nStopping\nCapturing Background Jobs\nAnalyzing\nReady\nFailed"
_EXPECTED = {
	"session_uuid": ("Data", {"reqd": 1, "unique": 1}),
	"title": ("Data", {"reqd": 1, "length": 140}),
	"user": ("Link", {"options": "User", "reqd": 1}),
	"status": (
		"Select",
		{"options": _STATUS_OPTIONS, "reqd": 1, "default": "Recording", "search_index": 1},
	),
	"top_severity": ("Select", {"options": "\nHigh\nMedium\nLow\nNone"}),
	"notes": ("Text Editor", {}),
	"ai_steps_tokens": ("Int", {"default": "0", "no_copy": 1}),
	"ai_tokens_spent": ("Long Int", {"default": "0", "no_copy": 1}),
	"ai_refresh_count": ("Int", {"default": "0", "no_copy": 1}),
	"started_at": ("Datetime", {"reqd": 1, "search_index": 1}),
	"stopped_at": ("Datetime", {}),
	"total_duration_ms": ("Float", {"precision": "2"}),
	"total_requests": ("Int", {}),
	"total_queries": ("Int", {}),
	"total_query_time_ms": ("Float", {"precision": "2"}),
	"analyze_duration_ms": ("Float", {"precision": "2"}),
	"summary_html": ("Text Editor", {}),
	"actions": ("Table", {"options": "Optimus Action"}),
	"findings": ("Table", {"options": "Optimus Finding"}),
	"background_jobs": ("Table", {"options": "Optimus Background Job"}),
	"phase_2_runs": ("Table", {"options": "Optimus Phase Two Run"}),
	"raw_report_file": ("Attach", {}),
	"raw_report_pdf_file": ("Attach", {}),
	"recordings_file": ("Attach", {"no_copy": 1}),
	"top_queries_json": ("Long Text", {}),
	"total_python_ms": ("Float", {}),
	"total_sql_ms": ("Float", {}),
	"hot_frames_json": ("Long Text", {}),
	"session_time_breakdown_json": ("Long Text", {}),
	"table_breakdown_json": ("Long Text", {}),
	"v5_aggregate_json": ("Long Text", {}),
	"analyzer_warnings": ("Long Text", {}),
}


def _doc():
	return json.loads(_JSON.read_text())


def _data_fields():
	return {f["fieldname"]: f for f in _doc()["fields"] if f["fieldtype"] not in _LAYOUT}


def test_field_order_lists_every_field_exactly_once():
	doc = _doc()
	order = doc["field_order"]
	assert len(order) == len(set(order))
	assert order == [f["fieldname"] for f in doc["fields"]]
	assert set(_EXPECTED) <= set(order)


def test_no_data_field_added_removed_or_retyped():
	fields = _data_fields()
	assert set(fields) == set(_EXPECTED)
	for name, (fieldtype, _attrs) in _EXPECTED.items():
		assert fields[name]["fieldtype"] == fieldtype, name


def test_schema_attributes_unchanged():
	fields = _data_fields()
	for name, (_fieldtype, attrs) in _EXPECTED.items():
		for key in _SCHEMA_KEYS:
			assert fields[name].get(key) == attrs.get(key), (name, key)


def test_most_used_information_comes_first():
	order = _doc()["field_order"]
	assert order[0] == "tab_overview"
	for early, late in (
		("status", "title"),
		("raw_report_file", "title"),
		("top_severity", "notes"),
		("total_duration_ms", "started_at"),
		("title", "findings"),
		("findings", "actions"),
		("phase_2_runs", "analyze_duration_ms"),
		("analyze_duration_ms", "top_queries_json"),
	):
		assert order.index(early) < order.index(late), (early, late)


def test_json_blobs_stay_hidden_and_read_only():
	fields = _data_fields()
	for name in _EXPECTED:
		if name.endswith("_json") or name == "analyzer_warnings":
			assert fields[name].get("hidden") == 1 and fields[name].get("read_only") == 1, name


def test_patch_registered_and_importable():
	line = "optimus.patches.v0_12_87.reload_optimus_session_layout"
	lines = (_ROOT / "patches.txt").read_text().splitlines()
	assert lines.count(line) == 1
	assert callable(importlib.import_module(line).execute)
