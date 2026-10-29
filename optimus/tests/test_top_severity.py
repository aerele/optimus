# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""top_severity counts only the findings the report renders."""

import json

from optimus.analyze import _compute_top_severity
from optimus.tests.test_renderer_structure_snapshot import _finding


def _memory_pressure(severity="High"):
	f = _finding(finding_type="Memory Pressure", severity=severity, title="Worker memory grew by 0MB (swap active)")
	f.technical_detail_json = json.dumps({"swap_active": True, "rss_delta_bytes": 0})
	return f


def test_finding_without_callsite_does_not_set_severity():
	assert _compute_top_severity([_memory_pressure()]) == "None"


def test_rendered_finding_wins_over_hidden_higher_one():
	assert _compute_top_severity([_memory_pressure("High"), _finding(severity="Medium")]) == "Medium"


def test_rendered_high_finding():
	assert _compute_top_severity([_finding(severity="Low"), _finding(severity="High")]) == "High"


def test_no_findings():
	assert _compute_top_severity([]) == "None"
