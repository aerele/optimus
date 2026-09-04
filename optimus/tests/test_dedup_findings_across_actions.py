# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for analyze._dedupe_findings_across_actions the post-analyze
pass that collapses per-action duplicates of the same code path
(Slow Hot Path / Hook Bottleneck / Self-Time Hot Path / Repeated Hot
Frame) into a single dominant finding with an 'Also affects N other
action(s)' note.

History: a recording with N actions touching the same hot function
used to produce N near-identical Slow Hot Path cards (same drill-down,
same Phase 2 callout, same AI fix). The dedup pass collapses them
into one dominant card with the others folded into
technical_detail.merged_action_refs / merged_impact_ms.
"""

import json

from optimus.analyze import _dedupe_findings_across_actions


def _shp(
	*,
	filename="common.py",
	lineno=6,
	function="looped_validate",
	severity="Medium",
	impact_ms=700.0,
	action_ref="0",
	title=None,
	description="walltime hotspot",
):
	"""Build a Slow Hot Path finding shaped like call_tree's emitter."""
	return {
		"finding_type": "Slow Hot Path",
		"severity": severity,
		"title": title or f"In action{action_ref}, time was spent in {function}",
		"customer_description": description,
		"estimated_impact_ms": impact_ms,
		"affected_count": 1,
		"action_ref": action_ref,
		"technical_detail_json": json.dumps({
			"function": function,
			"filename": filename,
			"lineno": lineno,
			"cumulative_ms": impact_ms,
			"action_wall_time_ms": impact_ms * 1.5,
			"is_hook": False,
		}),
	}


def _hot_line(*, file="common.py", lineno=20, function="_check_user_exists", impact_ms=349.0):
	return {
		"finding_type": "Hot Line",
		"severity": "High",
		"title": f"{function}:{lineno} consumed {impact_ms}ms",
		"customer_description": "leaf hot line",
		"estimated_impact_ms": impact_ms,
		"affected_count": 100,
		"action_ref": None,
		"technical_detail_json": json.dumps({
			"dotted_path": f"my_app.x.{function}",
			"file": file,
			"lineno": lineno,
			"function": function,
			"line_content": "user = frappe.get_doc(...)",
			"total_ms": impact_ms,
			"hits": 100,
		}),
	}


def _action(idx, label):
	return {"idx": idx, "action_label": label, "path": "/api/method/foo"}


def test_two_per_action_findings_same_key_merge_to_one():
	"""Two Slow Hot Path findings with the same (filename, lineno,
	function) but different action_refs collapse to ONE dominant card."""
	findings = [
		_shp(action_ref="0", impact_ms=699.0, severity="High",
		     title="In Save, 54% of the time was spent in looped_validate"),
		_shp(action_ref="1", impact_ms=771.0, severity="Medium",
		     title="In Submit, 48% of the time was spent in looped_validate"),
	]
	actions = [_action(0, "Save"), _action(1, "Submit")]

	_dedupe_findings_across_actions(findings, actions)

	assert len(findings) == 1
	dom = findings[0]
	# Dominant = highest impact_ms. Submit was 771 > Save 699 → Submit wins.
	assert "Submit" in dom["title"]
	# Impact is summed.
	assert dom["estimated_impact_ms"] == 1470.0
	assert dom["affected_count"] == 2
	# Customer description gets the "also affects" note pointing at Save.
	assert "Also affects 1 other action" in dom["customer_description"]
	assert "**Save**" in dom["customer_description"]
	# Technical detail carries the merged metadata.
	detail = json.loads(dom["technical_detail_json"])
	assert detail["merged_count"] == 2
	assert set(detail["merged_action_refs"]) == {"0", "1"}
	assert set(detail["merged_action_labels"]) == {"Save", "Submit"}
	# Action_ref preserved on dominant.
	assert dom["action_ref"] == "1"  # Submit's ref


def test_three_per_action_findings_same_key_merge():
	"""Three actions all firing the same hot path → one dominant card
	with merged_count=3 and 'Also affects 2 other actions: …'."""
	findings = [
		_shp(action_ref="0", impact_ms=400.0),
		_shp(action_ref="1", impact_ms=800.0),  # dominant
		_shp(action_ref="2", impact_ms=300.0),
	]
	actions = [
		_action(0, "Save"), _action(1, "Submit"), _action(2, "Cancel"),
	]

	_dedupe_findings_across_actions(findings, actions)

	assert len(findings) == 1
	dom = findings[0]
	assert dom["estimated_impact_ms"] == 1500.0
	assert dom["affected_count"] == 3
	detail = json.loads(dom["technical_detail_json"])
	assert detail["merged_count"] == 3
	# The "also affects" sentence mentions the other 2 actions.
	assert "Also affects 2 other actions" in dom["customer_description"]
	assert "**Save**" in dom["customer_description"]
	assert "**Cancel**" in dom["customer_description"]


def test_different_keys_do_not_merge():
	"""Two Slow Hot Path findings on DIFFERENT functions stay as two."""
	findings = [
		_shp(function="a", filename="a.py", lineno=10, action_ref="0"),
		_shp(function="b", filename="b.py", lineno=20, action_ref="0"),
	]

	_dedupe_findings_across_actions(findings, [_action(0, "Save")])

	assert len(findings) == 2


