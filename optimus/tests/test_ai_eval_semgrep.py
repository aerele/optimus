# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Pinned frappe/semgrep-rules: the rule map, the workflow pins and the scanner the eval
uses on rendered answer code. The scanner tests need the semgrep CLI and
OPTIMUS_SEMGREP_RULES_DIR; they skip locally without them and FAIL in CI's ai-tests job
(REQUIRE_SEMGREP=1)."""

import json
import os
import re

import pytest

from optimus.tests.ai_eval_support import SCRIPTS_DIR, load, require_semgrep, semgrep_rules_dir

pytestmark = pytest.mark.semgrep

H = "**Diagnosis**: x\n**Fix**\n{}\n**Why it works**: y\n**Verify**: z"
KINDS = {"guardrail": "code", "prompt": "idiom", "n/a": "reason"}


@pytest.fixture(scope="module")
def rule_map():
	with open(os.path.join(SCRIPTS_DIR, "semgrep_rule_map.json"), encoding="utf-8") as fh:
		return json.load(fh)


def _outcome(fn) -> str:
	"""pytest's Failed and Skipped are BaseException subclasses; name the one raised."""
	try:
		fn()
	except BaseException as exc:
		return type(exc).__name__
	return "returned"


def test_require_semgrep_fails_instead_of_skipping_when_required(monkeypatch):
	monkeypatch.setenv("OPTIMUS_SEMGREP_RULES_DIR", "/nonexistent/rules")
	monkeypatch.setenv("REQUIRE_SEMGREP", "1")
	assert _outcome(require_semgrep) == "Failed"
	monkeypatch.setenv("REQUIRE_SEMGREP", "0")
	assert _outcome(require_semgrep) == "Skipped"


def test_rule_map_entries_are_well_formed(rule_map):
	assert re.fullmatch(r"[0-9a-f]{40}", rule_map["rules_commit"])
	for rule_id, entry in rule_map["rules"].items():
		assert entry["kind"] in KINDS, rule_id
		assert entry[KINDS[entry["kind"]]].strip(), rule_id
		assert "\u2014" not in json.dumps(entry, ensure_ascii=False), rule_id


def test_rule_map_covers_every_pinned_rule(rule_map):
	rules = semgrep_rules_dir()
	if not rules or not os.path.isdir(rules):
		require_semgrep()  # skips locally, fails under REQUIRE_SEMGREP=1
	ids = load("_semgrep").pinned_rule_ids(rules)
	assert len(ids) == len(set(ids))
	assert set(rule_map["rules"]) == set(ids), {
		"unmapped": sorted(set(ids) - set(rule_map["rules"])),
		"stale": sorted(set(rule_map["rules"]) - set(ids)),
	}


def test_rule_map_guardrail_codes_exist(rule_map):
	guardrails = pytest.importorskip("optimus.ai_guardrails")  # lands with #54
	codes = {e["code"] for e in rule_map["rules"].values() if e["kind"] == "guardrail"}
	assert codes <= set(guardrails.CODES), codes - set(guardrails.CODES)


def test_rule_map_guardrail_codes_are_block(rule_map):
	"""Owner decision D-SEMGREP: every guardrail code mapped to a pinned semgrep rule is a
	BLOCK code in #54 (re-ask once, then strip), so a violation of a semgrep-mapped rule
	never stays on screen as code. Activates when optimus.ai_guardrails lands (#54)."""
	guardrails = pytest.importorskip("optimus.ai_guardrails")
	codes = sorted({e["code"] for e in rule_map["rules"].values() if e["kind"] == "guardrail"})
	assert {c: guardrails.CODE_ACTIONS.get(c) for c in codes if guardrails.CODE_ACTIONS.get(c) != "block"} == {}


def _hits(text):
	rules = require_semgrep()
	return load("_semgrep").scan_texts({"a": text}, rules)["a"]


def test_scanner_counts_only_code_the_answer_adds():
	kept = _hits(H.format("```diff\n     frappe.db.commit()\n-    x = 1\n+    x = 2\n```"))
	assert kept.hits == [] and kept.units == 1
	added = _hits(H.format("```diff\n     x = 1\n+    frappe.db.commit()\n```"))
	assert [h["rule"] for h in added.hits] == ["frappe-manual-commit"]


def test_scanner_applies_module_and_doctype_scoped_rules():
	res = _hits(H.format("```python\nrows = frappe.db.sql(f\"select name from `tabUser` where name='{n}'\")\n```"))
	assert "frappe-sql-format-injection" in [h["rule"] for h in res.hits]
	res = _hits(H.format("```python\ndef validate(self):\n    for d in self.items:\n        self.remove(d)\n```"))
	assert "frappe-modifying-child-tables-while-iterating" in [h["rule"] for h in res.hits]


