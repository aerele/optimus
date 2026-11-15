# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The line profiling picker dialog and start alert show escaped, correctly filled text.

Runs the real form script in node with a stubbed Frappe: the dialog's configuration and
the alert's message are captured and checked, so a lost escape or swapped {0}/{1} fails.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).parents[1] / "optimus/doctype/optimus_session/optimus_session.js"

SCRIPT = r"""
const vm = require("node:vm"), fs = require("node:fs");
const MAP = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "`": "&#x60;", "=": "&#x3D;"};
const out = {dialogs: [], alerts: [], indicators: []};
let reply = null;
function Dialog(config) { out.dialogs.push(config); this.show = () => {}; }
const ctx = {
  __: (t, a = []) => t.replace(/\{(\d+)\}/g, (_, n) => a[n]),
  frappe: {ui: {form: {on: () => {}}, Dialog}, realtime: {on: () => {}},
    utils: {escape_html: (s) => String(s).replace(/[&<>"'`=]/g, (c) => MAP[c])},
    show_alert: (a) => out.alerts.push(a),
    call: (c) => c.callback({message: reply})},
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
const job = JSON.parse(process.argv[2]);
const frm = {doc: {session_uuid: "s1"}, dashboard: {add_indicator: (t) => out.indicators.push(t)}};
if (job.dialog) ctx.show_phase2_dialog(frm, job.dialog);
if (job.start) { reply = job.start; ctx.start_phase2(frm, [], true); }
process.stdout.write(JSON.stringify(out));
"""

HOSTILE = "<img src=x onerror=alert(1)>"


def _run(job):
	node = shutil.which("node")
	if not node:
		pytest.skip("node not installed")
	result = subprocess.run(
		[node, "-e", SCRIPT, str(SOURCE), json.dumps(job)], capture_output=True, text=True, timeout=60
	)
	assert result.returncode == 0, result.stderr
	return json.loads(result.stdout)


def _field(dialog, name):
	return next(f for f in dialog["fields"] if f["fieldname"] == name)


def test_intro_marks_only_these_in_bold_and_leaves_no_placeholder():
	[dialog] = _run({"dialog": {}})["dialogs"]
	html = _field(dialog, "intro_html")["options"]
	assert "<strong>only these</strong> functions during your next reproduction" in html
	assert "{0}" not in html and "\x00" not in html


def test_empty_state_hint_is_escaped_text():
	[dialog] = _run({"dialog": {"diagnostic": {"hint": HOSTILE}}})["dialogs"]
	html = _field(dialog, "empty_state_html")["options"]
	assert "<img" not in html and "&lt;img" in html


def test_framework_label_gets_the_count():
	candidate = {"dotted_path": "frappe.a", "app": "frappe"}
	[dialog] = _run({"dialog": {"candidates": [candidate], "observations": [candidate, candidate]}})[
		"dialogs"
	]
	assert _field(dialog, "framework_section")["label"].startswith("+ 2 framework frames")


def _started(expansions, resolved=1):
	return {
		"run_uuid": "abcdef0123",
		"resolved_picks": [{"eligible": True}] * resolved,
		"expansions": expansions,
	}


def test_alert_fills_name_before_count_and_escapes_the_function_name():
	chain = {"original": f"app.mod.{HOSTILE}", "chain": [1, 2, 3]}
	[alert] = _run({"start": _started([chain, chain])})["alerts"]
	message = alert["message"]
	assert "<img" not in message
	assert "Auto-expanded &lt;img src&#x3D;x onerror&#x3D;alert(1)&gt; → 3 functions (+ 1 more)" in message


def test_alert_with_one_expansion_has_no_more_suffix():
	[alert] = _run({"start": _started([{"original": "app.mod.fn", "chain": [1, 2]}], resolved=2)})["alerts"]
	assert "started instrumenting 2 functions." in alert["message"]
	assert alert["message"].endswith("Auto-expanded fn → 2 functions")
