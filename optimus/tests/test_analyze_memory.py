# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Memory-bounding behavior of the analyze pipeline.

Long recorded flows OOM-killed the box because every recording's raw
pyinstrument tree stayed in RAM through persist and render. These tests pin
the fixes: call_tree frees each ``pyi_session`` once consumed (no later
analyzer depends on it) and run() drops references and GCs afterward.
"""

import inspect

from optimus import analyze
from optimus.analyzers import call_tree
from optimus.analyzers.base import AnalyzeContext


def _ctx(actions):
	c = AnalyzeContext(session_uuid="t", docname="PS-1")
	c.actions = actions
	return c


def _tree_recording(uuid="r1"):
	return {
		"uuid": uuid,
		"calls": [],
		"sidecar": [],
		"pyi_session": {
			"function": "<root>", "filename": "", "lineno": 0,
			"self_ms": 0, "cumulative_ms": 1000,
			"children": [{
				"function": "app.heavy", "filename": "apps/app/h.py", "lineno": 1,
				"self_ms": 600, "cumulative_ms": 600, "children": [],
			}],
		},
	}


class TestPyiSessionFreed:
	"""The raw pyinstrument Session must not survive call_tree."""

	def test_freed_for_recording_with_tree(self):
		rec = _tree_recording()
		call_tree.analyze([rec], _ctx([
			{"action_label": "a", "duration_ms": 1000, "queries_count": 0},
		]))
		assert "pyi_session" not in rec

	def test_freed_for_null_pyi_branch(self):
		"""The early-continue (pyi is None) path must free it too."""
		rec = {"uuid": "r1", "calls": [{"query": "SELECT 1", "duration": 5}],
			"sidecar": [], "pyi_session": None}
		call_tree.analyze([rec], _ctx([
			{"action_label": "x", "duration_ms": 100, "queries_count": 1},
		]))
		assert "pyi_session" not in rec

	def test_no_post_calltree_analyzer_reads_pyi_session(self):
		"""Guard: freeing pyi_session in call_tree is only safe while no
		analyzer ordered AFTER it reads pyi_session. Lock that in."""
		builtins = analyze._BUILTIN_ANALYZERS
		idx = builtins.index(call_tree.analyze)
		for fn in builtins[idx + 1:]:
			src = inspect.getsource(fn)
			assert "pyi_session" not in src, (
				f"{fn.__module__}.{fn.__name__} reads pyi_session after "
				"call_tree frees it"
			)


class TestRunGcCollect:
	"""run() drops the recordings reference and GCs after the analyzer loop so
	freed Session objects return to the allocator promptly."""

	def test_run_gc_collect_after_analyzers(self):
		src = inspect.getsource(analyze.run)
		assert "gc.collect()" in src
		assert "optimus_analyze_gc_collect" in src


class TestNoRecordingDroppedForPerformance:
	"""HARD INVARIANT: analyze must NEVER drop a captured recording (and so
	never a captured background job) for performance reasons. If a flow
	captured 10 RQ jobs, all 10 must reach the report.

	A session-wide recording cap that dropped the lightest recordings could
	silently remove jobs (jobs ARE recordings), so it was removed. These guards
	stop it coming back.
	"""

	def test_analyze_has_no_recording_cap(self):
		# The capping helper must not exist...
		assert not hasattr(analyze, "_apply_session_recording_cap")
		# ...nor any recording-count cap knob / call anywhere in the module.
		src = inspect.getsource(analyze)
		assert "optimus_max_recordings_analyzed" not in src
		assert "_apply_session_recording_cap" not in src

	def test_run_does_not_cap_recordings(self):
		src = inspect.getsource(analyze.run)
		assert "optimus_max_recordings_analyzed" not in src
		assert "_apply_session_recording_cap" not in src

	def test_all_captured_jobs_surface_in_report(self):
		"""End of the pipeline: build_background_jobs must list every RQ Job
		action there is no cap that can drop one."""
		from optimus import renderer

		n_jobs = 10
		actions = [
			{
				"event_type": "RQ Job",
				"recording_uuid": f"job-{i}",
				"idx": i,
				"action_label": f"RQ Job: task_{i}",
				"path": f"app.tasks.task_{i}",
				"duration_ms": 5 + i,  # deliberately varied (incl. tiny ones)
				"queries_count": 1,
			}
			for i in range(n_jobs)
		]
		# Mix in a couple of HTTP requests to prove only event_type filters.
		actions += [
			{"event_type": "HTTP Request", "recording_uuid": "req-1", "idx": 100},
			{"event_type": "HTTP Request", "recording_uuid": "req-2", "idx": 101},
		]
		out = renderer.build_background_jobs(actions, recordings_by_uuid={}, findings=[])
		assert out["count"] == n_jobs
		assert {j["recording_uuid"] for j in out["jobs"]} == {f"job-{i}" for i in range(n_jobs)}
