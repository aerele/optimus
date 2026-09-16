# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Slow Hot Path gating-clamp regression.

The finding's percentage is ``cumulative_ms / action_wall_time_ms``.
Pyinstrument's aggregated tree can sum across actions, giving pct > 100%.
The fix clamps ``pct_of_action`` to 1.0 for display and severity gating and
clamps ``estimated_impact_ms`` to the action wall, so a finding never claims to
consume more than the action took. This guards against that regressing.
"""

import json

from optimus.analyzers import call_tree


def _walk_findings(node: dict, action_wall_ms: float) -> list[dict]:
	findings: list = []
	call_tree._walk_for_findings(
		node,
		parent_chain=[],
		action_idx=0,
		action_label="POST /api/method/work",
		action_wall_time_ms=action_wall_ms,
		findings=findings,
	)
	return findings


def _node(function, filename, cumulative, children, self_ms=None):
	return {
		"function": function,
		"filename": filename,
		"lineno": 10,
		"kind": "python",
		"cumulative_ms": cumulative,
		"self_ms": cumulative if self_ms is None else self_ms,
		"children": children,
	}


def test_pct_display_clamped_to_100_percent():
	"""Cross-action aggregation can give raw_pct > 1.0. Display must
	never exceed 100% so the finding title stays readable."""
	tree = _node(
		"my_app.work.do_work",
		"apps/my_app/work.py",
		# 1500ms cumulative > 1000ms action wall (cross-action inflation).
		cumulative=1500,
		self_ms=1500,
		children=[],
	)
	findings = _walk_findings(tree, action_wall_ms=1000)
	assert findings, "expected Slow Hot Path to fire"
	# Title carries the pct_str (e.g. "100% of its wall time").
	assert "100%" in findings[0]["title"]
	# Never the un-clamped 150%.
	assert "150%" not in findings[0]["title"]


def test_estimated_impact_ms_clamped_to_action_wall():
	"""estimated_impact_ms must not exceed action_wall_time_ms a finding
	claiming to consume more than the action took is nonsensical."""
	tree = _node(
		"my_app.work.do_work",
		"apps/my_app/work.py",
		cumulative=1500,
		self_ms=1500,
		children=[],
	)
	findings = _walk_findings(tree, action_wall_ms=1000)
	assert findings
	assert findings[0]["estimated_impact_ms"] <= 1000


def test_within_action_severity_unchanged():
	"""Clamp doesn't touch findings whose pct is already <= 100%
	a true 80% hot path still reads as High (> high_pct=50%)."""
	tree = _node(
		"my_app.work.do_work",
		"apps/my_app/work.py",
		cumulative=800,  # 80% of a 1000ms action legitimate
		self_ms=800,
		children=[],
	)
	findings = _walk_findings(tree, action_wall_ms=1000)
	assert findings
	assert findings[0]["severity"] == "High"
	assert "80%" in findings[0]["title"]


def test_absolute_impact_promotes_to_high():
	"""A subtree consuming >= 2× high_ms is High even when its pct_of_action is
	just below the relative threshold (e.g. a 1.4s subtree at 49% of a 3s action
	would otherwise land Medium and lose the TL;DR headline)."""
	# 49% × 3000ms = 1470ms. With high_pct=50% (default), pct fails
	# the relative gate. But cumulative=1470 >= 2×high_ms=1000 → High.
	tree = _node(
		"my_app.validators.looped_validate",
		"apps/my_app/validators.py",
		cumulative=1470,  # below 50% of 3000 (49%) but >= 1000ms
		self_ms=1470,
		children=[],
	)
	findings = _walk_findings(tree, action_wall_ms=3000)
	assert findings, "expected a Slow Hot Path finding"
	assert findings[0]["severity"] == "High", (
		"a 1.47s subtree should be High regardless of pct without "
		"the absolute-impact escape hatch it falls to Medium and the "
		"TL;DR headline mis-ranks against smaller High findings"
	)


def test_below_absolute_threshold_stays_medium():
	"""The absolute-impact escape hatch must not promote borderline findings: a
	600ms subtree at 45% pct (below both 50% high_pct and the 1000ms floor)
	stays Medium."""
	# 45% × 1333ms ≈ 600ms cumulative. Neither rule fires:
	#   - relative: 45% < 50% high_pct
	#   - absolute: 600ms < 1000ms (2× high_ms)
	tree = _node(
		"my_app.work.borderline",
		"apps/my_app/work.py",
		cumulative=600,
		self_ms=600,
		children=[],
	)
	findings = _walk_findings(tree, action_wall_ms=1333)
	assert findings, "expected a Slow Hot Path finding (pct=45% > med_pct=25%)"
	assert findings[0]["severity"] == "Medium", (
		"borderline pct + sub-1s cumulative should stay Medium the "
		"absolute-impact rule only promotes overwhelming impact"
	)
