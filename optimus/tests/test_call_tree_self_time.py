# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Self time through the call_tree analyzer: walker, prune, soft cap, leaderboard, totals.

Frames are plain classes shaped like pyinstrument's (``function``, ``file_path_short``,
``line_no``, ``time``, ``children``) with no ``self_time``, ``total_self_time`` or
``is_synthetic``, so these run without pyinstrument. A real pyinstrument frame gets one
``[self]`` child per sample; ``_fn`` builds the same shape.
"""

import json

import pytest

from optimus.analyzers import call_tree
from optimus.analyzers.base import AnalyzeContext

APP = "my_app/work.py"


class _Frame:
	def __init__(self, function, path, time_s, children=()):
		self.function = function
		self.file_path_short = path
		self.line_no = 1
		self.time = time_s
		self.children = list(children)


class _Session:
	def __init__(self, root):
		self._root = root

	def root_frame(self):
		return self._root


def _fn(function, own_ms, children=(), path=APP):
	"""A frame owning ``own_ms``, shown as two ``[self]`` samples like pyinstrument does."""
	kids = list(children)
	synthetic = [_Frame("[self]", path, own_ms / 2000.0) for _ in range(2)] if own_ms else []
	total_s = own_ms / 1000.0 + sum(k.time for k in kids)
	return _Frame(function, path, total_s, synthetic + kids)


def _names(node):
	yield node["function"]
	for child in node["children"]:
		yield from _names(child)


def _nodes(node):
	yield node
	for child in node["children"]:
		yield from _nodes(child)


def _python_self(tree):
	return sum(n["self_ms"] for n in _nodes(tree) if n["kind"] == "python")


def _run(sessions, wall_ms=None, calls=None):
	"""Run call_tree.analyze over fake sessions; one action per session."""
	recs = [{"pyi_session": s, "calls": list((calls or {}).get(i, []))} for i, s in enumerate(sessions)]
	acts = []
	for i, s in enumerate(sessions):
		root = s.root_frame() if hasattr(s, "root_frame") else None
		wall = (wall_ms or {}).get(i) or (root.time * 1000 if root is not None else 0)
		acts.append({"action_label": f"/api/method/a{i}", "duration_ms": wall})
	ctx = AnalyzeContext(session_uuid="u", docname="d", actions=acts)
	result = call_tree.analyze(recs, ctx)
	return result, ctx


class TestWalker:
	def test_synthetic_children_are_dropped_and_self_is_derived(self):
		root = _fn("<module>", 0, [_fn("heavy", 300, [_fn("helper", 50)])])
		tree = call_tree._pyi_to_dict_tree(_Session(root))
		assert "[self]" not in list(_names(tree))
		heavy = tree["children"][0]
		assert heavy["self_ms"] == pytest.approx(300, abs=1e-3)
		assert heavy["cumulative_ms"] == pytest.approx(350, abs=1e-3)
		assert heavy["children"][0]["self_ms"] == pytest.approx(50, abs=1e-3)
		assert _python_self(tree) == pytest.approx(tree["cumulative_ms"], abs=1e-3)

	def test_values_are_rounded_to_three_decimals(self):
		tree = call_tree._pyi_to_dict_tree(_Session(_Frame("f", APP, 0.0123456789)))
		assert tree["cumulative_ms"] == 12.346
		assert tree["self_ms"] == 12.346

	def test_children_longer_than_parent_clamp_self_to_zero(self):
		kids = [_Frame("a", APP, 0.060), _Frame("b", APP, 0.060)]
		tree = call_tree._pyi_to_dict_tree(_Session(_Frame("p", APP, 0.100, kids)))
		assert tree["self_ms"] == 0
		assert all(n["self_ms"] >= 0 for n in _nodes(tree))

	@pytest.mark.parametrize("name", ["[self]", "[await]", "[out-of-context]", "[root]"])
	def test_synthetic_names_dropped_without_the_flag(self, name):
		parent = _Frame("p", APP, 0.1, [_Frame(name, APP, 0.04), _Frame("real", APP, 0.06)])
		tree = call_tree._pyi_to_dict_tree(_Session(parent))
		assert [c["function"] for c in tree["children"]] == ["real"]
		assert tree["self_ms"] == 40

	def test_is_synthetic_flag_wins_over_the_name(self):
		kept = _Frame("looks_real", APP, 0.04)
		kept.is_synthetic = False
		dropped = _Frame("odd_name", APP, 0.03)
		dropped.is_synthetic = True
		tree = call_tree._pyi_to_dict_tree(_Session(_Frame("p", APP, 0.1, [kept, dropped])))
		assert [c["function"] for c in tree["children"]] == ["looks_real"]
		assert tree["self_ms"] == 60

	def test_a_frame_without_time_fails_loudly(self):
		class _NoTime:
			function = "f"
			file_path_short = APP
			line_no = 1
			children = ()

		with pytest.raises(AttributeError):
			call_tree._pyi_to_dict_tree(_Session(_NoTime()))

	def test_dict_fixture_path_is_unchanged(self):
		fixture = {"root": {"function": "<root>", "self_ms": 7, "cumulative_ms": 9, "children": []}}
		tree = call_tree._pyi_to_dict_tree(fixture)
		assert tree["self_ms"] == 7 and tree["cumulative_ms"] == 9

	def test_empty_profile_is_an_empty_root(self):
		class _Empty:
			def root_frame(self):
				return None

		assert call_tree._pyi_to_dict_tree(_Empty())["function"] == "<root>"


def _sql(ms):
	return {"query": "SELECT 1", "normalized_query": "SELECT ?", "duration": ms, "stack": []}


class TestPruneAndSoftCap:
	def _tree(self):
		cold = [_fn(f"cold{i}", 3, path=f"my_app/c{i}.py") for i in range(5)]
		hot = _fn("hot", 400, [_fn("inner", 100)])
		root = _fn("<module>", 10, [hot] + cold)
		return call_tree.reconcile(_Session(root), [_sql(40), _sql(60)], 600)

	def test_prune_placeholder_self_is_the_dropped_cumulative(self):
		tree = self._tree()
		before = _python_self(tree)
		call_tree._prune(tree, 1000, 0.005)
		placeholder = next(n for n in _nodes(tree) if n["function"].startswith("[other"))
		assert placeholder["self_ms"] == pytest.approx(placeholder["cumulative_ms"])
		assert placeholder["self_ms"] == pytest.approx(15, abs=1e-3)
		assert _python_self(tree) == pytest.approx(before, abs=1e-3)
		assert _python_self(tree) == pytest.approx(tree["cumulative_ms"], abs=1e-3)

	def test_soft_cap_keeps_python_self_with_sql_leaves(self):
		root = _fn("<module>", 0, [_fn(f"f{i}", 10, path=f"my_app/m{i}.py") for i in range(6)])
		tree = call_tree.reconcile(_Session(root), [_sql(5) for _ in range(4)], 60)
		before = _python_self(tree)
		call_tree._soft_cap_nodes(tree, 3)
		assert any("more frames omitted" in n["function"] for n in tree["children"])
		assert _python_self(tree) == pytest.approx(before, abs=1e-3)

	def test_soft_cap_placeholder_ignores_omitted_sql_leaf(self):
		python_child = {
			"function": "py",
			"filename": APP,
			"lineno": 1,
			"self_ms": 30.0,
			"cumulative_ms": 30.0,
			"kind": "python",
			"children": [],
		}
		node = {
			"function": "<root>",
			"filename": "",
			"lineno": 0,
			"self_ms": 0.0,
			"cumulative_ms": 50.0,
			"kind": "python",
			"children": [python_child, call_tree._make_sql_leaf(_sql(20), partial_match=False)],
		}
		call_tree._soft_cap_nodes(node, 1)
		placeholder = next(n for n in node["children"] if "more frames omitted" in n["function"])
		assert placeholder["cumulative_ms"] == 50.0
		assert placeholder["self_ms"] == 30.0


class TestAnalyzeEndToEnd:
	def _heavy_action(self, own_ms=300):
		return _Session(
			_fn("<module>", 0, [_fn("heavy", own_ms, [_fn("helper", 50, path="my_app/util.py")])])
		)

	def test_user_rows_have_self_time_and_the_flag(self):
		result, _ = _run([self._heavy_action()])
		rows = {r["function"].split("::")[-1]: r for r in result.aggregate["hot_frames"]}
		heavy = rows["heavy"]
		assert heavy["total_ms"] == pytest.approx(300, abs=0.01)
		assert heavy["total_cumulative_ms"] == pytest.approx(350, abs=0.01)
		assert heavy["self_time_measured"] is True
		assert rows["helper"]["total_ms"] == pytest.approx(50, abs=0.01)

	def test_python_ms_excludes_sql_and_fits_the_wall_time(self):
		session = self._heavy_action()
		result, ctx = _run([session], calls={0: [_sql(100)]})
		agg = result.aggregate
		assert agg["total_sql_ms"] == 100
		# heavy's 300ms of own time includes the 100ms it waited on SQL.
		assert agg["total_python_ms"] == pytest.approx(250, abs=0.01)
		wall = sum(a["duration_ms"] for a in ctx.actions)
		assert agg["total_python_ms"] + agg["total_sql_ms"] <= wall * 1.01

	def test_python_ms_never_negative(self):
		result, _ = _run([self._heavy_action(own_ms=10)], calls={0: [_sql(500)]})
		assert result.aggregate["total_python_ms"] == 0

	def test_sql_is_removed_per_action_not_once_for_the_session(self):
		light = _Session(_fn("<module>", 0, [_fn("light", 20, path="my_app/light.py")]))
		result, _ = _run(
			[self._heavy_action(), light], calls={0: [_sql(100)], 1: [_sql(500)]}
		)
		agg = result.aggregate
		assert agg["total_sql_ms"] == 600
		# 350 own - 100 sql = 250; the SQL-heavy action clamps at 0 (20 - 500), it does not go negative.
		assert agg["total_python_ms"] == pytest.approx(250, abs=0.01)
		assert agg["session_time_breakdown"]["python_ms"] == agg["total_python_ms"]

	def test_sql_heavy_action_does_not_zero_the_other_actions(self):
		light = _Session(_fn("<module>", 0, [_fn("light", 20, path="my_app/light.py")]))
		result, _ = _run([self._heavy_action(), light], calls={1: [_sql(5000)]})
		assert result.aggregate["total_python_ms"] == pytest.approx(350, abs=0.01)

	def test_sql_heavy_session_is_not_reported_as_missing_self_time(self):
		light = _Session(_fn("<module>", 0, [_fn("light", 20, path="my_app/light.py")]))
		result, ctx = _run([light], calls={0: [_sql(500)]})
		assert result.aggregate["total_python_ms"] == 0
		assert all(r["self_time_measured"] for r in result.aggregate["hot_frames"])
		assert not any("self time" in w.lower() for w in ctx.warnings)

	def test_sql_of_an_action_without_a_tree_does_not_reduce_python_ms(self):
		sessions = [self._heavy_action(), None]
		result, _ = _run(sessions, wall_ms={0: 350, 1: 200}, calls={1: [_sql(200)]})
		assert result.aggregate["total_sql_ms"] == 200
		assert result.aggregate["total_python_ms"] == pytest.approx(350, abs=0.01)

	def test_sql_of_a_failed_reconciliation_does_not_reduce_python_ms(self, monkeypatch):
		real_cap = call_tree._soft_cap_nodes
		seen = []

		def flaky(tree, max_nodes):
			seen.append(1)
			if len(seen) == 1:
				raise RuntimeError("boom")
			return real_cap(tree, max_nodes)

		monkeypatch.setattr(call_tree, "_soft_cap_nodes", flaky)
		result, ctx = _run([self._heavy_action(), self._heavy_action()], calls={0: [_sql(200)]})
		assert any("Reconciliation failed" in w for w in ctx.warnings)
		assert result.aggregate["total_python_ms"] == pytest.approx(350, abs=0.01)

	def test_repeated_hot_frame_comes_back(self):
		sessions = [_Session(_fn("<module>", 0, [_fn("heavy", 200)])) for _ in range(3)]
		result, _ = _run(sessions)
		titles = [f["title"] for f in result.findings if f["finding_type"] == "Repeated Hot Frame"]
		assert len(titles) == 1 and "heavy" in titles[0]

	def test_recursion_counts_once_in_total(self):
		rec = _fn("rec", 100, [_fn("rec", 100, [_fn("rec", 100, [_fn("rec", 100)])])])
		session = _Session(_fn("<module>", 0, [rec]))
		result, ctx = _run([session, _Session(_fn("<module>", 0, [_fn("rec", 50)]))])
		row = next(r for r in result.aggregate["hot_frames"] if r["function"].endswith("::rec"))
		assert row["total_ms"] == pytest.approx(450, abs=0.01)
		assert row["total_cumulative_ms"] == pytest.approx(450, abs=0.01)
		assert row["occurrences"] == 5
		assert row["total_cumulative_ms"] <= sum(a["duration_ms"] for a in ctx.actions) + 1e-6

	def test_reentrant_function_through_a_different_function_counts_once(self):
		inner = _fn("a", 100)
		mid = _fn("b", 100, [inner])
		session = _Session(_fn("<module>", 0, [_fn("a", 100, [mid])]))
		result, _ = _run([session])
		row = next(r for r in result.aggregate["hot_frames"] if r["function"].endswith("::a"))
		assert row["total_cumulative_ms"] == pytest.approx(300, abs=0.01)
		assert row["total_ms"] == pytest.approx(200, abs=0.01)

	def test_sibling_calls_both_count_in_total(self):
		session = _Session(_fn("<module>", 0, [_fn("a", 100), _fn("a", 60)]))
		result, _ = _run([session])
		row = next(r for r in result.aggregate["hot_frames"] if r["function"].endswith("::a"))
		assert row["total_cumulative_ms"] == pytest.approx(160, abs=0.01)

	def test_no_self_time_clears_the_flag_and_warns(self):
		fixture = {
			"root": {
				"function": "<root>",
				"filename": "",
				"lineno": 0,
				"self_ms": 0,
				"cumulative_ms": 500,
				"children": [
					{
						"function": "heavy",
						"filename": APP,
						"lineno": 1,
						"self_ms": 0,
						"cumulative_ms": 500,
						"children": [],
					}
				],
			}
		}
		result, ctx = _run([fixture], wall_ms={0: 500})
		assert result.aggregate["hot_frames"]
		assert not any(r.get("self_time_measured") for r in result.aggregate["hot_frames"])
		assert any("self time" in w.lower() for w in ctx.warnings)

	def test_measured_self_time_adds_no_warning(self):
		_, ctx = _run([self._heavy_action()])
		assert not any("self time" in w.lower() for w in ctx.warnings)

	def test_call_tree_json_has_no_self_nodes(self):
		_, ctx = _run([self._heavy_action()])
		tree = json.loads(ctx.actions[0]["call_tree_json"])
		assert "[self]" not in list(_names(tree))


class TestRankingByTotalWhenSelfIsNotMeasured:
	@staticmethod
	def _tree():
		"""25 user functions whose Self order is the reverse of their Total order.

		The root's time (5000) is far above the self times (550), so self time is not
		consistent with the sampled time and the ranking stays on Total.
		"""
		kids = []
		for i in range(25):
			kids.append(
				{
					"function": f"fn{i:02d}",
					"filename": f"my_app/m{i:02d}.py",
					"lineno": 1,
					"kind": "python",
					"self_ms": 10 + i,
					"cumulative_ms": 1000 - 10 * i,
					"children": [],
				}
			)
		return {
			"function": "<root>",
			"filename": "",
			"lineno": 0,
			"kind": "python",
			"self_ms": 0,
			"cumulative_ms": 5000,
			"children": kids,
		}

	def test_cap_and_stored_order_follow_total(self):
		assert not call_tree._self_time_measured([self._tree()])
		_, board = call_tree._aggregate_hot_frames([self._tree()], None, frozenset({"my_app"}))
		names = [r["function"].split("::")[1] for r in board if not r["is_framework"]]
		assert names == [f"fn{i:02d}" for i in range(20)]

	def test_intermediate_trim_follows_total(self):
		cap = call_tree.HOT_FRAMES_INTERMEDIATE_CAP
		trees = []
		for i in range(cap * 2 + 5):
			trees.append(
				{
					"function": "<root>",
					"filename": "",
					"lineno": 0,
					"kind": "python",
					"self_ms": 0,
					"cumulative_ms": 1,
					"children": [
						{
							"function": f"f{i}",
							"filename": f"my_app/m{i}.py",
							"lineno": 1,
							"kind": "python",
							# Self rises with i while Total falls with i.
							"self_ms": 1 + i,
							"cumulative_ms": 100000 - i,
							"children": [],
						}
					],
				}
			)
		_, board = call_tree._aggregate_hot_frames(trees, None, frozenset({"my_app"}))
		kept = {r["function"].split("::")[1] for r in board}
		assert "f0" in kept and "f1" in kept


def _kid(function, path, own_ms, children=()):
	return {
		"function": function,
		"filename": path,
		"lineno": 1,
		"kind": "python",
		"self_ms": own_ms,
		"cumulative_ms": own_ms + sum(c["cumulative_ms"] for c in children),
		"children": list(children),
	}


def _root(kids, cumulative_ms=None):
	return {
		"function": "<root>",
		"filename": "",
		"lineno": 0,
		"kind": "python",
		"self_ms": 0,
		"cumulative_ms": (
			sum(k["cumulative_ms"] for k in kids) if cumulative_ms is None else cumulative_ms
		),
		"children": list(kids),
	}


INSTALLED = frozenset({"my_app", "frappe"})


def _app_names(board):
	return [r["function"].split("::")[1] for r in board if not r["is_framework"]]


def _fw_names(board):
	return [r["function"].split("::")[1] for r in board if r["is_framework"]]


class TestSelfTimeReliability:
	def test_no_self_time_is_not_measured(self):
		tree = _root([_kid("f", APP, 0)], cumulative_ms=500)
		assert call_tree._self_time_measured([tree]) is False

	def test_consistent_self_time_is_measured(self):
		tree = _root([_kid("f", APP, 300, [_kid("g", "my_app/g.py", 200)])])
		assert tree["cumulative_ms"] == 500
		assert call_tree._self_time_measured([tree]) is True

	def test_sql_is_not_subtracted_from_the_target(self):
		# The SQL wait sits inside python self time, so self still adds up to the root's time.
		tree = _root([_kid("f", APP, 500)])
		tree["children"].append(call_tree._make_sql_leaf(_sql(400), partial_match=False))
		assert call_tree._self_time_measured([tree]) is True

	def test_self_far_below_the_sampled_time_is_not_measured(self):
		tree = _root([_kid("f", APP, 10)], cumulative_ms=5000)
		assert call_tree._self_time_measured([tree]) is False

	def test_self_above_the_sampled_time_is_not_measured(self):
		tree = _root([_kid("f", APP, 3000)], cumulative_ms=1000)
		assert call_tree._self_time_measured([tree]) is False

	def test_rounding_and_clamping_noise_is_tolerated(self):
		assert call_tree._self_time_measured([_root([_kid("f", APP, 1020)], cumulative_ms=1000)])
		assert call_tree._self_time_measured([_root([_kid("f", APP, 700)], cumulative_ms=1000)])

	def test_trees_without_samples_do_not_make_it_unreliable(self):
		real = _root([_kid("f", APP, 500)])
		empty = _root([], cumulative_ms=0)
		assert call_tree._self_time_measured([real, empty]) is True

	def test_flag_on_the_rows_follows_the_check(self):
		bad = _root([_kid("f", APP, 10)], cumulative_ms=5000)
		_, board = call_tree._aggregate_hot_frames([bad], None, INSTALLED)
		assert board and not any(r["self_time_measured"] for r in board)
		good = _root([_kid("f", APP, 500)])
		_, board = call_tree._aggregate_hot_frames([good], None, INSTALLED)
		assert board and all(r["self_time_measured"] for r in board)

	def test_inconsistent_self_time_warns_and_degrades(self):
		fixture = {"root": _root([_kid("f", APP, 10)], cumulative_ms=5000)}
		result, ctx = _run([fixture], wall_ms={0: 5000})
		assert not any(r["self_time_measured"] for r in result.aggregate["hot_frames"])
		assert any("self time" in w.lower() for w in ctx.warnings)


class TestRankingBySelfWhenMeasured:
	@staticmethod
	def _tree(root_cumulative_ms=None):
		"""25 app functions: Self rises with i, Total falls with i (callee time in frappe code)."""
		kids = [
			_kid(
				f"fn{i:02d}", f"my_app/m{i:02d}.py", 10 + i,
				[_kid(f"callee{i:02d}", f"frappe/c{i:02d}.py", 1000 - 20 * i)],
			)
			for i in range(25)
		]
		return _root(kids, cumulative_ms=root_cumulative_ms)

	def test_measured_ranks_the_app_group_by_self(self):
		tree = self._tree()
		assert call_tree._self_time_measured([tree])
		_, board = call_tree._aggregate_hot_frames([tree], None, INSTALLED)
		assert _app_names(board) == [f"fn{i:02d}" for i in range(24, 4, -1)]

	def test_not_measured_ranks_the_same_tree_by_total(self):
		tree = self._tree(root_cumulative_ms=10 * 30000)
		assert not call_tree._self_time_measured([tree])
		_, board = call_tree._aggregate_hot_frames([tree], None, INSTALLED)
		assert _app_names(board) == [f"fn{i:02d}" for i in range(20)]

	def test_final_merge_orders_each_row_by_its_group_key(self):
		_, board = call_tree._aggregate_hot_frames([self._tree()], None, INSTALLED)
		app = [r["total_ms"] for r in board if not r["is_framework"]]
		fw = [r["total_cumulative_ms"] for r in board if r["is_framework"]]
		assert app == sorted(app, reverse=True)
		assert fw == sorted(fw, reverse=True)

	def test_framework_group_still_ranks_by_total(self):
		# Wrappers: Self rises with i, Total falls with i. Their callee time is in app code.
		kids = [
			_kid(
				f"w{i:02d}", f"frappe/w{i:02d}.py", 1 + i,
				[_kid(f"leaf{i:02d}", f"my_app/l{i:02d}.py", 1000 - 40 * i)],
			)
			for i in range(25)
		]
		tree = _root(kids)
		assert call_tree._self_time_measured([tree])
		_, board = call_tree._aggregate_hot_frames([tree], None, INSTALLED)
		assert _fw_names(board) == [f"w{i:02d}" for i in range(20)]

	def test_intermediate_trim_keeps_the_top_self_rows_when_measured(self):
		cap = call_tree.HOT_FRAMES_INTERMEDIATE_CAP
		trees = []
		for i in range(cap * 2 + 5):
			# Self falls with i, Total rises with i (callee time in frappe code).
			trees.append(
				_root([
					_kid(
						f"f{i}", f"my_app/m{i}.py", 1000 - i % 900,
						[_kid(f"c{i}", f"frappe/c{i}.py", i * 10)],
					)
				])
			)
		assert call_tree._self_time_measured(trees)
		_, board = call_tree._aggregate_hot_frames(trees, None, INSTALLED)
		kept = set(_app_names(board))
		assert "f0" in kept


class TestStoredMeaningOfTotalMs:
	def test_row_total_ms_is_the_self_sum_and_the_finding_agrees(self):
		trees = [
			_root([_kid("heavy", APP, 400, [_kid("inner", "my_app/i.py", 100)])]) for _ in range(3)
		]
		findings, board = call_tree._aggregate_hot_frames(trees, None, INSTALLED)
		row = next(r for r in board if r["function"].endswith("::heavy"))
		assert row["total_ms"] == 1200
		assert row["total_cumulative_ms"] == 1500
		finding = next(f for f in findings if f["title"].startswith(row["function"]))
		assert json.loads(finding["technical_detail_json"])["total_ms"] == 1200
		assert finding["estimated_impact_ms"] == 1200


class TestSelfBasedFindingsFollowTheReliabilityFlag:
	"""Repeated Hot Frame and the own-time hot path run on self time, so they need it trusted.

	Before self time was recorded both read ``self_ms`` too (always 0 in practice), so
	suppressing them while it is untrusted restores that behaviour and drops nothing new.
	"""

	@staticmethod
	def _repeated(root_cumulative_ms):
		trees = [
			_root([_kid("calc", "my_app/d.py", 200)], cumulative_ms=root_cumulative_ms)
			for _ in range(4)
		]
		findings, board = call_tree._aggregate_hot_frames(trees, None, INSTALLED)
		return [f for f in findings if f["finding_type"] == "Repeated Hot Frame"], board

	def test_repeated_hot_frame_fires_when_self_time_is_consistent(self):
		repeated, board = self._repeated(root_cumulative_ms=200)
		assert len(repeated) == 1 and repeated[0]["estimated_impact_ms"] == 800
		assert all(r["self_time_measured"] for r in board)

	def test_repeated_hot_frame_is_skipped_when_self_time_is_implausible(self):
		repeated, board = self._repeated(root_cumulative_ms=5000)
		assert repeated == []
		# The table still lists the frame, on Total only.
		assert board and not any(r["self_time_measured"] for r in board)

	@staticmethod
	def _endpoint():
		return _root([
			_kid("get_report", "my_app/api.py", 500, [_kid("slow_child", "my_app/calc.py", 500)])
		])

	def _titles(self, trusted):
		findings = call_tree._emit_per_action_findings(
			self._endpoint(), action_idx=0, action_label="my_app.api.get_report",
			action_wall_time_ms=1000, self_time_trusted=trusted,
		)
		return [f["title"] for f in findings if f["finding_type"] == "Slow Hot Path"]

	def test_own_time_hot_path_fires_when_trusted(self):
		titles = self._titles(True)
		assert any("self-time hot path" in t for t in titles)
		assert any("slow_child" in t for t in titles)

	def test_own_time_hot_path_is_skipped_when_untrusted_and_the_callee_finding_stays(self):
		titles = self._titles(False)
		assert not any("self-time hot path" in t for t in titles)
		assert any("slow_child" in t for t in titles)

	def test_analyze_gates_the_own_time_hot_path_on_the_session_check(self):
		def run(root_cumulative_ms):
			tree = _root(
				[_kid("get_report", "my_app/api.py", 500, [_kid("slow_child", "my_app/calc.py", 500)])],
				cumulative_ms=root_cumulative_ms,
			)
			recs = [{"pyi_session": {"root": tree}, "calls": []}]
			ctx = AnalyzeContext(
				session_uuid="u", docname="d",
				actions=[{"action_label": "my_app.api.get_report", "duration_ms": 1000}],
			)
			return [f["title"] for f in call_tree.analyze(recs, ctx).findings]

		assert any("self-time hot path" in t for t in run(1000))
		assert not any("self-time hot path" in t for t in run(5000))