def test_moved_or_quoted_lines_are_not_introduced():
	"""R-INTRODUCED, one rule with #54's guardrail: a line is introduced unless it is among
	the diff's "-" lines or, in a plain block, verbatim from the shown source window."""
	sg = load("_semgrep")
	src = ["def validate(self):", "    frappe.throw('x')", "    frappe.db.commit()"]
	moved = H.format("```diff\n-    frappe.db.commit()\n-    frappe.throw('x')\n+    frappe.throw('x')\n+    frappe.db.commit()\n```")
	quoted = H.format("```python\nfrappe.throw('x')\nfrappe.db.commit()\n```")
	added = H.format("```diff\n     frappe.throw('x')\n+    frappe.db.commit()\n```")
	assert sg.units(moved, src) == []
	assert sg.units(quoted, src) == []
	assert len(sg.units(quoted, [])) == 1  # nothing shown: every plain-block line is introduced
	[unit] = sg.units(added, src)
	assert [ln.strip() for ln, is_new in unit.lines if is_new] == ["frappe.db.commit()"]
	ddl = H.format("```sql\nALTER TABLE `tabUser` ADD INDEX i (email);\n```")
	assert sg.units(ddl, src) == []  # code for the report, nothing for Python/JS rules


def test_scanner_skips_moved_lines_and_flags_new_ones():
	rules = require_semgrep()
	src = ["def validate(self):", "    frappe.db.commit()"]
	texts = {
		"moved": H.format("```diff\n-    frappe.db.commit()\n+    frappe.db.commit()  \n```"),
		"added": H.format("```diff\n     frappe.db.commit()\n+    frappe.db.rollback()\n+    frappe.db.commit()\n```"),
	}
	scanned = load("_semgrep").scan_texts(texts, rules, {"moved": src, "added": src})
	assert scanned["moved"].units == 0 and scanned["moved"].hits == []
	assert "frappe-manual-commit" in [h["rule"] for h in scanned["added"].hits]


def test_report_scans_only_introduced_code():
	"""report.score passes each case's source window, so a quoted source line is shown as
	code but never scanned."""
	rules = require_semgrep()
	rep = load("report")
	case = {"name": "x", "finding_type": "N+1 Query", "source_lines": ["def f(self):", "    frappe.db.commit()"],
		"window_first_lineno": 1, "target_lineno": 2, "technical_detail": {}}
	run = {"meta": {}, "labels": {}, "cases": [{"name": "x", "finding_type": "N+1 Query", "outcome": None,
		"result": {"suggestion": H.format("```python\nfrappe.db.commit()\n```")}, "error": None, "elapsed_s": 1.0}]}
	[row] = rep.score(run, {"cases": [case]}, semgrep_rules=rules)
	assert row["code"] is True and row["semgrep"] == [] and row["unparsed"] == 0


def test_scanner_sees_the_code_in_every_rendered_shape():
	from optimus.tests.test_ai_eval_answer import HOSTILE
	from optimus.tests.test_ai_eval_report import NESTED_HARMFUL

	rules = require_semgrep()
	texts = {shape: "**Fix**\n\n" + md for shape, md in HOSTILE.items()}
	texts["nested harmful answer"] = NESTED_HARMFUL
	for shape, res in load("_semgrep").scan_texts(texts, rules).items():
		assert "frappe-manual-commit" in [h["rule"] for h in res.hits], (shape, res)
		assert res.unparsed == 0, shape


def test_scanner_reports_unparsed_blocks_instead_of_clean():
	res = _hits(H.format("```python\ndef broken(:\n    frappe.db.commit()\n```"))
	assert res.unparsed == 1 and res.hits == []


def test_stored_answers_semgrep_baseline():
	"""The 2026-09-09 answers, scanned the way every eval run is. A change here means
	the pinned rules or the scanner changed; update deliberately."""
	rules = require_semgrep()
	corpus = load("_corpus").load_corpus()
	scanned = load("_semgrep").scan_texts({c["name"]: c["suggestion"] for c in corpus["cases"]}, rules)
	got = {name: sorted(h["rule"] for h in res.hits) for name, res in scanned.items() if res.hits}
	assert got == STORED_HITS
	assert sum(res.unparsed for res in scanned.values()) == STORED_UNPARSED


STORED_HITS: dict = {}
STORED_UNPARSED = 0


def test_javascript_parse_error_is_incomplete():
	res = _hits(H.format("```javascript\nfunction broken( {\nfrappe.throw('x');\n```"))
	assert res.unparsed == 1


def test_semgrep_skipped_files_are_not_clean(monkeypatch):
	from types import SimpleNamespace

	sg = load("_semgrep")
	monkeypatch.setattr(sg.subprocess, "run", lambda *a, **kw: SimpleNamespace(
		returncode=0, stdout=json.dumps({"results": [], "errors": [], "paths": {"scanned": []}}), stderr=""))
	res = sg.scan_texts({"case": "```python\nx = 1\n```"}, "/fake/rules")["case"]
	assert res.unparsed == 1
