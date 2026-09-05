# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Validates the rendered ``report_data`` contract against the formal
JSON-schema in ``optimus/report_data_schema.json``, so a silent shape drift
becomes a test failure rather than a third-party integration breakage.
"""

import json
import os
from types import SimpleNamespace

import jsonschema

from optimus.report_context import build_report_context

_SCHEMA_PATH = os.path.join(
	os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
	"report_data_schema.json",
)


def _schema():
	with open(_SCHEMA_PATH) as fh:
		return json.load(fh)


def _doc(**overrides):
	defaults = dict(
		name="PS-schema",
		session_uuid="schema-uuid",
		title="schema fixture",
		user="t@x",
		status="Ready",
		started_at="2026-05-19T00:00:00",
		stopped_at="2026-05-19T00:00:05",
		notes=None,
		top_severity="Low",
		summary_html=None,
		total_duration_ms=100,
		total_query_time_ms=10,
		total_queries=1,
		total_requests=1,
		top_queries_json="[]",
		table_breakdown_json="[]",
		hot_frames_json=None,
		session_time_breakdown_json=None,
		total_python_ms=None,
		total_sql_ms=None,
		analyzer_warnings=None,
		v5_aggregate_json="{}",
		actions=[],
		findings=[],
		phase_2_runs=[],
	)
	defaults.update(overrides)
	return SimpleNamespace(**defaults)


def _ctx(**overrides):
	defaults = dict(
		fmt_ms=lambda v, **kw: f"{v}",
		fmt_dt=lambda v: str(v),
		generated_at="now",
		server_tz="UTC",
		severity_counts={},
		render_config={
			"hide_framework_tables": False,
			"tracked_apps": [],
			"ignored_apps": [],
			"ai_suggest_findings": True,
			"ai_suggest_indexes": True,
			"min_action_duration_ms": 0,
			"large_duration_threshold_ms": 1000,
		},
		actions=[],
		findings=[],
		findings_by_app=[],
		observational_findings_by_app=[],
	)
	defaults.update(overrides)
	return defaults


def test_report_data_schema_loads():
	"""The schema file is well-formed JSON-schema and lists every contract
	key."""
	schema = _schema()
	required = set(schema.get("required") or [])
	assert {
		"session", "tldr", "kpis", "repro", "summary", "findings",
		"line_drilldown_runs", "action_plan", "waterfall", "actions",
		"background_jobs", "doc_events", "resource", "frontend",
		"hot_frames", "slow_queries", "db", "how_to_read_items",
		"footer",
	} <= required


def test_built_report_data_matches_schema():
	"""``build_report_context(...)`` over an empty-but-valid fixture produces a
	dict that validates against the schema."""
	schema = _schema()
	out = build_report_context(_doc(), _ctx())
	# Validate raises ``jsonschema.ValidationError`` on mismatch.
	jsonschema.validate(instance=out, schema=schema)
