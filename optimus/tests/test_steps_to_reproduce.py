# optimus/tests/test_steps_to_reproduce.py
# Copyright (c) 2026, Optimus contributors

"""Tests for the Steps-to-Reproduce / Notes field.

The `notes` field on Optimus Session is a Text Editor so users can include rich
"what did you do during this session" context, rendered at the top of the
report above findings.
"""

import json
import os

HERE = os.path.dirname(__file__)


def test_report_template_renders_notes():
	"""The report template must render the notes section from a pre-sanitized
	variable: ``report_data.repro.raw_html``, sourced from the ``notes_html``
	that ``renderer.render`` builds via ``sanitize_html(always_sanitize=True)``."""
	tpath = os.path.join(HERE, "..", "templates", "report.html")
	with open(tpath) as f:
		template = f.read()

	# (A template that rendered the raw doc field through |safe is caught by
	# test_notes_sanitizer_compat, which renders a script canary through render_raw.)
	# The repro section reads from the contract-shaped report_data.repro.
	assert "report_data.repro" in template
	# "Steps to Reproduce" heading must appear so users know the purpose.
	assert "Steps to Reproduce" in template

	# Repro section renders ABOVE the "Findings what to fix" section.
	repro_idx = template.find("report_data.repro")
	findings_heading_idx = template.find("Findings - what to fix")
	assert repro_idx > 0
	assert findings_heading_idx > 0
	assert findings_heading_idx > repro_idx, (
		"repro section must appear above 'Findings what to fix'"
	)


# ---------------------------------------------------------------------------
# v0.5.1: auto-filled "Steps to Reproduce" from captured actions
# ---------------------------------------------------------------------------
# The start dialog no longer prompts for notes. At analyze time, if the
# user hasn't typed anything on the Optimus Session form, we synthesize
# a bullet list of the captured actions so reviewers see context when
# they open the report.


def test_auto_notes_empty_recordings_returns_empty_string():
	"""No recordings → no notes. The caller must be able to skip the
	assignment entirely rather than setting an empty <ol></ol>."""
	from optimus.analyze import _build_auto_notes_html

	assert _build_auto_notes_html([]) == ""


def test_auto_notes_produces_ordered_list_with_humanized_labels():
	"""The helper should run each recording through per_action._label
	so 'Save Sales Invoice' beats 'POST /api/method/frappe.client.save'
	in the rendered reproducer."""
	from optimus.analyze import _build_auto_notes_html

	recordings = [
		{
			"method": "POST",
			"path": "/api/method/frappe.client.save",
			"cmd": "frappe.client.save",
			"form_dict": {"doctype": "Sales Invoice"},
			"duration": 842.3,
			"calls": [],
		},
		{
			"method": "GET",
			"path": "/api/resource/Sales Invoice/INV-00042",
			"cmd": None,
			"duration": 58.1,
			"calls": [],
		},
	]
	html_out = _build_auto_notes_html(recordings)

	# Preamble explains auto-generation and invites editing.
	assert "Auto-generated" in html_out
	# Ordered list, not unordered order matters for reproducers.
	assert "<ol>" in html_out and "</ol>" in html_out
	# First recording resolves to "Save Sales Invoice" via per_action._label.
	assert "Save Sales Invoice" in html_out
	# Second recording falls through to METHOD + path.
	assert "GET /api/resource/Sales Invoice/INV-00042" in html_out
	# Duration rendered in milliseconds (rounded to 1 decimal).
	assert "842.3ms" in html_out
	assert "58.1ms" in html_out


def test_auto_notes_html_escapes_user_controlled_strings():
	"""A path containing <script> must NOT produce a live <script> tag
	in the stored value. The renderer also sanitizes on the way out,
	but defense in depth: escape at emit time too."""
	from optimus.analyze import _build_auto_notes_html

	recordings = [{
		"method": "GET",
		"path": "/<script>alert(1)</script>",
		"cmd": None,
		"duration": 10.0,
		"calls": [],
	}]
	html_out = _build_auto_notes_html(recordings)
	assert "<script>" not in html_out
	assert "&lt;script&gt;" in html_out


def test_auto_notes_caps_long_sessions_with_overflow_marker():
	"""A 200-action session shouldn't fill the notes field with 200
	<li> entries cap at 50 and surface a '… and N more' marker so
	users know the list is truncated."""
	from optimus.analyze import _AUTO_NOTES_MAX_ENTRIES, _build_auto_notes_html

	recordings = [
		{"method": "GET", "path": f"/item/{i}", "cmd": None,
		 "duration": 5.0, "calls": []}
		for i in range(_AUTO_NOTES_MAX_ENTRIES + 10)
	]
	html_out = _build_auto_notes_html(recordings)
	# Count the <li> entries should be cap + 1 (for the overflow marker).
	li_count = html_out.count("<li>")
	assert li_count == _AUTO_NOTES_MAX_ENTRIES + 1
	assert "10 more" in html_out


def test_auto_notes_unnamed_action_falls_back_gracefully():
	"""If somehow a recording has no cmd / path / method, the label
	resolver returns an empty string. The helper must substitute a
	placeholder rather than emit '<li>: 0 ms</li>'."""
	from optimus.analyze import _build_auto_notes_html

	recordings = [{"method": "", "path": "", "cmd": "",
	               "duration": 0, "calls": []}]
	html_out = _build_auto_notes_html(recordings)
	# Must not have a blank label the per_action._label fallback ends up
	# as " " (method + " " + path with both empty), so we check the
	# placeholder OR a non-empty label.
	assert "<li>" in html_out
	# The <li> content between <li> and the colon separator must be
	# non-whitespace otherwise the reproducer is " 0 ms" which is
	# useless to a reader.
	import re
	m = re.search(r"<li>([^<]*?): ", html_out)
	assert m is not None
	label = m.group(1).strip()
	assert label, f"Auto-notes emitted blank label: {html_out!r}"


