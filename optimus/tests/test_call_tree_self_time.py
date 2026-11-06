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


class TestRankingByTotal:
	@staticmethod
	def _tree():
		"""25 user functions whose Self order is the reverse of their Total order."""
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
