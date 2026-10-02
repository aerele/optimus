# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Edits inside existing signatures and loops must not evade blocking rules."""

import pytest

from optimus import ai_guardrails as guardrails
from optimus.renderer.finding_enrichment import _markdown_to_safe_html
from optimus.tests.ai_eval_support import load, require_semgrep


def _answer(diff):
	text = "**Diagnosis**: x\n**Fix**\n```diff\n" + diff + "\n```\n**Why it works**: y\n**Verify**: z"
	source = [line[1:] for line in diff.splitlines() if line.startswith((" ", "-"))]
	return text, source


_UNSAFE = [
	("new-decorator", "whitelist-type-hints", "missing-argument-type-hint",
		"+@frappe.whitelist()\n def endpoint(value):\n     return value"),
	("multiline-argument", "whitelist-type-hints", "missing-argument-type-hint",
		" @frappe.whitelist()\n def endpoint(\n-    value: str,\n+    value,\n ):\n     return value"),
	("keyword-only", "whitelist-type-hints", "missing-argument-type-hint",
		"+@frappe.whitelist()\n+def endpoint(*, value):\n+    return value"),
	("positional-only", "whitelist-type-hints", "missing-argument-type-hint",
		"+@frappe.whitelist()\n+def endpoint(value, /):\n+    return value"),
	("remove-in-kept-loop", "child-modify-while-iterating", "frappe-modifying-child-tables-while-iterating",
		" def validate(self):\n     for row in self.items:\n-        consume(row)\n+        self.remove(row)"),
	("append-in-kept-loop", "child-modify-while-iterating", "frappe-modifying-child-tables-while-iterating",
		' def validate(self):\n     for row in self.items:\n-        consume(row)\n+        self.append("items", row)'),
	("multiline-iterator", "child-modify-while-iterating", "frappe-modifying-child-tables-while-iterating",
		" def validate(self):\n     for row in (\n-        saved_rows\n+        self.items\n     ):\n         self.remove(row)"),
]


@pytest.mark.parametrize("name,code,rule,diff", _UNSAFE, ids=[case[0] for case in _UNSAFE])
def test_changed_compound_parts_are_blocked_and_removed(name, code, rule, diff):
	text, source = _answer(diff)
	violations = guardrails.verify_fix(text, source_lines=source)
	assert {v.code for v in violations} == {code}
	assert guardrails.reaskable(violations) == violations
	assert "<pre" not in _markdown_to_safe_html(guardrails.apply_fallback(text, violations))


@pytest.mark.semgrep
def test_changed_compound_parts_agree_with_pinned_semgrep():
	texts, sources = {}, {}
	for name, _code, _rule, diff in _UNSAFE:
		texts[name], sources[name] = _answer(diff)
	results = load("_semgrep").scan_texts(texts, require_semgrep(), sources)
	for name, code, rule, _diff in _UNSAFE:
		assert results[name].unparsed == 0
		assert rule in {hit["rule"] for hit in results[name].hits}, name
		assert code in {v.code for v in guardrails.verify_fix(texts[name], source_lines=sources[name])}, name


@pytest.mark.parametrize("diff", [
	# Editing a function body does not introduce its existing untyped signature.
	" @frappe.whitelist()\n def endpoint(value):\n-    return slow(value)\n+    return cached(value)",
	# An unrelated loop-body edit does not introduce its existing removal call.
	" for row in self.items:\n     self.remove(row)\n-    audit(row)\n+    observe(row)",
	# The new decorator is safe when every fixed argument has a type hint.
	"+@frappe.whitelist()\n def endpoint(value: str, /, *, limit: int):\n     return value",
	# Iterating over a copy permits mutation of the original child table.
	" for row in list(self.items):\n-    consume(row)\n+    self.remove(row)",
	# Moving the whole loop preserves the accepted moved-line exemption.
	"-for row in self.items:\n-    self.remove(row)\n+for row in self.items:\n+    self.remove(row)\n+finish()",
])
def test_compound_checks_keep_unchanged_code_and_safe_edits(diff):
	text, source = _answer(diff)
	assert guardrails.verify_fix(text, source_lines=source) == []
