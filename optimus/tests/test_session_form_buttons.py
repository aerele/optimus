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
ctx._phase2_armed_banner = () => {};
const out = {};
for (const [status, runs] of [["Ready", [{run_uuid: "run-a", status: "Failed"}, {run_uuid: "run-b", status: "Failed"}]],
                              ["Ready", [{run_uuid: "run-c", status: "Recording"}]], ["Failed", []]]) {
  for (const can_act of [true, false]) {
    const labels = [];
    const frm = {is_new: () => false, is_dirty: () => false,
      doc: {__onload: {optimus_can_act: can_act}, session_uuid: "s", status, phase_2_runs: runs},
      add_custom_button: (label) => { labels.push(label); return {removeClass: () => ({addClass: () => {}})}; }};
    ctx.render_retry_button(frm); ctx.render_regenerate_report_button(frm); ctx.render_phase2_button(frm);
    out[`${status}:${runs.map((r) => r.status).join(",")}:${can_act}`] = labels;
  }
}
process.stdout.write(JSON.stringify(out));
"""

SESSION_ACTIONS = {"Regenerate Reports", "Retry Analyze", "Run Line-Profile Pass", "Stop Line Profiling Run"}


def _render():
	node = shutil.which("node")
	if not node:
		pytest.skip("node not installed")
	result = subprocess.run([node, "-e", SCRIPT, str(SOURCE)], capture_output=True, text=True, timeout=60)
	assert result.returncode == 0, result.stderr
	return json.loads(result.stdout)


def _session_actions(labels):
	return {label for label in labels if label in SESSION_ACTIONS or label.startswith("Retry ")}


def test_read_only_viewer_gets_no_session_actions():
	out = _render()
	for key, labels in out.items():
		if key.endswith(":false"):
			assert _session_actions(labels) == set(), key
	assert "Force Stop Stuck Run" in out["Ready:Failed,Failed:false"]


def test_owner_keeps_every_session_action():
	out = _render()
	ready = set(out["Ready:Failed,Failed:true"])
	assert {"Regenerate Reports", "Run Line-Profile Pass", "Retry next 2 line profiling runs"} <= ready
	assert "Stop Line Profiling Run" in out["Ready:Recording:true"]
	assert {"Retry Analyze", "Regenerate Reports"} <= set(out["Failed::true"])
