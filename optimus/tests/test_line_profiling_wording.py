"""Visible strings say "line profiling", not "Phase 1/2"."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PHASE_RE = re.compile(r"phase[ -]?(1|2)\b", re.IGNORECASE)


def _visible_js_strings(path: Path) -> list[str]:
	out = []
	for line in path.read_text().splitlines():
		if line.strip().startswith(("//", "*", "/*")):
			continue
		out.extend(re.findall(r"\"([^\"\n]*)\"", line))
	return out


def test_js_has_no_phase_wording_in_strings():
	for rel in ("optimus/doctype/optimus_session/optimus_session.js", "public/js/floating_widget.js"):
		for s in _visible_js_strings(ROOT / rel):
			if " " not in s:
				continue
			assert not PHASE_RE.search(s), (rel, s)


def test_doctype_labels_and_descriptions_have_no_phase_wording():
	for rel in ("optimus/doctype/optimus_session/optimus_session.json", "optimus/doctype/optimus_settings/optimus_settings.json"):
		for field in json.loads((ROOT / rel).read_text())["fields"]:
			for key in ("label", "description", "options"):
				val = field.get(key) or ""
				if field.get("fieldtype") in ("Select", "Link", "Table"):
					continue
				assert not PHASE_RE.search(val), (rel, field["fieldname"], key)


def test_report_template_visible_hint_says_normal_profile():
	html = (ROOT / "templates/report.html").read_text()
	assert "in the normal profile)" in html
	assert "in phase&nbsp;1" not in html
