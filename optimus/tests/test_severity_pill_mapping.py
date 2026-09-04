# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""D.M-S8 severity-pill class mirrors finding severity.

The finding card renders a colour-coded pill via:
    <span class="severity-pill {{ f.severity | lower }}">{{ f.severity }}</span>

CSS pairs the pill colour with .severity-pill.high / .medium / .low.
A drift between the data severity and the CSS class would silently
mis-colour findings, undermining the at-a-glance triage signal.

This regression test scans rendered HTML and asserts the lower-case
class matches the data severity text inside the pill, for each
severity tier.
"""

import json
import re
from types import SimpleNamespace

from optimus import renderer


def _fake_doc(findings):
	defaults = {
		"name": "PS-severity-test",
		"session_uuid": "test-uuid",
		"title": "severity test",
		"user": "tester@example.com",
		"status": "Ready",
		"started_at": "2026-05-19T00:00:00",
		"stopped_at": "2026-05-19T00:00:05",
		"total_duration_ms": 5000,
		"total_requests": 1,
		"total_queries": 0,
		"total_query_time_ms": 0,
		"analyze_duration_ms": 100,
		"top_severity": "High",
		"summary_html": "<p>summary</p>",
		"top_queries_json": "[]",
		"table_breakdown_json": "[]",
		"analyzer_warnings": None,
		"actions": [],
		"findings": [
			SimpleNamespace(
				finding_type=f["finding_type"],
				severity=f["severity"],
				title=f["title"],
				customer_description=f["customer_description"],
				technical_detail_json=json.dumps(f.get("technical_detail", {})),
				estimated_impact_ms=f.get("estimated_impact_ms", 0),
				affected_count=f.get("affected_count", 1),
				action_ref=f.get("action_ref"),
			)
			for f in findings
		],
		"hot_frames_json": None,
		"session_time_breakdown_json": None,
		"total_python_ms": None,
		"total_sql_ms": None,
	}
	return SimpleNamespace(**defaults)


def test_severity_pill_class_matches_data_severity():
	"""For every rendered finding, the .severity-pill's lower-case
	class must match the severity label inside the pill."""
	findings = [
		{
			"finding_type": "N+1 Queries",
			"severity": "High",
			"title": "High-severity finding",
			"customer_description": "high tier desc",
			"technical_detail": {
				"callsite": {"filename": "apps/my_app/work.py", "lineno": 10, "function": "do"}
			},
			"estimated_impact_ms": 800,
		},
		{
			"finding_type": "Slow Hot Path",
			"severity": "Medium",
			"title": "Medium-severity finding",
			"customer_description": "medium tier desc",
			"technical_detail": {
				"callsite": {"filename": "apps/my_app/work.py", "lineno": 20, "function": "do2"}
			},
			"estimated_impact_ms": 300,
		},
		{
			"finding_type": "Slow Hot Path",
			"severity": "Low",
			"title": "Low-severity finding",
			"customer_description": "low tier desc",
			"technical_detail": {
				"callsite": {"filename": "apps/my_app/work.py", "lineno": 30, "function": "do3"}
			},
			"estimated_impact_ms": 100,
		},
	]
	html = renderer.render_raw(_fake_doc(findings), recordings=[])
	# Capture every (class, label) pair from rendered pills.
	pills = re.findall(
		r'<span class="severity-pill\s+(\w+)">\s*(\w+)\s*</span>',
		html,
	)
	assert pills, "expected severity-pill spans in rendered HTML"
	# At least one of each severity must render confirms the test
	# fixture flowed through the finding pipeline.
	classes_seen = {cls for cls, _label in pills}
	assert classes_seen >= {"high", "medium", "low"}, (
		f"missing severity tiers got {classes_seen!r}"
	)
	# Every pair must agree.
	for cls, label in pills:
		assert cls == label.lower(), (
			f"severity-pill class={cls!r} doesn't match label={label!r}"
		)
