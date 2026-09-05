# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""BG-job profiling regression: findings surface the real hotspot, not the RQ
wrapper.

The Slow Hot Path walker used to blame ``rq.worker.execute_job`` /
``worker_main`` / ``run_job`` for every slow job, because those wrapper frames
carry 100% of the wall time. These tests guard the fix: the walker consults
``_is_pure_helper_frame``, which covers ``/rq/`` paths and wrapper function
names like ``execute_job`` / ``worker_main`` regardless of file.
"""

import json

from optimus.analyzers import call_tree


def _node(function, filename, cumulative, self_ms, children=None, kind="python", lineno=10):
	return {
		"function": function,
		"filename": filename,
		"lineno": lineno,
		"kind": kind,
		"cumulative_ms": cumulative,
		"self_ms": self_ms,
		"children": children or [],
	}


def test_walker_descends_past_rq_wrappers():
	"""Synthetic tree mirroring a real RQ-captured stack:

	    <root>
	    └── worker_main           (rq/worker.py)
	        └── execute_job       (rq/worker.py)
	            └── perform_job   (frappe/utils/background_jobs.py)
	                └── sync_customer_data  (apps/my_app/jobs.py)
	                    └── process_invoice_line  (apps/my_app/invoice.py)

	The walker must descend through every wrapper layer and emit on the
	user-code hot frame (process_invoice_line), not the wrappers."""
	user_hot = _node(
		"process_invoice_line",
		"apps/my_app/invoice.py",
		cumulative=4500,
		self_ms=4500,
		lineno=142,
	)
	job_body = _node(
		"sync_customer_data",
		"apps/my_app/jobs.py",
		cumulative=4800,
		self_ms=300,
		children=[user_hot],
	)
	bg_dispatch = _node(
		"perform_job",
		"frappe/utils/background_jobs.py",
		cumulative=4900,
		self_ms=100,
		children=[job_body],
	)
	rq_inner = _node(
		"execute_job",
		"site-packages/rq/worker.py",
		cumulative=4950,
		self_ms=50,
		children=[bg_dispatch],
	)
	rq_outer = _node(
		"worker_main",
		"site-packages/rq/worker.py",
		cumulative=5000,
		self_ms=50,
		children=[rq_inner],
	)
	tree = _node(
		"<root>", "", cumulative=5000, self_ms=0, children=[rq_outer],
	)

	findings = call_tree._emit_per_action_findings(
		tree,
		action_idx=0,
		action_label="RQ Job: sync_customer_data",
		action_wall_time_ms=5000,
	)
	assert findings, "expected a finding on the user-code hot frame"
	# Exactly one finding (the deepest qualifying frame).
	hot = findings[0]
	td = json.loads(hot["technical_detail_json"])
	# CRITICAL ASSERTION: the wrapper frames must not be the
	# blame surface.
	assert td["function"] not in (
		"worker_main", "execute_job", "perform_job", "run_job",
	), f"finding still blames a wrapper frame: {td['function']!r}"
	# The user-code frame OR its direct user-code ancestor wins.
	assert td["function"] in (
		"process_invoice_line", "sync_customer_data",
	), f"unexpected hot frame in finding: {td['function']!r}"
	# Callsite carries a real source location, not a /rq/ wrapper.
	assert "/rq/" not in (td.get("filename") or "")
	assert "background_jobs" not in (td.get("filename") or "")


def test_walker_skips_wrapper_function_names_regardless_of_file():
	"""``execute_job`` in a user-app file is still skipped: the bare function
	name marks plumbing per ``_PURE_HELPER_FUNCTION_NAMES``. This risks hiding a
	rare user method named ``execute_job``, preferable to re-blaming the wrapper
	on every BG job."""
	# A user-app file that defines a function happening to be named
	# `execute_job`. Even though the filename is in an app path, the
	# bare name match takes precedence.
	wrapper_in_user_file = _node(
		"execute_job",
		"apps/my_app/scheduler.py",
		cumulative=3000,
		self_ms=200,
		children=[
			_node(
				"do_real_work",
				"apps/my_app/work.py",
				cumulative=2700,
				self_ms=2700,
			),
		],
	)
	tree = _node(
		"<root>", "", cumulative=3000, self_ms=0,
		children=[wrapper_in_user_file],
	)
	findings = call_tree._emit_per_action_findings(
		tree,
		action_idx=0,
		action_label="RQ Job: nightly_cleanup",
		action_wall_time_ms=3000,
	)
	assert findings
	td = json.loads(findings[0]["technical_detail_json"])
	assert td["function"] == "do_real_work", (
		f"walker should skip the bare 'execute_job' frame, got "
		f"{td['function']!r}"
	)


def test_bg_job_finding_title_uses_short_job_name():
	"""Finding title reads 'In job <short>, …' rather than carrying
	the verbose 'RQ Job: <short>' action-label prefix."""
	user_hot = _node(
		"process_invoice_line",
		"apps/my_app/invoice.py",
		cumulative=4500,
		self_ms=4500,
	)
	job_body = _node(
		"sync_customer_data",
		"apps/my_app/jobs.py",
		cumulative=4800,
		self_ms=300,
		children=[user_hot],
	)
	rq_outer = _node(
		"execute_job",
		"site-packages/rq/worker.py",
		cumulative=5000,
		self_ms=200,
		children=[job_body],
	)
	tree = _node(
		"<root>", "", cumulative=5000, self_ms=0, children=[rq_outer],
	)
	findings = call_tree._emit_per_action_findings(
		tree,
		action_idx=0,
		action_label="RQ Job: sync_customer_data",
		action_wall_time_ms=5000,
	)
	assert findings
	title = findings[0]["title"]
	assert title.startswith("In job sync_customer_data"), (
		f"title didn't trim the 'RQ Job: ' prefix: {title!r}"
	)
	assert "RQ Job:" not in title


def test_slow_background_job_fallback_emits_on_deepest_user_frame():
	"""A 12s job whose user-code frame is only 20% (below the 25% Slow-Hot-Path
	threshold) but is the deepest non-plumbing frame. The walker emits nothing,
	so the BG-job fallback fires and still surfaces an actionable callsite."""
	# 20% of 12s = 2400ms below default 25% threshold but well
	# above the absolute 200ms floor. Walker won't emit; fallback
	# should.
	user_frame = _node(
		"trickle_through_records",
		"apps/my_app/work.py",
		cumulative=2400,
		self_ms=2400,
		lineno=88,
	)
	job_body = _node(
		"long_running_job",
		"apps/my_app/jobs.py",
		cumulative=2600,
		self_ms=200,
		children=[user_frame],
	)
	# Most of the wall time is a sleep / network wait in framework
	# plumbing not user-actionable individually but the user-code
	# frame is still the right callsite to surface.
	rq_outer = _node(
		"execute_job",
		"site-packages/rq/worker.py",
		cumulative=12000,
		self_ms=9400,
		children=[job_body],
	)
	tree = _node(
		"<root>", "", cumulative=12000, self_ms=0,
		children=[rq_outer],
	)
	findings = call_tree._emit_per_action_findings(
		tree,
		action_idx=3,
		action_label="RQ Job: long_running_job",
		action_wall_time_ms=12000,
	)
	assert findings, "expected fallback finding for slow BG job"
	hot = findings[0]
	assert hot["finding_type"] == "Slow Background Job"
	td = json.loads(hot["technical_detail_json"])
	# Deepest user-code frame wins.
	assert td["function"] == "trickle_through_records"
	assert td["is_bg_job_fallback"] is True
	# Title reads as "In job <short>, ..." per Delta 3.
	assert "In job long_running_job" in hot["title"]
	# Action linkage preserved for the per-action table cross-link.
	assert hot["action_ref"] == "3"
	# Impact clamped to action wall never claims more than the job
	# actually took.
	assert hot["estimated_impact_ms"] <= 12000


def test_fallback_does_not_fire_for_fast_bg_jobs():
	"""The fallback is gated on action_wall_time_ms ≥ med_ms (200ms
	default). A 50ms job shouldn't trigger it even if the walker
	emitted nothing."""
	tree = _node(
		"<root>", "", cumulative=50, self_ms=0,
		children=[
			_node(
				"execute_job",
				"site-packages/rq/worker.py",
				cumulative=50,
				self_ms=10,
				children=[
					_node(
						"quick_check",
						"apps/my_app/jobs.py",
						cumulative=40,
						self_ms=40,
					),
				],
			),
		],
	)
	findings = call_tree._emit_per_action_findings(
		tree,
		action_idx=0,
		action_label="RQ Job: quick_check",
		action_wall_time_ms=50,
	)
	assert findings == [], "fallback fired for a fast job"


def test_fallback_does_not_fire_for_request_actions():
	"""The fallback is gated on the 'RQ Job: ' label prefix so it
	can't pollute request-action findings (which are intentionally
	allowed to be silent when nothing crosses thresholds)."""
	# A slow request action that produces no Slow Hot Path finding
	# because every frame is below 25%. Fallback should NOT fire.
	user_frame = _node(
		"trickle_through_records",
		"apps/my_app/work.py",
		cumulative=2400,
		self_ms=2400,
	)
	job_body = _node(
		"slow_view",
		"apps/my_app/views.py",
		cumulative=2600,
		self_ms=200,
		children=[user_frame],
	)
	wrapper = _node(
		"application",
		"frappe/app.py",
		cumulative=12000,
		self_ms=9400,
		children=[job_body],
	)
	tree = _node(
		"<root>", "", cumulative=12000, self_ms=0,
		children=[wrapper],
	)
	findings = call_tree._emit_per_action_findings(
		tree,
		action_idx=0,
		action_label="POST /api/method/slow_view",
		action_wall_time_ms=12000,
	)
	# No "Slow Background Job" finding for non-BG actions.
	bg_findings = [
		f for f in findings if f["finding_type"] == "Slow Background Job"
	]
	assert bg_findings == [], (
		"Slow Background Job fallback leaked into a request action"
	)
