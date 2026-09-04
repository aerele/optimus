# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""D.M-S3 finding impact cannot exceed parent action wall.

Every Slow Hot Path / Hook Bottleneck finding rooted in an action carries
``action_ref`` (the action_idx as a string) and ``estimated_impact_ms``.
The latter must be ≤ that action's ``duration_ms``; a finding claiming
to consume more than the action took is nonsensical and would mislead
both the human reader and the action-plan ranking.
"""

import json
from types import SimpleNamespace

from optimus.analyzers import call_tree


def _node(function, filename, cumulative, self_ms, children):
	return {
		"function": function,
		"filename": filename,
		"lineno": 1,
		"kind": "python",
		"cumulative_ms": cumulative,
		"self_ms": self_ms,
		"children": children,
	}


def test_slow_hot_path_estimated_impact_bounded_by_action_wall():
	"""Use a synthetic tree where the hot subtree's cumulative_ms
	exceeds the action wall (cross-action inflation) the emitted
	finding's estimated_impact_ms must still be clamped."""
	tree = _node(
		"my_app.work.do",
		"apps/my_app/work.py",
		cumulative=2500,  # cross-action inflated; > action wall
		self_ms=2500,
		children=[],
	)
	findings = []
	call_tree._walk_for_findings(
		tree,
		parent_chain=[],
		action_idx=0,
		action_label="POST /api/method/do",
		action_wall_time_ms=1000,
		findings=findings,
	)
	assert findings, "expected Slow Hot Path to fire"
	for f in findings:
		assert f["estimated_impact_ms"] <= 1000, (
			f"finding {f['title']} claims {f['estimated_impact_ms']}ms "
			"but action wall was 1000ms"
		)


def test_findings_with_action_ref_respect_action_wall():
	"""Cross-check: for every finding produced by walking a tree, the
	finding's estimated_impact_ms must not exceed the action wall it
	was rooted in regardless of how the subtree's cumulative scales."""
	# Realistic-ish tree: top-level user code with two slow children.
	tree = _node(
		"my_app.views.handler",
		"apps/my_app/views.py",
		cumulative=900,
		self_ms=100,
		children=[
			_node(
				"my_app.work.heavy",
				"apps/my_app/work.py",
				cumulative=600,
				self_ms=600,
				children=[],
			),
			_node(
				"my_app.work.also_heavy",
				"apps/my_app/work.py",
				cumulative=300,
				self_ms=300,
				children=[],
			),
		],
	)
	findings = []
	call_tree._walk_for_findings(
		tree,
		parent_chain=[],
		action_idx=7,
		action_label="POST /api/method/handler",
		action_wall_time_ms=1000,
		findings=findings,
	)
	for f in findings:
		# When a finding fires, it carries action_ref = "7" and
		# estimated_impact_ms must stay <= 1000.
		assert f["estimated_impact_ms"] <= 1000
		# action_ref pinned to the right action so a downstream consumer
		# (template, action plan ranker) can cross-check by index.
		assert f["action_ref"] == "7"
