# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The Failed session form says why, as text (run in node against the real form script)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from optimus import analyze

SOURCE = Path(__file__).parents[1] / "optimus/doctype/optimus_session/optimus_session.js"

SCRIPT = r"""
const vm = require("node:vm"), fs = require("node:fs");
// Frappe's escape_html (utils.js) and the translations a site could load.
const MAP = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "`": "&#x60;", "=": "&#x3D;"};
const TRANSLATIONS = {"Use Retry Analyze to run it again while its captured input is available.": "RETRY-HINT",
  "Ask the session owner to run Retry Analyze.": "ASK-OWNER", "Recording snapshot could not be saved. Redis input was retained for up to 7 days; do not clear Recorder data.": "SNAPSHOT-T"};
const ROLE = {};
const ctx = {
  __: (t, a = []) => (TRANSLATIONS[t] || t).replace(/\{(\d+)\}/g, (_, n) => a[n]),
  frappe: {ui: {form: {on: () => {}}}, realtime: {on: () => {}},
    user: {has_role: (r) => r === "System Manager" && !!ROLE.admin},
    utils: {escape_html: (s) => String(s).replace(/[&<>"'`=]/g, (c) => MAP[c])}}};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
const cases = JSON.parse(process.argv[2]);
const out = cases.map((c) => {
  ROLE.admin = c.admin !== false;
  const intros = [];
  const frm = {is_new: () => !!c.is_new, set_intro: (html, color) => intros.push([html, color]),
    doc: {status: c.status, analyzer_warnings: c.warnings, __onload: {optimus_can_act: !!c.can_act}}};
  ctx.render_failure_reason(frm);
  return intros;
});
process.stdout.write(JSON.stringify(out));
"""

HOSTILE = '<img src=x onerror=alert(1)> it\'s "quoted" & `tick`\nsecond line'
GENERIC = analyze.ANALYZE_FAILURE_WARNING.format("KeyError")
ENTITY = {"&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&#x60;": "`", "&#x3D;": "=", "&amp;": "&"}


def _intros(*cases):
	node = shutil.which("node")
	if not node:
		pytest.skip("node not installed")
	result = subprocess.run(
		[node, "-e", SCRIPT, str(SOURCE), json.dumps(cases)], capture_output=True, text=True, timeout=60
	)
	assert result.returncode == 0, result.stderr
	return json.loads(result.stdout)


def _shown_text(html):
	"""What jQuery's .html() would show: the markup parsed, entities decoded, <br> a line break."""
	inner = html.removeprefix('<div class="optimus-failure-reason">').removesuffix("</div>")
	text = inner.replace("<br>", "\n")
	for entity, char in ENTITY.items():
		text = text.replace(entity, char)
	return text


def test_reason_is_an_element_whose_shown_text_is_the_raw_message():
	[[[html, color]]] = _intros({"status": "Failed", "warnings": HOSTILE})
	assert color == "red"
	assert html.startswith("<div"), "a string with no element would be shown as text, entities and all"
	assert "<img" not in html and "onerror=alert" in html.replace("&#x3D;", "=")
	assert _shown_text(html) == HOSTILE
	assert html.count("<br>") == 1


def test_generic_failure_names_the_error_type_and_hints_retry_only_to_those_who_can_act():
	[[[actor, _]], [[viewer, _]]] = _intros(
		{"status": "Failed", "warnings": GENERIC, "can_act": True},
		{"status": "Failed", "warnings": GENERIC, "can_act": False},
	)
	assert "Analysis failed (KeyError)" in actor and "optimus ai analyze" in actor
	assert "RETRY-HINT" in actor and "ASK-OWNER" not in actor
	assert "Analysis failed (KeyError)" in viewer and "ASK-OWNER" in viewer
	assert "RETRY-HINT" not in viewer and "Use Retry Analyze" not in viewer


def test_error_log_is_named_only_to_a_system_manager():
	[[[manager, _]], [[other, _]]] = _intros(
		{"status": "Failed", "warnings": GENERIC, "admin": True},
		{"status": "Failed", "warnings": GENERIC, "admin": False},
	)
	assert "Error Log under" in manager and "optimus ai analyze" in manager
	assert "Error Log under" not in other and "Ask a system administrator to check the Error Log." in other


def test_error_type_in_any_script_is_recognized():
	warning = analyze.ANALYZE_FAILURE_WARNING.format("\u00c9chec\u9519\u8bef")
	[[[html, _]]] = _intros({"status": "Failed", "warnings": warning, "admin": False})
	# only a recognized sentence is rewritten for this role; an unmatched one is shown as stored
	assert "Analysis failed (\u00c9chec\u9519\u8bef)" in html and "Ask a system administrator" in html


JANITOR = (
	"Analyze job timed out, crashed, or its analyze heartbeat lapsed (the optimus log names a failed or yielded "
	"heartbeat). The session owner, or anyone who can edit it, can use Retry Analyze; saved reports and retained "
	"input are kept."
)
CAP = (
	"Scheduler is disabled and this session has 80 recordings, exceeding the inline analyze cap of 50. "
	"Re-enable the scheduler (bench enable-scheduler) and click Retry Analyze on this session's form view."
)


def test_stored_janitor_snapshot_and_cap_sentences_are_translatable_and_hint_by_role():
	from optimus.recording_bundle import SNAPSHOT_FAILURE_WARNING

	cases = [{"status": "Failed", "warnings": w, "can_act": a} for w in (JANITOR, SNAPSHOT_FAILURE_WARNING, CAP) for a in (True, False)]
	got = [intro[0][0] for intro in _intros(*cases)]
	janitor_a, janitor_v, snapshot_a, snapshot_v, cap_a, cap_v = got
	assert "The session owner, or anyone" not in janitor_a + janitor_v
	assert "heartbeat lapsed" in janitor_a and "RETRY-HINT" in janitor_a and "ASK-OWNER" in janitor_v
	assert "SNAPSHOT-T" in snapshot_a and "RETRY-HINT" in snapshot_a and "ASK-OWNER" in snapshot_v
	assert "has 80 recordings, exceeding the inline analyze cap of 50." in cap_a and "RETRY-HINT" in cap_a
	assert "click Retry Analyze" not in cap_v and "ASK-OWNER" in cap_v and "RETRY-HINT" not in cap_v


@pytest.mark.parametrize(
	"case",
	[
		{"status": "Ready", "warnings": GENERIC},
		{"status": "Failed", "warnings": None},
		{"status": "Failed", "warnings": ""},
		{"status": "Failed", "warnings": GENERIC, "is_new": True},
	],
)
def test_no_intro_unless_a_failed_saved_session_has_a_reason(case):
	assert _intros(case) == [[]]


def test_error_type_in_the_stored_text_cannot_carry_markup():
	[[[html, _]]] = _intros(
		{
			"status": "Failed",
			"warnings": "Analysis failed (<b>x</b>). Details are in the Error Log under 'optimus ai analyze'.",
		}
	)
	assert "<b>" not in html


def test_long_error_type_may_wrap_inside_the_notice():
	css = (Path(__file__).parents[1] / "public/css/floating_widget.css").read_text()
	rule = css[css.index(".optimus-failure-reason") :].split("}")[0]
	assert "overflow-wrap: anywhere" in rule
