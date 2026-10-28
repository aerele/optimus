# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the pill state module (run in node) and its widget wiring."""

import os
import shutil
import subprocess

import pytest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(APP_DIR, "public", "js")
PILL_JS = os.path.join(JS_DIR, "optimus_pill_state.js")
WIDGET_JS = os.path.join(JS_DIR, "floating_widget.js")
HARNESS = os.path.join(APP_DIR, "tests", "js", "pill_state_harness.js")
WIDGET_HARNESS = os.path.join(APP_DIR, "tests", "js", "widget_poll_harness.js")


def _read(path):
	with open(path) as f:
		return f.read()


def _need_node():
	if not shutil.which("node"):
		pytest.skip("node not installed")


def test_pill_state_js_syntax():
	_need_node()
	for path in (PILL_JS, WIDGET_JS):
		result = subprocess.run(["node", "--check", path], capture_output=True, text=True)
		assert result.returncode == 0, result.stderr


def test_pill_state_logic_in_node():
	_need_node()
	result = subprocess.run(["node", HARNESS], capture_output=True, text=True, timeout=60)
	assert result.returncode == 0, result.stdout + result.stderr


def test_widget_polling_and_resync_in_node():
	_need_node()
	result = subprocess.run(["node", WIDGET_HARNESS], capture_output=True, text=True, timeout=60)
	assert result.returncode == 0, result.stdout + result.stderr


def test_pill_module_loads_before_widget():
	hooks = _read(os.path.join(APP_DIR, "hooks.py"))
	assert hooks.index('"/assets/optimus/js/optimus_pill_state.js') < hooks.index(
		'"/assets/optimus/js/floating_widget.js'
	)


def test_widget_resyncs_from_server_and_polls():
	src = _read(WIDGET_JS)
	assert "optimus.api.pill_state" in src
	# The old early return that let a reload never clear "analyzing" must stay gone.
	body = src[src.index("function refreshStatus"):src.index("function onClick")]
	assert 'display === "analyzing"' not in body
	for symbol in ("syncPoll", "pollTick", "reduceHardStop", "pollAction", "visibilitychange"):
		assert symbol in src, symbol


def test_widget_routes_phase_1_and_2_events_through_reducer():
	src = _read(WIDGET_JS)
	assert "Pill.EVENT_NAMES.forEach" in src
	pill = _read(PILL_JS)
	for name in (
		"optimus_session_analyzing", "optimus_session_ready", "optimus_session_failed",
		"phase_2_run_analyzing", "phase_2_run_ready", "phase_2_run_failed",
	):
		assert name in pill, name
