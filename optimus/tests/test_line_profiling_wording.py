"""Visible strings say "line profiling", not "Phase 1/2"."""

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent
PHASE_RE = re.compile(r"phase[ -](1|2)\b", re.IGNORECASE)
JS_STRING_RE = re.compile(r"\"([^\"\n]*)\"|'([^'\n]*)'|`([^`]*)`")
JS_FILES = (
	"optimus/doctype/optimus_session/optimus_session.js",
	"optimus/doctype/optimus_settings/optimus_settings.js",
	"public/js/floating_widget.js",
)
DOC_FILES = ("README.md", "SECURITY.md", "docs/AI-FIXING.md", "docs/AI-REFRESH.md", "docs/REDIS-SCHEMA.md")


def _js_strings(path: Path) -> list[str]:
	out = []
	for line in path.read_text().splitlines():
		if line.strip().startswith(("//", "*", "/*")):
			continue
		out.extend(next(g for g in m.groups() if g is not None) for m in JS_STRING_RE.finditer(line))
	return out


def _py_strings(path: Path) -> list[str]:
	tree = ast.parse(path.read_text())
	docstrings = set()
	for node in ast.walk(tree):
		if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
			body = node.body
			if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
				docstrings.add(id(body[0].value))
	return [
		n.value for n in ast.walk(tree)
		if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
	]


def test_js_has_no_phase_wording_in_strings():
	for rel in JS_FILES:
		for s in _js_strings(ROOT / rel):
			assert not PHASE_RE.search(s), (rel, s)


def test_python_user_facing_strings_have_no_phase_wording():
	skip = ("tests", "tests_integration", "patches")
	for path in sorted(ROOT.rglob("*.py")):
		if any(part in skip for part in path.relative_to(ROOT).parts):
			continue
		for s in _py_strings(path):
			assert not PHASE_RE.search(s), (str(path.relative_to(ROOT)), s)


def test_doctype_labels_and_descriptions_have_no_phase_wording():
	for rel in ("optimus/doctype/optimus_session/optimus_session.json", "optimus/doctype/optimus_settings/optimus_settings.json"):
		for field in json.loads((ROOT / rel).read_text())["fields"]:
			for key in ("label", "description", "options"):
				val = field.get(key) or ""
				if field.get("fieldtype") in ("Select", "Link", "Table"):
					continue
				assert not PHASE_RE.search(val), (rel, field["fieldname"], key)


def test_docs_have_no_phase_wording():
	for rel in DOC_FILES:
		for n, line in enumerate((REPO / rel).read_text().splitlines(), 1):
			assert not PHASE_RE.search(line), (rel, n, line)


def test_report_template_visible_hint_says_normal_profile():
	html = (ROOT / "templates/report.html").read_text()
	assert "in the normal profile)" in html
	assert "in phase&nbsp;1" not in html
