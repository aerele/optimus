# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Pure-Python unit tests for the call-tree drill-down walker.

The walker locates a finding's origin frame in the action's pyinstrument tree and
follows hottest-child links down to (a) a framework frame, (b) ``max_depth``,
(c) the signal floor (10% of origin's cumulative_ms), or (d) a leaf.

Each tree node is a dict shaped like ``analyzers/call_tree._walk_pyi_frame``:
function / filename / lineno / self_ms / cumulative_ms / children."""

from optimus.renderer import (
	_find_node_in_tree,
	_find_node_path_in_tree,
	_walk_ancestor_chain,
	_walk_drilldown_chain,
)
from optimus.renderer.finding_enrichment import _drilldown_display_rows


def _node(function, filename, lineno, cumulative_ms, children=None):
	return {
		"function": function,
		"filename": filename,
		"lineno": lineno,
		"self_ms": 0.0,
		"cumulative_ms": float(cumulative_ms),
		"children": children or [],
	}


def _screenshot_tree():
	"""Mirror the user's screenshot: looped_validate → _run_validations →
	(get_doc loop body in user code) → frappe/.../get_doc.

	The framework frame at the bottom (frappe/model/document.py::get_doc)
	is where the walker should stop."""
	return _node(
		function="<root>", filename="", lineno=0, cumulative_ms=689.0,
		children=[
			_node(
				function="looped_validate",
				filename="apps/ugly_code/ugly_code/python/common.py",
				lineno=6, cumulative_ms=689.0,
				children=[
					_node(
						function="_run_validations",
						filename="apps/ugly_code/ugly_code/python/common.py",
						lineno=15, cumulative_ms=620.0,
						children=[
							_node(
								function="_check_user_exists",
								filename="apps/ugly_code/ugly_code/python/common.py",
								lineno=19, cumulative_ms=530.0,
								children=[
									_node(
										function="get_doc",
										filename="apps/frappe/frappe/model/document.py",
										lineno=42, cumulative_ms=520.0,
										children=[],
									),
								],
							),
						],
					),
					# A second, smaller child to verify hottest-child selection.
					_node(
						function="_other_branch",
						filename="apps/ugly_code/ugly_code/python/common.py",
						lineno=99, cumulative_ms=60.0,
						children=[],
					),
				],
			),
		],
	)


class TestFindNodeInTree:
	def test_finds_by_basename_and_function(self):
		tree = _screenshot_tree()
		node = _find_node_in_tree(tree, "common.py", "looped_validate")
		assert node is not None
		assert node["function"] == "looped_validate"
		assert node["lineno"] == 6

	def test_finds_deeper_node(self):
		tree = _screenshot_tree()
		node = _find_node_in_tree(tree, "common.py", "_run_validations")
		assert node is not None
		assert node["lineno"] == 15

	def test_returns_none_when_function_missing(self):
		tree = _screenshot_tree()
		assert _find_node_in_tree(tree, "common.py", "nonexistent_fn") is None

	def test_returns_none_when_function_empty(self):
		tree = _screenshot_tree()
		assert _find_node_in_tree(tree, "common.py", "") is None

	def test_basename_match_only(self):
		"""Even if the tree stores full paths and the callsite carries a
		bench-relative path, the basename match should still find it."""
		tree = _screenshot_tree()
		node = _find_node_in_tree(tree, "common.py", "looped_validate")
		assert node is not None


class TestWalkDrilldownChain:
	def test_stops_at_framework_boundary(self):
		tree = _screenshot_tree()
		callsite = {
			"filename": "apps/ugly_code/ugly_code/python/common.py",
			"function": "looped_validate",
		}
		chain = _walk_drilldown_chain(tree, callsite)
		# Expected: _run_validations + _check_user_exists. The frappe
		# get_doc beneath them is framework stop.
		assert [(level["function"], level["filename"].rsplit("/", 1)[-1]) for level in chain] == [
			("_run_validations", "common.py"),
			("_check_user_exists", "common.py"),
		]
		# Percentages computed against the ORIGIN (looped_validate, 689ms).
		assert chain[0]["pct_of_origin"] == round(620 / 689 * 100)
		assert chain[1]["pct_of_origin"] == round(530 / 689 * 100)

	def test_picks_hottest_child(self):
		"""The looped_validate origin has two children _run_validations
		(620ms) and _other_branch (60ms). Walker must pick the 620ms one."""
		tree = _screenshot_tree()
		callsite = {
			"filename": "apps/ugly_code/ugly_code/python/common.py",
			"function": "looped_validate",
		}
		chain = _walk_drilldown_chain(tree, callsite)
		assert chain[0]["function"] == "_run_validations"

	def test_max_depth_one_truncates_chain(self):
		tree = _screenshot_tree()
		callsite = {
			"filename": "apps/ugly_code/ugly_code/python/common.py",
			"function": "looped_validate",
		}
		chain = _walk_drilldown_chain(tree, callsite, max_depth=1)
		assert len(chain) == 1
		assert chain[0]["function"] == "_run_validations"

	def test_signal_floor_drops_low_pct_children(self):
		"""Hottest child below the floor → return empty chain."""
		tree = _node(
			function="parent", filename="apps/myapp/x.py", lineno=1,
			cumulative_ms=1000.0,
			children=[
				_node(
					function="cold_child", filename="apps/myapp/x.py",
					lineno=10, cumulative_ms=50.0,  # 5% of parent below 10% floor
					children=[],
				),
			],
		)
		callsite = {"filename": "apps/myapp/x.py", "function": "parent"}
		chain = _walk_drilldown_chain(tree, callsite)
		assert chain == []

	def test_origin_not_found_returns_empty(self):
		tree = _screenshot_tree()
		callsite = {
			"filename": "apps/myapp/missing.py",
			"function": "nonexistent",
		}
		assert _walk_drilldown_chain(tree, callsite) == []

	def test_origin_in_framework_returns_empty(self):
		"""If the finding's own callsite is already framework, there's no
		point drilling further into framework code."""
		tree = _screenshot_tree()
		callsite = {
			"filename": "apps/frappe/frappe/desk/form/save.py",
			"function": "savedocs",
		}
		assert _walk_drilldown_chain(tree, callsite) == []

	def test_tracked_apps_inclusion_mode_still_stops_at_frappe(self):
		"""With tracked_apps=("ugly_code",), the frappe frame is framework via
		inclusion mode, so the walker still stops there."""
		tree = _screenshot_tree()
		callsite = {
			"filename": "apps/ugly_code/ugly_code/python/common.py",
			"function": "looped_validate",
		}
		chain = _walk_drilldown_chain(tree, callsite, tracked_apps=("ugly_code",))
		# Still 2 levels (frappe stops the chain in both modes).
		assert len(chain) == 2
		assert all("frappe" not in level["filename"] for level in chain)

	def test_tracked_apps_inclusion_mode_also_stops_at_other_user_app(self):
		"""With tracked_apps=("ugly_code",), a child in a DIFFERENT user
		app (myapp) is now framework → stop."""
		tree = _node(
			function="<root>", filename="", lineno=0, cumulative_ms=1000.0,
			children=[
				_node(
					function="parent",
					filename="apps/ugly_code/x.py", lineno=1,
					cumulative_ms=1000.0,
					children=[
						_node(
							function="cross_app",
							filename="apps/myapp/y.py", lineno=10,
							cumulative_ms=900.0,
							children=[],
						),
					],
				),
			],
		)
		callsite = {"filename": "apps/ugly_code/x.py", "function": "parent"}
		chain = _walk_drilldown_chain(tree, callsite, tracked_apps=("ugly_code",))
		# myapp/y.py is "framework" under inclusion mode → chain empty.
		assert chain == []

	def test_no_children_returns_empty(self):
		tree = _node(
			function="<root>", filename="", lineno=0, cumulative_ms=100.0,
			children=[
				_node(
					function="leaf",
					filename="apps/myapp/x.py", lineno=1,
					cumulative_ms=100.0,
					children=[],
				),
			],
		)
		callsite = {"filename": "apps/myapp/x.py", "function": "leaf"}
		assert _walk_drilldown_chain(tree, callsite) == []

	def test_zero_cumulative_ms_origin_returns_empty(self):
		tree = _node(
			function="<root>", filename="", lineno=0, cumulative_ms=0.0,
			children=[
				_node(
					function="instant",
					filename="apps/myapp/x.py", lineno=1,
					cumulative_ms=0.0,
					children=[],
				),
			],
		)
		callsite = {"filename": "apps/myapp/x.py", "function": "instant"}
		assert _walk_drilldown_chain(tree, callsite) == []

	def test_defensive_garbage_tree(self):
		assert _walk_drilldown_chain(None, {"function": "x"}) == []
		assert _walk_drilldown_chain({}, None) == []
		assert _walk_drilldown_chain({"children": "not-a-list"}, {"function": "x"}) == []


class TestFindNodePathInTree:
	def test_returns_full_ancestry_path(self):
		tree = _screenshot_tree()
		path = _find_node_path_in_tree(tree, "common.py", "_check_user_exists")
		assert [n["function"] for n in path] == [
			"<root>", "looped_validate", "_run_validations", "_check_user_exists",
		]

	def test_root_node_path_is_itself(self):
		tree = _screenshot_tree()
		path = _find_node_path_in_tree(tree, "common.py", "looped_validate")
		assert path[-1]["function"] == "looped_validate"
		assert path[0]["function"] == "<root>"

	def test_missing_function_returns_none(self):
		assert _find_node_path_in_tree(_screenshot_tree(), "common.py", "nope") is None

	def test_empty_function_returns_none(self):
		assert _find_node_path_in_tree(_screenshot_tree(), "common.py", "") is None


class TestWalkAncestorChain:
	def test_leaf_finding_shows_nested_call_path(self):
		# A finding at the deepest user frame (nothing to drill below) shows the
		# user-code call path that leads to it; the synthetic <root> and the
		# framework get_doc below are excluded; the finding is the terminal.
		tree = _screenshot_tree()
		callsite = {
			"filename": "apps/ugly_code/ugly_code/python/common.py",
			"function": "_check_user_exists",
		}
		chain = _walk_ancestor_chain(tree, callsite, tracked_apps=("ugly_code",))
		assert [c["function"] for c in chain] == [
			"looped_validate", "_run_validations", "_check_user_exists",
		]
		assert chain[-1]["function"] == "_check_user_exists"  # terminal = finding

	def test_doctype_suffix_stripped_for_lookup(self):
		tree = _screenshot_tree()
		callsite = {
			"filename": "apps/ugly_code/ugly_code/python/common.py",
			"function": "_check_user_exists (Sales Invoice)",
		}
		chain = _walk_ancestor_chain(tree, callsite, tracked_apps=("ugly_code",))
		assert [c["function"] for c in chain][-1] == "_check_user_exists"

	def test_top_level_leaf_has_no_ancestry(self):
		# A user frame called straight from framework (no user ancestor) is not
		# nested → empty, so the caller keeps the "no deeper frame" placeholder.
		tree = _node(
			function="<root>", filename="", lineno=0, cumulative_ms=100.0,
			children=[
				_node(function="bg_job", filename="apps/ugly_code/x.py",
				      lineno=1, cumulative_ms=100.0, children=[]),
			],
		)
		callsite = {"filename": "apps/ugly_code/x.py", "function": "bg_job"}
		assert _walk_ancestor_chain(tree, callsite, tracked_apps=("ugly_code",)) == []

	def test_framework_callsite_returns_empty(self):
		tree = _screenshot_tree()
		callsite = {"filename": "apps/frappe/frappe/model/document.py", "function": "get_doc"}
		assert _walk_ancestor_chain(tree, callsite, tracked_apps=("ugly_code",)) == []

	def test_missing_function_returns_empty(self):
		tree = _screenshot_tree()
		assert _walk_ancestor_chain(tree, {"filename": "apps/ugly_code/x.py"}) == []

	def test_node_not_in_tree_returns_empty(self):
		tree = _screenshot_tree()
		callsite = {"filename": "apps/ugly_code/other.py", "function": "nowhere"}
		assert _walk_ancestor_chain(tree, callsite, tracked_apps=("ugly_code",)) == []

	def test_intervening_framework_frames_are_dropped(self):
		# user → framework dispatch → user leaf: the ancestry keeps only the user
		# frames, so it still reads as a nested user-code path.
		tree = _node(
			function="<root>", filename="", lineno=0, cumulative_ms=100.0,
			children=[
				_node(function="outer", filename="apps/ugly_code/a.py", lineno=1,
				      cumulative_ms=100.0, children=[
					_node(function="dispatch", filename="apps/frappe/frappe/d.py",
					      lineno=5, cumulative_ms=90.0, children=[
						_node(function="inner_leaf", filename="apps/ugly_code/a.py",
						      lineno=9, cumulative_ms=80.0, children=[]),
					]),
				]),
			],
		)
		callsite = {"filename": "apps/ugly_code/a.py", "function": "inner_leaf"}
		chain = _walk_ancestor_chain(tree, callsite, tracked_apps=("ugly_code",))
		assert [c["function"] for c in chain] == ["outer", "inner_leaf"]

	def test_defensive_garbage(self):
		assert _walk_ancestor_chain(None, {"function": "x"}) == []
		assert _walk_ancestor_chain({}, None) == []


class TestDrilldownDisplayRows:
	"""The label rows a sub-finding carries so its collapsed row can render the
	same nested tree as a primary card."""

	def test_downward_chain_prefixes_root(self):
		detail = {
			"callsite": {"function": "looped_validate"},
			"drilldown_chain": [
				{"function": "_run_validations", "lineno": 14},
				{"function": "_check_user_exists", "lineno": 20},
			],
		}
		assert _drilldown_display_rows(detail) == [
			"looped_validate", "_run_validations:14", "_check_user_exists:20",
		]

	def test_root_uses_original_wrapper_when_present(self):
		detail = {
			"callsite": {
				"function": "_check_user_exists",
				"original_wrapper": {"function": "looped_validate (Sales Invoice)"},
			},
			"drilldown_chain": [{"function": "_inner", "lineno": 9}],
		}
		assert _drilldown_display_rows(detail)[0] == "looped_validate (Sales Invoice)"

	def test_ancestry_rows_without_root_prefix(self):
		detail = {
			"callsite": {"function": "_check_user_exists"},
			"drilldown_ancestry": [
				{"function": "looped_validate", "lineno": 8},
				{"function": "_run_enrichment", "lineno": 141},
				{"function": "_fetch_related_docs", "lineno": 146},
			],
		}
		assert _drilldown_display_rows(detail) == [
			"looped_validate:8", "_run_enrichment:141", "_fetch_related_docs:146",
		]

	def test_missing_lineno_omits_colon(self):
		detail = {
			"callsite": {"function": "a"},
			"drilldown_ancestry": [
				{"function": "a", "lineno": 1},
				{"function": "b", "lineno": None},
			],
		}
		assert _drilldown_display_rows(detail) == ["a:1", "b"]

	def test_no_chain_or_ancestry_returns_none(self):
		assert _drilldown_display_rows({"callsite": {"function": "x"}}) is None
		assert _drilldown_display_rows({"drilldown_chain": []}) is None
		assert _drilldown_display_rows(None) is None