def test_auto_notes_filters_realtime_polling_noise():
	"""Only genuine user actions belong in the reproducer: given savedocs plus
	two ``frappe.realtime.has_permission`` polls, only the savedocs survives (the
	polls stay visible in the per-action table)."""
	import json as _json

	from optimus.analyze import _build_auto_notes_html

	recordings = [
		{
			"method": "GET",
			"path": "/api/method/frappe.realtime.has_permission",
			"cmd": "frappe.realtime.has_permission",
			"duration": 25.0,
			"calls": [],
		},
		{
			"method": "POST",
			"path": "/api/method/frappe.desk.form.save.savedocs",
			"cmd": "frappe.desk.form.save.savedocs",
			"duration": 774.8,
			"calls": [],
			"form_dict": {
				"doc": _json.dumps({
					"doctype": "Sales Invoice",
					"__islocal": 1,
				}),
				"action": "Save",
			},
		},
		{
			"method": "GET",
			"path": "/api/method/frappe.realtime.has_permission",
			"cmd": "frappe.realtime.has_permission",
			"duration": 6.0,
			"calls": [],
		},
	]

	html_out = _build_auto_notes_html(recordings)
	# Only the savedocs survives humanized as "Create Sales Invoice".
	assert "Create Sales Invoice" in html_out
	assert "774.8ms" in html_out
	# Polling endpoints filtered out
	assert "has_permission" not in html_out
	assert "frappe.realtime" not in html_out
	# Footer tells the user noise was filtered so they don't wonder
	# why only 1 entry showed from a 3-request session.
	assert "2 background / polling request(s) filtered" in html_out


def test_auto_notes_filters_static_assets_and_form_load_boilerplate():
	"""Static /assets/ requests and form-metadata loads are noise too."""
	from optimus.analyze import _build_auto_notes_html

	recordings = [
		{
			"method": "GET",
			"path": "/assets/frappe/dist/js/desk.bundle.js",
			"cmd": None,
			"duration": 180,
			"calls": [],
		},
		{
			"method": "GET",
			"path": "/api/method/frappe.desk.form.load.getdoctype",
			"cmd": "frappe.desk.form.load.getdoctype",
			"duration": 120,
			"calls": [],
			"form_dict": {"doctype": "Sales Invoice"},
		},
		{
			"method": "POST",
			"path": "/api/method/frappe.client.submit",
			"cmd": "frappe.client.submit",
			"duration": 500,
			"calls": [],
			"form_dict": {"doctype": "Sales Invoice"},
		},
	]
	html_out = _build_auto_notes_html(recordings)
	assert "Submit Sales Invoice" in html_out
	assert "desk.bundle.js" not in html_out
	assert "getdoctype" not in html_out
	assert "2 background / polling" in html_out


def test_auto_notes_all_noise_returns_empty_string():
	"""A session of only polling/noise returns empty the caller
	then leaves the notes field blank rather than filling it with
	the preamble and an empty list."""
	from optimus.analyze import _build_auto_notes_html

	recordings = [
		{
			"method": "GET",
			"path": "/api/method/frappe.realtime.has_permission",
			"cmd": "frappe.realtime.has_permission",
			"duration": 5.0,
			"calls": [],
		}
	] * 20
	html_out = _build_auto_notes_html(recordings)
	assert html_out == ""


# --------------------------------------------------------------------------
# v0.6.0: LLM-humanized "Steps to Reproduce"
# --------------------------------------------------------------------------

def test_actions_for_humanizer_compacts_recordings():
	"""actions_for_humanizer should drop noise, cap and emit the
	compact {label, cmd, path, method, doctype, duration_ms} dicts the
	humanizer prompt expects."""
	from optimus.analyze import actions_for_humanizer

	recordings = [
		{"cmd": "frappe.realtime.has_permission", "duration": 3, "calls": []},  # noise
		{"cmd": "frappe.desk.form.save.savedocs", "duration": 780, "calls": [],
		 "form_dict": {"doc": json.dumps({"doctype": "Sales Invoice", "__islocal": 1}),
		               "action": "Save"}},
		{"method": "GET", "path": "/api/method/foo.bar", "cmd": None, "duration": 12, "calls": []},
	]
	out = actions_for_humanizer(recordings)
	assert len(out) == 2  # the realtime one is filtered
	first = out[0]
	assert first["cmd"] == "frappe.desk.form.save.savedocs"
	assert first["doctype"] == "Sales Invoice"
	assert "Sales Invoice" in first["label"]
	assert out[1]["path"] == "/api/method/foo.bar"
	# All the expected keys present.
	for k in ("label", "cmd", "path", "method", "doctype", "duration_ms"):
		assert k in first


def test_assemble_humanized_notes_is_just_the_friendly_steps():
	"""Only the preamble + the rendered Markdown steps no raw
	"Captured actions" appendix (the per-action breakdown in the report
	already lists every action with its technical label)."""
	from optimus.analyze import assemble_humanized_notes

	html_out = assemble_humanized_notes("1. Submit a Delivery Note.\n\n**Summary:** submitting a DN.")
	assert "drafted by AI" in html_out
	assert "Submit a Delivery Note" in html_out
	assert "Captured actions" not in html_out
	assert "what actually hit the server" not in html_out
