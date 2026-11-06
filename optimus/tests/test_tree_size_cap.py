# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The stored Python call tree is compacted without loss and bounded in size."""

import pickle
import time
from types import SimpleNamespace

import pytest

from optimus import capture


def frame(name, depth, path="/apps/x"):
	return f"{name}\x00{path}/{name}.py\x00{depth}"


def fake_session(records):
	return SimpleNamespace(frame_records=records, sample_count=len(records))


def leaf_times(records):
	times = {}
	for stack, seconds in records:
		leaf = stack[-1].split("\x00")[0]
		times[leaf] = times.get(leaf, 0.0) + seconds
	return times


def total_time(records):
	return sum(seconds for _, seconds in records)


def unique_records(count, depth=40, seconds=0.001):
	return [([frame(f"f{i}_{d}", d) for d in range(depth)], seconds) for i in range(count)]


def slow_sql_records():
	base = [frame(f"f{i}", i) for i in range(90)]
	records = []
	for i in range(800):
		slow = i == 403
		leaf = frame("slow_sql_execute", 2) if slow else frame("busy_loop", 1)
		records.append((base + [leaf], 2.0 if slow else 0.001))
	return records


def real_session():
	pyinstrument = pytest.importorskip("pyinstrument")
	profiler = pyinstrument.Profiler(interval=0.001)
	profiler.start()
	end = time.perf_counter() + 0.3
	while time.perf_counter() < end:
		sorted(range(200), key=lambda value: -value)
	profiler.stop()
	return profiler.last_session


def tree_signature(session):
	def walk(node):
		return (node.function, node.file_path_short, round(node.time, 9), [walk(c) for c in node.children])

	return walk(session.root_frame())


class TestCompaction:
	def test_real_profile_keeps_an_identical_tree_and_shrinks(self):
		session = real_session()
		before_signature = tree_signature(session)
		before_size = len(pickle.dumps(session))
		info = capture.compact_pyi_session(session)
		assert info is not None and info["every"] == 1
		assert tree_signature(session) == before_signature
		assert not hasattr(session, "optimus_thinned")
		assert len(pickle.dumps(session)) * 20 <= before_size

	def test_estimate_is_at_least_the_real_pickle(self):
		session = fake_session(unique_records(500))
		info = capture.compact_pyi_session(session)
		assert info["estimate"] >= len(pickle.dumps(session))

	def test_estimate_counts_utf8_bytes(self):
		name = "é" * 3000
		session = fake_session([([frame(name, 1)], 0.1)])
		info = capture.compact_pyi_session(session)
		assert info["estimate"] >= len(pickle.dumps(session))

	def test_unknown_shape_is_left_alone(self):
		triples = [(["a"], 0.1, "extra")]
		session = fake_session(triples)
		assert capture.compact_pyi_session(session) is None
		assert session.frame_records is triples
		assert capture.compact_pyi_session(SimpleNamespace(frame_records="not a list")) is None
		assert capture.compact_pyi_session(SimpleNamespace()) is None

	def test_equal_stacks_share_one_list(self):
		stack = ["a\x00f\x001", "b\x00f\x002"]
		session = fake_session([(list(stack), 0.1), (list(stack), 0.2)])
		capture.compact_pyi_session(session)
		assert session.frame_records[0][0] is session.frame_records[1][0]


class TestThinning:
	def test_samples_above_the_ceiling_are_thinned_before_interning(self, monkeypatch):
		monkeypatch.setattr(capture, "TREE_MAX_SAMPLES", 1000)
		session = fake_session([([frame("a", 1)], 0.001) for _ in range(5000)])
		info = capture.compact_pyi_session(session)
		assert len(session.frame_records) <= 1000
		assert info["every"] >= 5
		assert session.optimus_thinned == {"every": info["every"], "samples": 5000}
		assert session.sample_count == len(session.frame_records)

	def test_total_time_is_exact_after_thinning(self, monkeypatch):
		monkeypatch.setattr(capture, "TREE_MAX_SAMPLES", 100)
		records = unique_records(1234, depth=3, seconds=0.00123)
		total = total_time(records)
		session = fake_session(records)
		capture.compact_pyi_session(session)
		assert abs(total_time(session.frame_records) - total) < 1e-9

	def test_a_long_blocking_sample_keeps_its_own_stack(self, monkeypatch):
		monkeypatch.setattr(capture, "TREE_MAX_SAMPLES", 100)
		records = slow_sql_records()
		interval = total_time(records) / 100
		session = fake_session(records)
		capture.compact_pyi_session(session)
		times = leaf_times(session.frame_records)
		assert abs(times["slow_sql_execute"] - 2.0) <= interval
		assert abs(sum(times.values()) - total_time(records)) < 1e-9

	def test_thinning_is_deterministic(self, monkeypatch):
		monkeypatch.setattr(capture, "TREE_MAX_SAMPLES", 100)
		first, second = fake_session(slow_sql_records()), fake_session(slow_sql_records())
		capture.compact_pyi_session(first)
		capture.compact_pyi_session(second)
		assert first.frame_records == second.frame_records

	def test_over_the_byte_budget_thins_until_it_fits(self):
		session = fake_session(unique_records(30000, depth=20))
		total = total_time(session.frame_records)
		blob, info = capture.pickle_pyi_session(session)
		assert blob is not None and len(blob) <= capture.TREE_MAX_BYTES
		assert info["every"] > 1
		assert abs(total_time(pickle.loads(blob).frame_records) - total) < 1e-9

	def test_a_long_blocking_sample_survives_the_byte_budget(self, monkeypatch):
		monkeypatch.setattr(capture, "TREE_MAX_BYTES", 200_000)
		records = unique_records(3000, depth=20)
		records[1500] = (records[1500][0], 2.0)
		slow_leaf = records[1500][0][-1]
		session = fake_session(records)
		blob, _ = capture.pickle_pyi_session(session)
		kept = pickle.loads(blob).frame_records
		assert any(stack[-1] == slow_leaf and seconds >= 2.0 for stack, seconds in kept)

	def test_a_tree_that_cannot_fit_returns_no_blob(self, monkeypatch):
		monkeypatch.setattr(capture, "TREE_MAX_BYTES", 2000)
		session = fake_session([([frame("x" * 5000, 1)], 1.0)])
		blob, _ = capture.pickle_pyi_session(session)
		assert blob is None

	def test_real_pickle_over_the_cap_thins_further(self, monkeypatch):
		monkeypatch.setattr(capture, "_estimate_bytes", lambda *a, **k: 1)
		monkeypatch.setattr(capture, "TREE_MAX_BYTES", 100_000)
		session = fake_session(unique_records(3000, depth=20))
		blob, _ = capture.pickle_pyi_session(session)
		assert blob is not None and len(blob) <= 100_000

	def test_failure_halfway_leaves_the_session_untouched(self, monkeypatch):
		records = unique_records(50)
		session = fake_session(records)
		monkeypatch.setattr(capture, "_intern_records", lambda *a, **k: 1 / 0)
		with pytest.raises(ZeroDivisionError):
			capture.compact_pyi_session(session)
		assert session.frame_records is records
		assert not hasattr(session, "optimus_thinned")


def test_thinned_real_session_still_builds_a_tree(monkeypatch):
	session = real_session()
	monkeypatch.setattr(capture, "TREE_MAX_SAMPLES", 20)
	capture.compact_pyi_session(session)
	assert session.optimus_thinned["every"] > 1
	assert session.root_frame() is not None
