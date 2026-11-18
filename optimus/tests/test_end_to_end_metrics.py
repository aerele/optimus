# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""End-to-end: the infra_pressure + frontend_timings analyzer chain on
realistic fixtures, run against merged recording + frontend data without
touching Frappe or Redis.
"""

import json
import os

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name):
	with open(os.path.join(FIXTURES_DIR, name)) as f:
		return json.load(f)


def test_full_v5_analyzer_chain_produces_findings_and_aggregates():
	from optimus.analyzers import frontend_timings, infra_pressure
	from optimus.analyzers.base import AnalyzeContext

	infra = _load("infra_pressure_session.json")
	frontend = _load("frontend_metrics_session.json")

	# Merge the two fixtures: take the frontend fixture's recordings
	# (which have duration_ms + stable UUIDs for the XHR join) and attach
	# infra dicts from the first two recordings of the infra fixture.
	recordings = [
		{
			"uuid": "rec-A",
			"action_label": "POST /api/method/save",
			"duration_ms": 320,
			"infra": infra["recordings"][0]["infra"],
		},
		{
			"uuid": "rec-B",
			"action_label": "POST /api/method/submit",
			"duration_ms": 180,
			"infra": infra["recordings"][1]["infra"],
		},
	]

	ctx = AnalyzeContext(session_uuid="e2e-test", docname="e2e")
	ctx.frontend_data = frontend["frontend_data"]

	infra_result = infra_pressure.analyze(recordings, ctx)
	ctx.merge(infra_result)

	frontend_result = frontend_timings.analyze(recordings, ctx)
	ctx.merge(frontend_result)

	# Infra findings: at least Resource Contention should fire (two
	# actions breaching 85% CPU in the fixture: 92 and 88).
	infra_findings = [
		f for f in ctx.findings if f["finding_type"] == "Resource Contention"
	]
	assert len(infra_findings) == 1

	# Frontend findings: Slow Frontend Render on the LCP 2800ms page,
	# Network Overhead on the submit XHR (1900 vs 180 backend) and
	# Heavy Response on the 512000-byte submit response.
	ft_types = {f["finding_type"] for f in ctx.findings}
	assert "Slow Frontend Render" in ft_types
	assert "Network Overhead" in ft_types
	assert "Heavy Response" in ft_types

	# Aggregates are merged into the context so renderer.render() picks
	# them up from context.aggregate -> session doc v5_aggregate_json.
	assert "infra_timeline" in ctx.aggregate
	assert "infra_summary" in ctx.aggregate
	assert "frontend_xhr_matched" in ctx.aggregate
	assert "frontend_vitals_by_page" in ctx.aggregate

	assert len(ctx.aggregate["infra_timeline"]) == 2
	assert len(ctx.aggregate["frontend_xhr_matched"]) == 2