def test_slow_hot_path_and_hot_line_same_leaf_stay_separate():
	"""Slow Hot Path and Hot Line are different finding types even if
	they target adjacent code, they don't merge."""
	findings = [
		_shp(function="_check_user_exists", filename="common.py", lineno=18, action_ref="0"),
		_hot_line(file="common.py", lineno=20, function="_check_user_exists"),
	]

	_dedupe_findings_across_actions(findings, [_action(0, "Save")])

	# Both findings preserved.
	assert len(findings) == 2
	types = {f["finding_type"] for f in findings}
	assert types == {"Slow Hot Path", "Hot Line"}


def test_single_finding_unchanged():
	"""A code path with just one finding doesn't get touched."""
	findings = [_shp(impact_ms=500.0, severity="Medium")]
	original = dict(findings[0])

	_dedupe_findings_across_actions(findings, [_action(0, "Save")])

	assert len(findings) == 1
	dom = findings[0]
	# Title / description / impact untouched.
	assert dom["title"] == original["title"]
	assert dom["customer_description"] == original["customer_description"]
	assert dom["estimated_impact_ms"] == 500.0
	# No merged_count key.
	detail = json.loads(dom["technical_detail_json"])
	assert "merged_count" not in detail
	assert "Also affects" not in dom["customer_description"]


def test_hook_bottleneck_findings_are_deduped():
	"""Hook Bottleneck is also a per-action emitter should dedupe."""
	findings = [
		{
			"finding_type": "Hook Bottleneck",
			"severity": "High",
			"title": "In Save, validate hook consumed 500ms",
			"customer_description": "hook bottleneck",
			"estimated_impact_ms": 500.0,
			"affected_count": 1,
			"action_ref": "0",
			"technical_detail_json": json.dumps({
				"function": "looped_validate",
				"filename": "common.py",
				"lineno": 6,
				"cumulative_ms": 500.0,
			}),
		},
		{
			"finding_type": "Hook Bottleneck",
			"severity": "Medium",
			"title": "In Submit, validate hook consumed 400ms",
			"customer_description": "hook bottleneck",
			"estimated_impact_ms": 400.0,
			"affected_count": 1,
			"action_ref": "1",
			"technical_detail_json": json.dumps({
				"function": "looped_validate",
				"filename": "common.py",
				"lineno": 6,
				"cumulative_ms": 400.0,
			}),
		},
	]
	actions = [_action(0, "Save"), _action(1, "Submit")]

	_dedupe_findings_across_actions(findings, actions)

	assert len(findings) == 1
	assert findings[0]["estimated_impact_ms"] == 900.0


def test_npq_findings_are_not_deduped_by_this_pass():
	"""N+1 Query findings have their own dedup logic in the n_plus_one
	analyzer; this pass leaves them alone."""
	findings = [
		{
			"finding_type": "N+1 Query",
			"severity": "High",
			"title": "Same query ran 50× at common.py:20",
			"customer_description": "n+1",
			"estimated_impact_ms": 100.0,
			"affected_count": 50,
			"action_ref": "0",
			"technical_detail_json": json.dumps({
				"function": "_check_user_exists",
				"filename": "common.py",
				"lineno": 20,
				"occurrences": 50,
			}),
		},
		{
			"finding_type": "N+1 Query",
			"severity": "High",
			"title": "Same query ran 50× at common.py:20",
			"customer_description": "n+1",
			"estimated_impact_ms": 100.0,
			"affected_count": 50,
			"action_ref": "1",
			"technical_detail_json": json.dumps({
				"function": "_check_user_exists",
				"filename": "common.py",
				"lineno": 20,
				"occurrences": 50,
			}),
		},
	]

	_dedupe_findings_across_actions(findings, [_action(0, "Save"), _action(1, "Submit")])

	# N+1 not in the dedup set both pass through.
	assert len(findings) == 2


def test_missing_action_label_falls_back_to_generic_count():
	"""When an OTHER finding's action_ref doesn't resolve to an action
	label (e.g. the actions list was rebuilt and that idx is gone),
	the 'also affects' sentence drops the label list and uses just
	'N other action(s)'. The dominant's label being missing doesn't
	matter only the others' labels are shown."""
	findings = [
		_shp(action_ref="99", impact_ms=400.0),  # OTHER missing label
		_shp(action_ref="1", impact_ms=800.0),   # DOMINANT has label
	]

	_dedupe_findings_across_actions(findings, [_action(1, "Submit")])

	assert len(findings) == 1
	dom = findings[0]
	# Generic count form because the OTHER entry has no resolvable label.
	assert "Also affects 1 other action" in dom["customer_description"]
	# No label list ("**...**") since the only "other" is unlabeled.
	assert "**" not in dom["customer_description"]


def test_empty_findings_list_is_safe():
	findings = []
	_dedupe_findings_across_actions(findings, [])
	assert findings == []


def test_dedup_dropped_findings_actually_removed_from_list():
	"""Sanity check: the dropped findings should not appear in the
	output list at all."""
	original_titles = [
		"In Save, 54% of the time was spent in looped_validate",
		"In Submit, 48% of the time was spent in looped_validate",
	]
	findings = [
		_shp(action_ref="0", impact_ms=699.0, title=original_titles[0]),
		_shp(action_ref="1", impact_ms=771.0, title=original_titles[1]),
	]

	_dedupe_findings_across_actions(findings, [_action(0, "Save"), _action(1, "Submit")])

	assert len(findings) == 1
	titles_remaining = [f["title"] for f in findings]
	assert original_titles[0] not in titles_remaining
	assert original_titles[1] in titles_remaining  # Submit was dominant
