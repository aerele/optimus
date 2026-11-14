# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Session action buttons follow the server's can_act decision (form onload optimus_can_act)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[1] / "optimus/doctype/optimus_session/optimus_session.js"

SCRIPT = r"""
const vm = require("node:vm"), fs = require("node:fs");
const ctx = {__: (t, a = []) => t.replace(/\{(\d+)\}/g, (_, n) => a[n]),
  frappe: {ui: {form: {on: () => {}}}, realtime: {on: () => {}}, utils: {escape_html: (s) => s}}};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
let banner, hint;
ctx._phase2_armed_banner = (frm, html) => { banner = html; };
ctx._single_banner = (frm, id, cls, html) => { if (id === "optimus-phase2-retry-hint") hint = html; return null; };
const retryable = ["run-a", "run-b"];
const onloads = {allowed: {optimus_can_act: true, optimus_phase2_retryable: retryable},
  spent: {optimus_can_act: true, optimus_phase2_retryable: []}, unlisted: {optimus_can_act: true}, refused: {optimus_can_act: false}, empty: {}, missing: undefined};
const out = {};
for (const [status, runs] of [["Ready", [{run_uuid: "run-a", status: "Failed"}, {run_uuid: "run-b", status: "Failed"}]],
                              ["Ready", [{run_uuid: "run-c", status: "Recording"}]], ["Failed", []]]) {
  for (const [name, onload] of Object.entries(onloads)) {
    const labels = [];
    banner = hint = undefined;
    const doc = {session_uuid: "s", status, phase_2_runs: runs};
    if (onload !== undefined) doc.__onload = onload;
    const frm = {is_new: () => false, is_dirty: () => false, doc,
      add_custom_button: (label) => { labels.push(label); return {removeClass: () => ({addClass: () => {}})}; }};
    ctx.render_retry_button(frm); ctx.render_regenerate_report_button(frm); ctx.render_phase2_button(frm);
    out[`${status}:${runs.map((r) => r.status).join(",")}:${name}`] = {labels, banner: banner || null, hint: hint || null};
  }
}
process.stdout.write(JSON.stringify(out));
"""

SESSION_ACTIONS = {"Regenerate Reports", "Retry Analyze", "Run Line-Profile Pass", "Stop Line Profiling Run"}
VIEWERS = ("refused", "empty", "missing")


def _render():
	node = shutil.which("node")
	if not node:
		pytest.skip("node not installed")
	result = subprocess.run([node, "-e", SCRIPT, str(SOURCE)], capture_output=True, text=True, timeout=60)
	assert result.returncode == 0, result.stderr
	return json.loads(result.stdout)


def _session_actions(labels):
	return {label for label in labels if label in SESSION_ACTIONS or label.startswith("Retry ")}


def test_viewer_without_can_act_gets_no_session_actions():
	out = _render()
	for key, rendered in out.items():
		if key.rsplit(":", 1)[1] in VIEWERS:
			assert _session_actions(rendered["labels"]) == set(), key
	for viewer in VIEWERS:
		assert "Force Stop Stuck Run" in out[f"Ready:Failed,Failed:{viewer}"]["labels"]


def test_viewer_of_a_recording_run_gets_a_note_not_the_stop_instructions():
	out = _render()
	for viewer in VIEWERS:
		banner = out[f"Ready:Recording:{viewer}"]["banner"]
		assert banner == "Line profiling is recording on this session. The session owner can stop it."
	assert "Stop Line Profiling Run" in out["Ready:Recording:allowed"]["banner"]


def test_owner_keeps_every_session_action():
	out = _render()
	ready = set(out["Ready:Failed,Failed:allowed"]["labels"])
	assert {"Regenerate Reports", "Run Line-Profile Pass", "Retry next 2 line profiling runs"} <= ready
	assert "Stop Line Profiling Run" in out["Ready:Recording:allowed"]["labels"]
	assert {"Retry Analyze", "Regenerate Reports"} <= set(out["Failed::allowed"]["labels"])


def test_owner_gets_no_retry_for_runs_the_server_cannot_retry():
	out = _render()
	labels = out["Ready:Failed,Failed:spent"]["labels"]
	assert not [label for label in labels if "line profiling" in label.lower() and label.startswith("Retry")]
	assert "Run Line-Profile Pass" in labels


def test_owner_is_told_why_spent_runs_have_no_retry():
	out = _render()
	assert out["Ready:Failed,Failed:spent"]["hint"] == (
		"2 line profiling run(s) cannot be retried: the captured data is gone or the retry limit is reached. Record a new line-profile pass."
	)
	assert out["Ready:Failed,Failed:allowed"]["hint"] is None
	assert out["Ready:Failed,Failed:refused"]["hint"] is None


def test_retry_stays_offered_when_the_server_sent_no_retryable_list():
	labels = _render()["Ready:Failed,Failed:unlisted"]["labels"]
	assert "Retry next 2 line profiling runs" in labels
	assert "Retry Line Profiling Analyze (run-a)" in labels
