# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Render-level test for the call-tree drill-down on finding cards.

Builds a doc with one slow action carrying a hand-crafted ``call_tree_json``
and a Slow-Hot-Path finding whose callsite is the ``looped_validate`` frame.
The rendered HTML must contain a Drill-down block that walks one or two
user-code frames below and stops at the framework boundary.
"""

import json
import types


def _tree_for_screenshot():
	"""Mirrors the user's screenshot scenario."""
	return {
		"function": "<root>", "filename": "", "lineno": 0,
		"self_ms": 0.0, "cumulative_ms": 689.0,
		"children": [
			{
				"function": "looped_validate",
				"filename": "apps/ugly_code/ugly_code/python/common.py",
				"lineno": 6, "self_ms": 5.0, "cumulative_ms": 689.0,
				"children": [
					{
						"function": "_run_validations",
						"filename": "apps/ugly_code/ugly_code/python/common.py",
						"lineno": 15, "self_ms": 10.0, "cumulative_ms": 620.0,
						"children": [
							{
								"function": "_check_user_exists",
								"filename": "apps/ugly_code/ugly_code/python/common.py",
								"lineno": 19, "self_ms": 30.0, "cumulative_ms": 530.0,
								"children": [
									{
										"function": "get_doc",
										"filename": "apps/frappe/frappe/model/document.py",
										"lineno": 42, "self_ms": 500.0, "cumulative_ms": 520.0,
										"children": [],
									},
								],
							},
						],
					},
				],
			},
		],
	}


def _finding(callsite_filename, callsite_function, callsite_lineno, action_ref="0"):
	return types.SimpleNamespace(
		finding_type="Slow Hot Path",
		severity="High",
		title="In frappe.desk.form.save.savedocs:Save, 67% of the time was spent in looped_validate",
		customer_description="",
		estimated_impact_ms=689.0,
		affected_count=1,
		action_ref=action_ref,
		technical_detail_json=json.dumps({
			"callsite": {
				"filename": callsite_filename,
				"lineno": callsite_lineno,
				"function": callsite_function,
			},
			"cumulative_ms": 689.0,
		}),
		llm_fix_json=None,
	)


def _action(action_label, recording_uuid, duration_ms, call_tree):
	return types.SimpleNamespace(
		action_label=action_label,
		event_type="HTTP Request",
		http_method="POST",
		path="/api/method/frappe.desk.form.save.savedocs",
		recording_uuid=recording_uuid,
		duration_ms=duration_ms,
		queries_count=0,
		query_time_ms=0,
		slowest_query_ms=0,
		call_tree_json=json.dumps(call_tree),
	)


def _doc(actions, findings):
	return types.SimpleNamespace(
		name="PS-dd", session_uuid="dd-uuid", title="drill-down test",
		user="a@example.com", status="Ready",
		started_at="2026-05-13T00:00:00", stopped_at="2026-05-13T00:00:01",
		notes=None, top_severity="High", summary_html=None,
		total_duration_ms=1199, total_query_time_ms=600,
		total_queries=50, total_requests=1,
		top_queries_json="[]", table_breakdown_json="[]",
		hot_frames_json="[]", session_time_breakdown_json=None,
		total_python_ms=None, total_sql_ms=None,
		analyzer_warnings=None, v5_aggregate_json="{}",
		actions=actions, findings=findings, phase_2_runs=[],
	)


