# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The Redis lifetimes and stale-session limits only work together: pin how they relate."""

import os
import re

import pytest

from optimus import analyze, hooks, janitor, session, timing


def test_recording_data_outlives_a_janitor_stopped_session_until_analyze_reads_it():
	idle_stop = (
		timing.SESSION_TTL_SECONDS + (timing.STALE_RECORDING_MINUTES + timing.JANITOR_INTERVAL_MINUTES) * 60
	)
	analyze_wait = analyze._MAX_BG_JOB_WAIT_SECONDS + analyze._SINGLEFLIGHT_MAX_WAIT_SECONDS
	assert timing.RECORDING_DATA_TTL_SECONDS >= idle_stop + analyze_wait


def test_recording_data_is_kept_for_six_hours():
	assert timing.RECORDING_DATA_TTL_SECONDS == 6 * 60 * 60


def test_the_janitor_waits_for_the_pointer_to_expire_before_stopping_a_row():
	assert timing.STALE_RECORDING_MINUTES * 60 > timing.SESSION_TTL_SECONDS


def test_the_analyze_budget_ends_before_the_janitor_calls_a_run_dead():
	assert timing.ANALYZE_TOTAL_BUDGET_SECONDS < timing.STALE_ANALYZING_MINUTES * 60


def test_the_old_names_are_the_timing_values():
	assert session.SESSION_TTL_SECONDS == timing.SESSION_TTL_SECONDS
	assert session.JOB_KEYS_TTL_SECONDS == timing.JOB_KEYS_TTL_SECONDS
	assert janitor.STALE_RECORDING_MINUTES == timing.STALE_RECORDING_MINUTES
	assert janitor.STALE_ANALYZING_MINUTES == timing.STALE_ANALYZING_MINUTES
	assert analyze._MAX_BG_JOB_WAIT_SECONDS == timing.MAX_BG_JOB_WAIT_SECONDS
	assert analyze._SINGLEFLIGHT_MAX_WAIT_SECONDS == timing.SINGLEFLIGHT_MAX_WAIT_SECONDS
	assert analyze.ANALYZE_TOTAL_BUDGET_SECONDS == timing.ANALYZE_TOTAL_BUDGET_SECONDS


def test_the_janitor_cron_runs_at_the_timing_interval():
	cron = hooks.scheduler_events["cron"]
	entry = next(spec for spec, jobs in cron.items() if "optimus.janitor.sweep_stale_sessions" in jobs)
	assert re.fullmatch(rf"\*/{timing.JANITOR_INTERVAL_MINUTES} \* \* \* \*", entry)


def test_job_keys_outlive_recording_data():
	assert timing.JOB_KEYS_TTL_SECONDS >= timing.RECORDING_DATA_TTL_SECONDS


def test_the_janitor_stops_a_recording_row_after_11_minutes():
	assert timing.STALE_RECORDING_MINUTES == 11


def test_the_janitor_fails_a_stuck_analyze_only_after_the_rq_long_queue_timeout():
	# Frappe's long queue kills a job at 1500 s (25 min); optimus has no constant for it.
	assert timing.STALE_ANALYZING_MINUTES == 30
	assert timing.STALE_ANALYZING_MINUTES * 60 > 1500


def test_the_session_pointer_lives_as_long_as_frappes_recorder_auto_disable():
	recorder = pytest.importorskip("frappe.recorder")
	if not hasattr(recorder, "RECORDER_AUTO_DISABLE"):
		pytest.skip("frappe is stubbed")
	assert timing.SESSION_TTL_SECONDS == recorder.RECORDER_AUTO_DISABLE


def test_line_profiling_shares_the_session_ttl():
	from optimus.line_profile import capture

	assert capture.SESSION_TTL_SECONDS == timing.SESSION_TTL_SECONDS


def test_the_pill_checks_for_an_ended_recording_more_often_than_the_pointer_lives():
	assert timing.PILL_RECORDING_CHECK_SECONDS < timing.SESSION_TTL_SECONDS


def test_the_pill_script_uses_the_same_recording_check_interval():
	path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "public", "js", "optimus_pill_state.js")
	with open(path) as f:
		match = re.search(r"RECORDING_CHECK_MS = (\d+)", f.read())
	assert int(match.group(1)) == timing.PILL_RECORDING_CHECK_SECONDS * 1000
