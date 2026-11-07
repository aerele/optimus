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
const TRANSLATIONS = {"Use Retry Analyze to run it again while its captured input is available.": "RETRY-HINT"};
const ctx = {
  __: (t, a = []) => (TRANSLATIONS[t] || t).replace(/\{(\d+)\}/g, (_, n) => a[n]),
  frappe: {ui: {form: {on: () => {}}}, realtime: {on: () => {}},
    utils: {escape_html: (s) => String(s).replace(/[&<>"'`=]/g, (c) => MAP[c])}}};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
const cases = JSON.parse(process.argv[2]);
const out = cases.map((c) => {
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
	assert "RETRY-HINT" in actor
	assert "Analysis failed (KeyError)" in viewer and "RETRY-HINT" not in viewer and "Retry" not in viewer


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