class TestDrilldownRender:
	def test_drilldown_block_appears_under_finding_card(self):
		from optimus import renderer

		doc = _doc(
			actions=[_action(
				action_label="POST /api/method/frappe.desk.form.save.savedocs",
				recording_uuid="r0", duration_ms=1199,
				call_tree=_tree_for_screenshot(),
			)],
			findings=[_finding(
				callsite_filename="apps/ugly_code/ugly_code/python/common.py",
				callsite_function="looped_validate",
				callsite_lineno=6,
			)],
		)
		html = renderer.render_raw(doc, recordings=[])

		# The drill-down label appears on the card.
		assert '<div class="chain-label">Drill-down</div>' in html
		# Both user-code frames below looped_validate are surfaced.
		assert "_run_validations" in html
		assert "_check_user_exists" in html
		# Framework frame (frappe.../get_doc) does NOT appear in the drill-down.
		# (It might appear elsewhere in the report if framework data is
		# rendered, but for the drill-down block we just verify the chain
		# stops before it.)
		# The drill-down block lives inside the smoking-gun container, so
		# locate it and assert the framework path isn't inside that span.
		dd_idx = html.find("Drill-down")
		assert dd_idx > 0
		dd_segment = html[dd_idx:dd_idx + 4000]
		assert "frappe/frappe/model/document.py" not in dd_segment, (
			"framework frame leaked into the drill-down block"
		)
		# v0.7.x Phase D: drill-down chain renders as pill steps; the
		# per-step `% of parent` value isn't shown in the new layout
		# (the mock dropped it for visual cleanliness data still
		# available via the underlying drilldown_chain dict for API
		# consumers).

	def test_drilldown_renders_as_indented_tree(self):
		"""The chain renders as a nested-call tree (not the old single-line
		arrow track): the root sits first with no connector, each deeper frame
		is indented one level under a corner connector; the deepest user frame
		is the terminal (accent) node; the intermediate frame carries its own
		function:lineno."""
		from optimus import renderer

		doc = _doc(
			actions=[_action(
				action_label="POST /api/method/frappe.desk.form.save.savedocs",
				recording_uuid="r0", duration_ms=1199,
				call_tree=_tree_for_screenshot(),
			)],
			findings=[_finding(
				callsite_filename="apps/ugly_code/ugly_code/python/common.py",
				callsite_function="looped_validate",
				callsite_lineno=6,
			)],
		)
		html = renderer.render_raw(doc, recordings=[])

		# New tree container; the old pill track is gone.
		assert 'class="chain-tree"' in html
		assert 'class="chain-track"' not in html

		# Isolate the Drill-down block.
		start = html.find('chain-label">Drill-down')
		assert start > 0
		dd = html[start:html.find("chain-foot", start)]

		# Root first (no terminal styling), intermediate frame labelled with its
		# own line, deepest frame is terminal.
		assert 'class="ct-step">looped_validate</span>' in dd
		assert 'class="ct-step">_run_validations:15</span>' in dd
		assert 'ct-step terminal">_check_user_exists:19' in dd
		# Frames render in call order: root -> intermediate -> deepest.
		assert (
			dd.find("looped_validate")
			< dd.find("_run_validations:15")
			< dd.find("_check_user_exists:19")
		)
		# Two nested rows below the root, each with a corner connector and a
		# deeper indent than the last (depth carried by --ctd; padding is derived
		# in CSS so print can scale it down).
		assert dd.count("ct-branch") == 2
		assert "--ctd: 1" in dd and "--ctd: 2" in dd

	def test_call_chain_renders_as_indented_tree(self):
		"""The Phase-2 Call chain block goes through the same nested-tree macro:
		every step keeps its `qualname:lineno`, the first is the root and the
		deepest is the terminal node."""
		from optimus import renderer

		f = types.SimpleNamespace(
			finding_type="Hot Line", severity="High", title="hot line finding",
			customer_description="", estimated_impact_ms=120.0, affected_count=1,
			action_ref="0",
			technical_detail_json=json.dumps({
				"callsite": {
					"filename": "apps/ugly_code/ugly_code/python/common.py",
					"lineno": 30, "function": "compute",
				},
				"call_chain": [
					{"qualname": "compute", "lineno": 30},
					{"qualname": "_inner_loop", "lineno": 41},
					{"qualname": "_do_math", "lineno": 55},
				],
			}),
			llm_fix_json=None,
		)
		doc = _doc(
			actions=[_action("POST /x", "r0", 500, _tree_for_screenshot())],
			findings=[f],
		)
		html = renderer.render_raw(doc, recordings=[])

		start = html.find('chain-label">Call chain')
		assert start > 0
		cc = html[start:html.find("chain-foot", start)]
		assert 'class="chain-tree"' in cc
		assert 'class="ct-step">compute:30</span>' in cc          # root keeps lineno
		assert 'class="ct-step">_inner_loop:41</span>' in cc      # intermediate frame
		assert 'ct-step terminal">_do_math:55' in cc              # deepest is terminal
		# Steps render in call order: root -> intermediate -> deepest.
		assert (
			cc.find("compute:30")
			< cc.find("_inner_loop:41")
			< cc.find("_do_math:55")
		)
		assert cc.count("ct-branch") == 2                          # two nested rows
		assert "--ctd: 1" in cc and "--ctd: 2" in cc

	def test_chain_step_without_lineno_omits_colon(self):
		"""A frame whose lineno is None (a pass-through step the analyzer left
		without a line) renders as the bare function name, never `qualname:None`."""
		from optimus import renderer

		f = types.SimpleNamespace(
			finding_type="Hot Line", severity="High", title="hl",
			customer_description="", estimated_impact_ms=120.0, affected_count=1,
			action_ref="0",
			technical_detail_json=json.dumps({
				"callsite": {
					"filename": "apps/ugly_code/ugly_code/python/common.py",
					"lineno": 30, "function": "compute",
				},
				"call_chain": [
					{"qualname": "compute", "lineno": 30},
					{"qualname": "_pass_through", "lineno": None},
					{"qualname": "_do_math", "lineno": 55},
				],
			}),
			llm_fix_json=None,
		)
		doc = _doc(
			actions=[_action("POST /x", "r0", 500, _tree_for_screenshot())],
			findings=[f],
		)
		html = renderer.render_raw(doc, recordings=[])

		start = html.find('chain-label">Call chain')
		assert start > 0
		cc = html[start:html.find("chain-foot", start)]
		assert ":None" not in cc
		assert 'class="ct-step">_pass_through</span>' in cc

	def test_leaf_finding_renders_ancestry_call_path(self):
		"""A finding that sits at a leaf user frame (nothing to drill below, so the
		downward chain is empty) renders its nested call path instead of the bare
		'no deeper user-code frame' placeholder: the outer frame at the top down to
		the finding as the terminal."""
		from optimus import renderer

		# callsite = _check_user_exists, whose only child in the tree is the
		# framework get_doc -> downward chain empty -> ancestry fallback.
		f = types.SimpleNamespace(
			finding_type="N+1 Query", severity="High",
			title="Same query ran 150x at common.py:19",
			customer_description="", estimated_impact_ms=70.0, affected_count=150,
			action_ref="0",
			technical_detail_json=json.dumps({
				"callsite": {
					"filename": "apps/ugly_code/ugly_code/python/common.py",
					"lineno": 19, "function": "_check_user_exists",
				},
			}),
			llm_fix_json=None,
		)
		doc = _doc(
			actions=[_action("POST /x", "r0", 689, _tree_for_screenshot())],
			findings=[f],
		)
		html = renderer.render_raw(doc, recordings=[])

		start = html.find('chain-label">Drill-down')
		assert start > 0
		dd = html[start:html.find("chain-foot", start) + 120]
		# The nested path renders as a tree, finding is the terminal.
		assert 'class="chain-tree"' in dd
		assert 'class="ct-step">looped_validate:6</span>' in dd
		assert 'class="ct-step">_run_validations:15</span>' in dd
		assert 'ct-step terminal">_check_user_exists:19' in dd
		# Ancestry foot, not the old "no deeper user-code frame" placeholder.
		assert "nested call path to" in dd
		assert "no deeper user-code frame" not in dd

	def test_print_media_caps_indent_and_breaks_long_labels(self):
		"""On paper the card can't scroll, so the chain tree must cap its indent
		and let a long pill shrink and break, or a deep chain clips past the
		card's edge. Lock the print rules in the rendered stylesheet."""
		from optimus import renderer

		doc = _doc(
			actions=[_action("POST /x", "r0", 689, _tree_for_screenshot())],
			findings=[_finding(
				callsite_filename="apps/ugly_code/ugly_code/python/common.py",
				callsite_function="looped_validate", callsite_lineno=6,
			)],
		)
		html = renderer.render_raw(doc, recordings=[])
		# Indent is derived from --ctd so print can scale it: 16px/level on
		# screen, a gentler 6px/level on paper.
		assert "calc(var(--ctd, 0) * 16px)" in html
		assert "calc(var(--ctd, 0) * 6px)" in html
		# The pill can shrink below its content and break a long name in print.
		assert (
			"min-width: 0; overflow-wrap: break-word; word-break: break-word;"
			in html
		)

	def test_finding_without_matching_tree_node_renders_placeholder(self):
		"""A finding whose callsite matches no tree node (origin lookup fails)
		still renders the Drill-down placeholder ('no deeper user-code frame'):
		the walker returns [] and the template's placeholder branch fires."""
		from optimus import renderer

		doc = _doc(
			actions=[_action(
				action_label="POST /api/method/foo",
				recording_uuid="r0", duration_ms=500,
				call_tree=_tree_for_screenshot(),
			)],
			findings=[_finding(
				callsite_filename="apps/myapp/other.py",
				callsite_function="totally_different_function",
				callsite_lineno=1,
			)],
		)
		html = renderer.render_raw(doc, recordings=[])

		# Drill-down label IS present (the placeholder uses it).
		assert "Drill-down" in html
		# Placeholder text rendered.
		assert "no deeper user-code frame" in html
		# No actual chain entries rendered the bg of `_run_validations`
		# / framework descent shouldn't appear (we never matched an
		# origin to walk from).
		assert "% of parent" not in html

	def test_finding_without_action_ref_skips_drilldown(self):
		from optimus import renderer

		f = _finding(
			callsite_filename="apps/ugly_code/ugly_code/python/common.py",
			callsite_function="looped_validate",
			callsite_lineno=6,
			action_ref="",  # no action to look up
		)
		doc = _doc(
			actions=[_action(
				action_label="POST /api/method/foo",
				recording_uuid="r0", duration_ms=500,
				call_tree=_tree_for_screenshot(),
			)],
			findings=[f],
		)
		html = renderer.render_raw(doc, recordings=[])

		assert "Drill-down" not in html

	def test_action_without_call_tree_skips_drilldown(self):
		from optimus import renderer

		action = _action(
			action_label="POST /api/method/foo",
			recording_uuid="r0", duration_ms=500,
			call_tree={},  # empty tree
		)
		# Override to make call_tree_json empty string (more realistic for
		# pre-v0.3.0 sessions where the field didn't exist).
		action.call_tree_json = ""
		doc = _doc(
			actions=[action],
			findings=[_finding(
				callsite_filename="apps/ugly_code/ugly_code/python/common.py",
				callsite_function="looped_validate",
				callsite_lineno=6,
			)],
		)
		html = renderer.render_raw(doc, recordings=[])
		assert "Drill-down" not in html
