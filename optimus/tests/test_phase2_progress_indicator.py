# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The form's line profiling analyzing indicator counts only runs that are analyzing."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[1] / "optimus/doctype/optimus_session/optimus_session.js"

SCRIPT = r"""
const vm = require("node:vm"), fs = require("node:fs");
const ctx = {__: (t, a = []) => t.replace(/\{(\d+)\}/g, (_, n) => a[n]),
  frappe: {ui: {form: {on: () => {}}}, realtime: {on: () => {}}}};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
const out = {};
for (const statuses of [["Recording"], ["Recording", "Analyzing"], ["Analyzing", "Analyzing"]]) {
  const shown = [];
  const frm = {is_new: () => false, doc: {phase_2_runs: statuses.map((status) => ({status}))},
    dashboard: {add_indicator: (label) => shown.push(label)}};
  ctx.render_phase2_progress(frm);
  out[statuses.join(",")] = shown;
}
process.stdout.write(JSON.stringify(out));
"""


def test_a_recording_run_is_not_counted_as_analyzing():
	node = shutil.which("node")
	if not node:
		pytest.skip("node not installed")
	result = subprocess.run([node, "-e", SCRIPT, str(SOURCE)], capture_output=True, text=True, timeout=60)
	assert result.returncode == 0, result.stderr
	assert json.loads(result.stdout) == {
		"Recording": [],
		"Recording,Analyzing": ["Line profiling analyzing… (1 run in progress)"],
		"Analyzing,Analyzing": ["Line profiling analyzing… (2 runs in progress)"],
	}
