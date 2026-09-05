# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Phase-2 (line-profile) hook callbacks, registered alongside the phase-1
callbacks in ``hooks_callbacks.py``. Phase-1 and phase-2 are mutually
exclusive per user (separate Redis flags; the API rejects starting one
while the other is active).

Each request/job running while phase-2 is active for the user gets a fresh
``LineProfiler`` with the run's picked functions, ``enable_by_count()``
before the body and ``disable()`` + per-line stats RPUSH'd to Redis after.
No SQL recording, pyinstrument or sidecar wraps: only line-level timings.
"""

import frappe

from optimus import hooks_callbacks
from optimus.line_profile import capture


def _overhead_budget_seconds() -> float:
	"""Wall-clock seconds line tracing may run before the watchdog disengages
	it, so profiling can't freeze the user's flow (observe, don't spoil).
	``optimus_phase2_overhead_budget_seconds`` in site_config; default 10,
	``0`` disables the budget (unlimited profiling)."""
	try:
		return float(frappe.conf.get("optimus_phase2_overhead_budget_seconds", 10) or 0)
	except Exception:
		return 10.0


def _cancel_watchdog() -> None:
	"""Stop and clear this request/job's overhead watchdog. Called first in the
	after_* teardown so a request that finished within budget keeps full data
	and no stale timer disengages a later request."""
	watchdog = getattr(frappe.local, "_lp_watchdog", None)
	frappe.local._lp_watchdog = None
	if watchdog is not None:
		try:
			watchdog.cancel()
		except Exception:
			pass


# ---------------------------------------------------------------------------
# Request hooks
# ---------------------------------------------------------------------------


def before_request_line_profile(*args, **kwargs) -> None:
	"""If phase-2 is active for this user, build a per-request LineProfiler
	and enable it. Returns silently otherwise.

	Best-effort: any exception is swallowed and logged so the host request
	is never broken by profiler instrumentation.
	"""
	try:
		user = frappe.session.user
		run_uuid = capture.is_active(user)
		if not run_uuid:
			return

		# Skip the profiler's own endpoints same logic phase-1 uses to
		# avoid recording its own admin API calls.
		if hooks_callbacks._should_skip_request():
			return

		profiler = capture.make_profiler(run_uuid)
		if profiler is None:
			return

		# Self-heal: if a prior request was killed mid-flight (skipping its
		# after_request teardown) and left tool 2 registered, clear the orphan
		# before enabling so the worker recovers without a bench restart. Gated on
		# the PROCESS-WIDE active count, not this thread's local: under a gthread
		# worker a sibling thread mid-profile legitimately owns tool 2 and
		# reclaiming it would desync that thread (the tool-2 leak class). The
		# thread-local check stays as a cheap first gate. Log when the reclaim
		# actually fires silent reclaim masked the leak class in production.
		if (
			capture.active_profiler_count() == 0
			and getattr(frappe.local, "_lp_profiler", None) is None
		):
			import sys as _sys

			_mon = getattr(_sys, "monitoring", None)
			if _mon is not None and _mon.get_tool(_mon.PROFILER_ID) == "line_profiler":
				frappe.logger().warning(
					"optimus.line_profile.before_request: reclaiming orphan "
					"tool 2 from a prior request that skipped teardown."
				)
			capture.release_monitoring_tool()
		# Register process-wide BEFORE enabling so a concurrent sibling's teardown
		# can't observe a 0 count and free the tool we're about to enable.
		capture.incr_active_profilers()
		try:
			profiler.enable_by_count()
		except Exception:
			capture.decr_active_profilers()
			raise
		frappe.local._lp_profiler = profiler
		frappe.local._lp_run_uuid = run_uuid
		# Arm the overhead watchdog: if this request runs past the budget,
		# tracing is disengaged so the flow completes (observe, don't spoil).
		frappe.local._lp_watchdog = capture.start_overhead_watchdog(
			run_uuid, _overhead_budget_seconds()
		)
	except Exception as exc:
		frappe.log_error(
			title="phase 2 before_request failed",
			message=f"{type(exc).__name__}: {exc}",
		)


def after_request_line_profile(*args, **kwargs) -> None:
	"""Disable the per-request profiler, serialize per-line stats and push the
	batch to Redis. Locals are cleared even if the profiler was never enabled,
	to keep frappe.local clean."""
	profiler = getattr(frappe.local, "_lp_profiler", None)
	run_uuid = getattr(frappe.local, "_lp_run_uuid", None)
	# Always clear locals before doing I/O so a Redis hiccup doesn't leave
	# stale state on a recycled gunicorn worker.
	frappe.local._lp_profiler = None
	frappe.local._lp_run_uuid = None
	frappe.local._lp_active = None  # invalidate the per-request is_active cache
	_cancel_watchdog()

	if profiler is None or not run_uuid:
		return

	try:
		try:
			# Pair with before_request's enable_by_count(); count-guarded so it's
			# safe even if line_profiler already tore down (the "tool 2 is not in
			# use" path). Stats are still readable after, so don't skip serialize.
			profiler.disable_by_count()
		except Exception:
			pass
		samples = capture.serialize_stats(profiler)
		capture.flush_samples(run_uuid, samples)
	except Exception as exc:
		frappe.log_error(
			title="phase 2 after_request failed",
			message=f"{type(exc).__name__}: {exc}",
		)
	finally:
		# Unregister this profiler, then force-free tool 2 ONLY when no sibling
		# thread is still profiling. Freeing it while a concurrent gthread request
		# is enabled would desync line_profiler's shared manager (the tool-2 leak
		# class). When this is the last active profiler, the free guarantees no
		# line-trace hook survives to slow later requests.
		if capture.decr_active_profilers() == 0:
			capture.release_monitoring_tool()


# ---------------------------------------------------------------------------
# Background job hooks (mirror request hooks; gated by _lp_session_id kwarg)
# ---------------------------------------------------------------------------


def before_job_line_profile(method=None, kwargs=None, **rest) -> None:
	"""Phase-2 equivalent of ``hooks_callbacks.before_job``. Reads
	``_lp_session_id`` injected by the extended enqueue patch.

	WARNING: ``_lp_session_id`` is popped from the job's kwargs dict
	unconditionally, even when not instrumenting. That dict is the one
	``execute_job`` splats into the user's method, so leaving the marker
	crashes it with an unexpected-keyword-argument error.
	"""
	# Always pop our marker first before any control-flow that might
	# return early. The mutation propagates because ``kwargs`` is a
	# reference to the dict execute_job will use.
	if isinstance(kwargs, dict):
		run_uuid = kwargs.pop("_lp_session_id", None)
	else:
		run_uuid = None

	if not run_uuid:
		return

	try:
		user = getattr(frappe.session, "user", None)
		if not user or user == "Guest":
			return

		# Confirm the run is still active (user may have stopped it
		# between enqueue and the worker picking up the job).
		if capture.is_active(user) != run_uuid:
			return

		profiler = capture.make_profiler(run_uuid)
		if profiler is None:
			return

		# Self-heal a tool 2 orphaned by a previously-killed job (see
		# before_request_line_profile). RQ workers run one job at a time, so
		# the process-wide count is 0 between jobs and this is equivalent to the
		# old thread-local gate but we share the same count mechanism for
		# consistency and strict safety. Log when the reclaim actually fires.
		if (
			capture.active_profiler_count() == 0
			and getattr(frappe.local, "_lp_profiler", None) is None
		):
			import sys as _sys

			_mon = getattr(_sys, "monitoring", None)
			if _mon is not None and _mon.get_tool(_mon.PROFILER_ID) == "line_profiler":
				frappe.logger().warning(
					"optimus.line_profile.before_job: reclaiming orphan "
					"tool 2 from a prior job that skipped teardown."
				)
			capture.release_monitoring_tool()
		capture.incr_active_profilers()
		try:
			profiler.enable_by_count()
		except Exception:
			capture.decr_active_profilers()
			raise
		frappe.local._lp_profiler = profiler
		frappe.local._lp_run_uuid = run_uuid
		frappe.local._lp_watchdog = capture.start_overhead_watchdog(
			run_uuid, _overhead_budget_seconds()
		)
	except Exception as exc:
		frappe.log_error(
			title="phase 2 before_job failed",
			message=f"{type(exc).__name__}: {exc}",
		)


def after_job_line_profile(method=None, kwargs=None, result=None, **rest) -> None:
	"""Phase-2 equivalent of ``hooks_callbacks.after_job``. Same as
	``after_request_line_profile`` but called from the job lifecycle.
	Signature mirrors phase-1's ``hooks_callbacks.after_job``.
	"""
	profiler = getattr(frappe.local, "_lp_profiler", None)
	run_uuid = getattr(frappe.local, "_lp_run_uuid", None)
	frappe.local._lp_profiler = None
	frappe.local._lp_run_uuid = None
	frappe.local._lp_active = None
	_cancel_watchdog()

	if profiler is None or not run_uuid:
		return

	try:
		try:
			profiler.disable_by_count()
		except Exception:
			pass
		samples = capture.serialize_stats(profiler)
		capture.flush_samples(run_uuid, samples)
	except Exception as exc:
		frappe.log_error(
			title="phase 2 after_job failed",
			message=f"{type(exc).__name__}: {exc}",
		)
	finally:
		# Force-free tool 2 only when no sibling profiler is still active (see
		# after_request_line_profile).
		if capture.decr_active_profilers() == 0:
			capture.release_monitoring_tool()
