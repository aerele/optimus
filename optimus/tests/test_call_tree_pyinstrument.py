# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The walker turns a real pyinstrument Session into a tree with real self times.

pyinstrument has no ``self_time`` on a frame (it has ``total_self_time``) and adds a
synthetic ``[self]`` child per sample. The walker must drop those children and derive
each node's self time from its own time minus its real children's time.
"""

import time

import pytest

pytest.importorskip("pyinstrument")

from pyinstrument import Profiler  # noqa: E402

from optimus.analyzers import call_tree  # noqa: E402

# The floor-clock check runs every N iterations, so the loop frame owns nearly all of its
# time and does not hand it to perf_counter frames.
_CHECK_EVERY = 20000


def _busy_loop(min_ms=50.0):
	start = time.perf_counter()
	count = 0
	while True:
		for _ in range(_CHECK_EVERY):
			count += 1
		if (time.perf_counter() - start) * 1000 >= min_ms:
			return count


def _recurse(depth):
	if depth == 0:
		return sum(i * i for i in range(300000))
	return _recurse(depth - 1)


def _profile():
	profiler = Profiler(interval=0.001)
	profiler.start()
	_busy_loop()
	_recurse(4)
	profiler.stop()
	return profiler.last_session


def _all_nodes(node):
	yield node
	for child in node["children"]:
		yield from _all_nodes(child)


@pytest.fixture(scope="module")
def walked():
	session = _profile()
	return session, call_tree._pyi_to_dict_tree(session)


def test_some_node_has_self_time(walked):
	_, tree = walked
	assert max(n["self_ms"] for n in _all_nodes(tree)) > 0


def test_self_is_between_zero_and_cumulative(walked):
	_, tree = walked
	for node in _all_nodes(tree):
		assert 0 <= node["self_ms"] <= node["cumulative_ms"] + 1e-3, node["function"]


def test_no_synthetic_nodes(walked):
	_, tree = walked
	assert [n["function"] for n in _all_nodes(tree) if n["function"].startswith("[")] == []


def test_self_sums_to_root_cumulative(walked):
	_, tree = walked
	nodes = list(_all_nodes(tree))
	# Each value is rounded to 3 decimals, so allow half a microsecond per node.
	tolerance = 1e-3 + 5e-4 * len(nodes)
	assert sum(n["self_ms"] for n in nodes) == pytest.approx(tree["cumulative_ms"], abs=tolerance)


def test_loop_function_owns_most_of_its_time(walked):
	_, tree = walked
	loop = next(n for n in _all_nodes(tree) if n["function"] == "_busy_loop")
	assert loop["cumulative_ms"] >= 40
	assert loop["self_ms"] >= 0.5 * loop["cumulative_ms"]


def test_recursion_frames_keep_their_nesting(walked):
	_, tree = walked
	rec = [n for n in _all_nodes(tree) if n["function"] == "_recurse"]
	assert len(rec) >= 2
	assert all(n["self_ms"] <= n["cumulative_ms"] + 1e-3 for n in rec)


def test_matches_pyinstrument_total_self_time(walked):
	session, tree = walked

	def is_synthetic(frame):
		flag = getattr(frame, "is_synthetic", None)
		return bool(flag) if flag is not None else frame.function.startswith("[")

	def pairs(frame, node):
		yield frame, node
		real = [c for c in frame.children if not is_synthetic(c)]
		for f, n in zip(real, node["children"], strict=True):
			yield from pairs(f, n)

	root = session.root_frame()
	if not hasattr(root, "total_self_time"):
		pytest.skip("this pyinstrument has no total_self_time")
	checked = 0
	for frame, node in pairs(root, tree):
		assert node["self_ms"] == pytest.approx(frame.total_self_time * 1000, abs=1e-3)
		checked += 1
	assert checked > 1
