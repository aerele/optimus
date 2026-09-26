# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for optimus.ai_guardrails: the pure verifier behind AI fix suggestions.

The corpus is PR-E's committed fixture, read through the eval kit's loader: the 15
answers qwen3-coder:30b gave on 2026-09-09, the source window each finding pointed at
with draft labels kept separate until the owner confirms them. These tests never read the bench.
"""

import json
import os
import re

import pytest

from optimus import ai_guardrails as G
from optimus import ai_prompts
from optimus.renderer.finding_enrichment import _markdown_to_safe_html
from optimus.tests.ai_eval_support import SCRIPTS_DIR, load

CORPUS = load("_corpus").cases()
H = "**Diagnosis**: x\n**Fix**\n{}\n**Why it works**: y\n**Verify**: z"


def codes(text, src=()):
	return {v.code for v in G.verify_fix(text, source_lines=list(src))}


# ---------------------------------------------------------------- real corpus
BY_NAME = {c["name"]: c for c in CORPUS}
_BAD_LABELS = {"wrong", "harmful", "fabricated"}

# Answers the static verifier catches.
CAUGHT = {
	"3q1dsfrmti": {"ungrounded", "no-op-diff"},  # fabricated code
	"3q1j3utnoa": {"ungrounded"},  # fabricated code
	"5qid8t8lu5": {"customize-form-index"},  # a wrong Frappe fact, corrected by a note
}

# Wrong or harmful answers that no static rule can see (semantic errors). They pass
# verify_fix; the live eval and the owner's labels measure them. A rule that starts
# catching one fails test_known_blind_spots_pass_the_verifier: move it to CAUGHT.
KNOWN_BLIND_SPOTS = {
	"5qih2paean": "diff to ERPNext's SalesInvoice.validate adds a `_validated` flag that skips validation",
	"5qimkr12p6": "changes the returned value (`return out * 2000`)",
	"3q1ln8bv2o": "grounded, but the query stays inside the loop and get_all changes the ordering",
	"3q1fsdhl5p": "grounded, but fixes the get_doc loop instead of the has_permission loop",
	"3q1qg8btng": "grounded, but fixes the get_doc loop instead of the has_permission loop",
}


def _corpus_codes(name):
	c = BY_NAME[name]
	return codes(c["suggestion"], c["source_lines"])


def test_real_corpus_caught_cases():
	assert {n: _corpus_codes(n) for n in CAUGHT} == CAUGHT


def test_known_blind_spots_pass_the_verifier():
	assert {n: _corpus_codes(n) for n in KNOWN_BLIND_SPOTS} == {n: set() for n in KNOWN_BLIND_SPOTS}


def test_rest_of_the_corpus_is_clean():
	rest = sorted(set(BY_NAME) - set(CAUGHT) - set(KNOWN_BLIND_SPOTS))
	assert len(rest) == 7  # incl. the 3 verbatim hoists
	assert {n: _corpus_codes(n) for n in rest} == {n: set() for n in rest}


def test_owner_labels_agree_with_the_split():
	# Every answer the owner labelled wrong or harmful is caught or a named blind
	# spot, and every blind spot is labelled bad (if the owner labels one correct,
	# drop it from KNOWN_BLIND_SPOTS).
	if not all(c.get("label") for c in CORPUS):
		pytest.skip("Owner labels pending; this draft cannot pass the live merge gate")
	bad = {c["name"] for c in CORPUS if c["label"]["label"] in _BAD_LABELS}
	assert bad <= set(CAUGHT) | set(KNOWN_BLIND_SPOTS), bad - set(CAUGHT) - set(KNOWN_BLIND_SPOTS)
	assert set(KNOWN_BLIND_SPOTS) <= bad, set(KNOWN_BLIND_SPOTS) - bad


# ---------------------------------------------------------------- raw SQL policy (N2)
def test_hoisted_raw_sql_is_not_flagged_but_new_raw_sql_is():
	hoist = '```diff\n-for i in r:\n-    x = frappe.db.sql("select 1")\n+x = frappe.db.sql("select 1")\n+for i in r:\n```'
	src = ["for i in r:", '    x = frappe.db.sql("select 1")']
	assert codes(H.format(hoist), src) == set()
	assert "raw-sql" in codes(H.format("```python\nx = frappe.db.sql(q)\n```"))


def test_reshape_into_bind_params_allowed_fstring_blocked():
	src = ["rows = frappe.db.sql(f\"select name from `tabUser` where email='{e}'\")"]
	ok = "```diff\n-" + src[0] + '\n+rows = frappe.db.sql("select name from `tabUser` where email=%(e)s", {"e": e})\n```'
	bad = "```diff\n-" + src[0] + "\n+rows = frappe.db.sql(f\"select name from `tabUser` where name='{e}'\")\n```"
	assert codes(H.format(ok), src) == set()
	assert "sql-format-injection" in codes(H.format(bad), src)


def test_fence_variants_and_aliases_detected():
	for body in (
		"~~~python\nr = frappe.db.sql(q)\n~~~",
		"````python\nr = frappe.db.sql(q)\n````",
		"```python {linenos=true}\nr = frappe.db.sql(q)\n```",
		"```python\ndb = frappe.db\nr = db.sql(q)\n```",
		"```python\nr = frappe.local.db.sql(q)\n```",
		"```python\nr = frappe .db .sql(q)\n```",
	):
		assert "raw-sql" in codes(H.format(body)), body


def test_long_fence_containing_short_fence():
	body = "````markdown\nexample:\n```\nnot a fence end\n```\n````\n```python\nr = frappe.db.sql(q)\n```"
	assert "raw-sql" in codes(H.format(body))


def test_string_literal_mention_is_not_a_call():
	assert codes(H.format("```python\nmsg = 'avoid frappe.db.sql(...)'\n```")) == set()


def test_ddl_exemption_is_gone():
	assert "raw-ddl" in codes(H.format('```python\nfrappe.db.sql("ALTER TABLE `tabX` ADD INDEX i (a)")\n```'))
	assert "raw-ddl" in codes(H.format("```sql\nCREATE INDEX i ON `tabX` (a);\n```"))
	assert "raw-sql" in codes(H.format('```python\nfrappe.db.sql("DROP TABLE `tabX`")\n```'))


def test_multisql_dicts_are_violations():
	# The old regex guardrail exempted a multisql dict of DDL strings. Now DDL in any form is
	# raw-ddl and any net-new frappe.db.multisql is raw-sql.
	ddl = "```diff\n+frappe.db.multisql({\n+    'mariadb': \"ALTER TABLE `tabX` ADD INDEX idx (a)\",\n+})\n```"
	dml = "```diff\n+rows = frappe.db.multisql({'mariadb': 'SELECT name FROM `tabX`'})\n```"
	assert "raw-ddl" in codes(H.format(ddl))
	assert "raw-sql" in codes(H.format(dml))


def test_inline_code_ddl_is_raw_ddl():
	assert "raw-ddl" in codes(H.format("Run `ALTER TABLE tabX ADD INDEX i (a)` once."))


# ---------------------------------------------------------------- semgrep-equivalent rules
def test_semgrep_equivalent_rules():
	cases = {
		"enqueue-without-after-commit": "frappe.enqueue('a.b', queue='long', name=doc.name)",
		"single-doctype-value": "v = frappe.db.get_value('Stock Settings', None, 'allow_negative_stock')",
		"unchecked-permission": "frappe.has_permission('Customer', 'read', doc)",
		"qb-orderby-positional": "q = q.orderby('total', frappe.qb.desc)",
		"manual-commit": "frappe.db.commit()",
		"eval-exec": "eval(expr)",
		"ignore-permissions": "frappe.get_list('X', ignore_permissions=True)",
		"local-state": "frappe.local.cache_x = {}",
		"multitenant-cache": "@lru_cache(maxsize=10)\ndef f(a):\n    return a",
		"whitelist-type-hints": "@frappe.whitelist()\ndef f(a):\n    return a",
		"untranslated": "frappe.throw('Bad value')",
		"child-modify-while-iterating": "for d in self.items:\n    self.remove(d)",
		"modify-not-saved": "def on_submit(self):\n    self.total = 1",
	}
	for code, snippet in cases.items():
		assert code in codes(H.format("```python\n" + snippet + "\n```")), code
	clean = (
		"frappe.enqueue('a.b', queue='long', enqueue_after_commit=True)\n"
		"v = frappe.db.get_single_value('Stock Settings', 'allow_negative_stock')\n"
		"frappe.has_permission('Customer', 'read', doc, throw=True)\n"
		"q = q.orderby('total', order=frappe.qb.desc)\n"
	)
	assert codes(H.format("```python\n" + clean + "```")) == set()


def test_permission_downgrade():
	src = ["for g in groups:", "    out[g] = frappe.get_list('Item', filters={'item_group': g})"]
	bad = "```diff\n-" + "\n-".join(src) + "\n+rows = frappe.get_all('Item', filters={'item_group': ('in', groups)})\n```"
	good = bad.replace("frappe.get_all", "frappe.get_list")
	assert "permission-downgrade" in codes(H.format(bad), src)
	assert codes(H.format(good), src) == set()


# ---------------------------------------------------------------- index advice
def test_metadata_index_rules():
	assert "metadata-index" in codes(H.format("```python\nfrappe.db.add_index('SI', ['modified', 'status'])\n```"))
	assert codes(H.format("```python\nfrappe.db.add_index('SI', ['customer', 'creation'])\n```")) == set()
	assert codes(H.format("The table already has the primary-key index on `name`.")) == set()
	assert "metadata-index" in codes(H.format("Add an index on `modified` to speed this up."))


# ---------------------------------------------------------------- tiers
_TIERS = {
	"block": {
		"ungrounded", "no-source-diff", "no-op-diff", "raw-sql", "raw-ddl", "sql-format-injection",
		"ignore-permissions", "allow-guest", "permission-downgrade", "eval-exec", "unsafe-deserialize",
		"shell-exec", "set-user-admin", "manual-commit", "multitenant-cache",
		"enqueue-without-after-commit", "headings",
		# D-SEMGREP (owner, cycle 2): the semgrep-mapped codes are block
		"untranslated", "whitelist-type-hints", "qb-orderby-positional", "single-doctype-value",
		"modify-not-saved", "child-modify-while-iterating", "module-state", "local-state",
		"unchecked-permission",
	},
	"truncated": {"truncated"},
	"advise": {"dynamic-import", "metadata-index"},
	"note": {"customize-form-index", "context-truncated", "markdown-image", "external-link", "echoed-data-tag"},
}


def test_tiers_are_exactly_the_agreed_sets():
	got: dict[str, set] = {}
	for code, action in G.CODE_ACTIONS.items():
		got.setdefault(action, set()).add(code)
	assert got == _TIERS


def test_every_semgrep_mapped_guardrail_code_is_block():
	# D-SEMGREP: a code that a pinned Frappe semgrep rule maps to (PR-E's rule map) is
	# re-asked and then stripped, so rendered code never carries a mapped semgrep hit.
	with open(os.path.join(SCRIPTS_DIR, "semgrep_rule_map.json"), encoding="utf-8") as fh:
		rule_map = json.load(fh)
	mapped = {e["code"] for e in rule_map["rules"].values() if e["kind"] == "guardrail"}
	assert mapped and {c: G.CODE_ACTIONS[c] for c in mapped} == {c: "block" for c in mapped}


def test_violation_takes_its_tier_from_the_registry():
	assert G.Violation("dynamic-import").action == "advise"
	assert G.Violation("untranslated").action == "block"
	assert G.Violation("raw-sql").action == "block"
	assert G.Violation("context-truncated").action == "note"


_ADVISE_ONLY = H.format("```python\nmod = importlib.import_module(name)\n```")


def test_advise_only_answer_is_not_reaskable_and_keeps_its_code():
	vs = G.verify_fix(_ADVISE_ONLY, source_lines=[])
	assert {v.code for v in vs} == {"dynamic-import"}
	assert G.reaskable(vs) == [] and not G.strips_code(vs)
	out = G.apply_fallback(_ADVISE_ONLY, vs)
	assert "importlib.import_module(name)" in out and "```python" in out
	assert out.count("> **Profiler note:**") == 1
	assert "use a normal import" in out


def test_untranslated_throw_is_block_reasked_then_stripped():
	# D-SEMGREP: frappe-missing-translate-function-python is a pinned rule, so an
	# untranslated throw is re-asked and, if it survives the re-ask, its code is removed.
	text = H.format("```python\ndef on_submit(self):\n    frappe.throw('Total is too high')\n```")
	vs = G.verify_fix(text, source_lines=[])
	assert [(v.code, v.action) for v in vs] == [("untranslated", "block")]
	assert "`_()`" in G.reask_message(vs) and G.strips_code(vs)
	body = G.apply_fallback(text, vs).split("> **Profiler note:**", 1)[0]
	assert "frappe.throw" not in body and "```" not in body


def test_block_violation_strips_code_and_advice_is_then_dropped():
	text = H.format("```python\nrows = frappe.db.sql(q)\nmod = importlib.import_module(name)\n```")
	vs = G.verify_fix(text, source_lines=[])
	assert {v.code for v in vs} == {"raw-sql", "dynamic-import"}
	assert [v.code for v in G.reaskable(vs)] == ["raw-sql"]
	out = G.apply_fallback(text, vs)
	body, note = out.split("> **Profiler note:**", 1)
	assert "```" not in body and "frappe.db.sql" not in body
	assert "must not call `frappe.db.sql`" in note and "normal import" not in out


def test_strip_note_names_each_rule_in_plain_english():
	# D-SEMGREP codes such as module-state are not self-explanatory: the note carries the
	# rule text, two rules at most, then a pointer to the docs.
	text = H.format("```python\nSETTINGS = frappe.get_all('Item')\n\n\ndef f():\n    return SETTINGS\n```")
	vs = G.verify_fix(text, source_lines=[])
	assert [v.code for v in vs] == ["module-state"]
	note = G.apply_fallback(text, vs).split("> **Profiler note:**", 1)[1]
	assert "they leak across sites" in note and "module-state" not in note
	many = [G.Violation(c) for c in ("raw-sql", "manual-commit", "eval-exec", "local-state")]
	note = G.apply_fallback(H.format("```python\nx = 1\n```"), many).split("> **Profiler note:**", 1)[1]
	assert "`frappe.db.sql`" in note and "`frappe.db.commit()`" in note and "eval" not in note
	assert "And 2 more; see docs/AI-FIXING.md, Guardrail tiers." in note


def test_headings_alone_never_strip_code():
	text = "### Diagnosis\nx\n### Fix\n```python\nrows = frappe.get_all('X')\n```"
	vs = G.verify_fix(text, source_lines=[])
	assert [v.code for v in vs] == ["headings"] and [v.action for v in vs] == ["block"]
	assert G.reaskable(vs) and not G.strips_code(vs)
	assert "frappe.get_all('X')" in G.apply_fallback(text, vs)


# ---------------------------------------------------------------- code the report renders, anywhere (G1)
_BAD = 'frappe.db.sql("ALTER TABLE `tabX` ADD INDEX i (a)")\nfrappe.db.commit()'
_OK = "rows = frappe.get_all(\"Item\", filters={\"item_group\": group})\ntotal = len(rows)"


def _indent(code, n):
	return "\n".join(" " * n + ln for ln in code.splitlines())


_SHAPES = {
	"list-nested 4-space fence": lambda c: "**Diagnosis**: d\n**Fix**\n1. Replace the loop:\n\n    ```python\n"
	+ _indent(c, 4) + "\n    ```\n\n**Why it works**: w\n**Verify**: v",
	"list-nested fence, no blank line": lambda c: "**Diagnosis**: d\n**Fix**\n1. Run:\n    ```python\n"
	+ _indent(c, 4) + "\n    ```\n**Why it works**: w\n**Verify**: v",
	"indented code block": lambda c: "**Diagnosis**: d\n**Fix**\nRun this once:\n\n" + _indent(c, 4)
	+ "\n\n**Why it works**: w\n**Verify**: v",
	"fence in Verify": lambda c: "**Diagnosis**: d\n**Fix**: batch it.\n**Why it works**: w\n**Verify**:\n```python\n"
	+ c + "\n```",
	"tilde fence": lambda c: "**Diagnosis**: d\n**Fix**\n~~~python\n" + c + "\n~~~\n**Why it works**: w\n**Verify**: v",
	"4-backtick fence": lambda c: "**Diagnosis**: d\n**Fix**\n````python\n" + c + "\n````\n**Why it works**: w\n**Verify**: v",
	# R-RENDERED (cycle 3): shapes the grammar alone missed; PR-0c's renderer is the reference.
	"indented run right after a fence closer": lambda c: "**Diagnosis**: d\n**Fix**\n```python\nrows = []\n```\n"
	+ _indent(c, 4) + "\n\n**Why it works**: w\n**Verify**: v",
	"indented run after an ATX heading": lambda c: "**Diagnosis**: d\n**Fix**\n### Apply\n" + _indent(c, 4)
	+ "\n\n**Why it works**: w\n**Verify**: v",
	"indented run after ---": lambda c: "**Diagnosis**: d\n**Fix**\nUse it.\n\n---\n" + _indent(c, 4)
	+ "\n\n**Why it works**: w\n**Verify**: v",
	"indented run after a setext heading": lambda c: "**Diagnosis**: d\n**Fix**\nApply\n=====\n" + _indent(c, 4)
	+ "\n\n**Why it works**: w\n**Verify**: v",
	"fence inside a blockquote": lambda c: "**Diagnosis**: d\n**Fix**\n> ```python\n"
	+ "\n".join("> " + ln for ln in c.splitlines()) + "\n> ```\n\n**Why it works**: w\n**Verify**: v",
	"indented block inside a blockquote": lambda c: "**Diagnosis**: d\n**Fix**\n> quote\n>\n"
	+ "\n".join(">     " + ln for ln in c.splitlines()) + "\n\n**Why it works**: w\n**Verify**: v",
	"raw HTML pre": lambda c: "**Diagnosis**: d\n**Fix**\nUse it.\n\n<pre>" + c + "</pre>\n\n**Why it works**: w\n**Verify**: v",
	"raw HTML pre code": lambda c: "**Diagnosis**: d\n**Fix**\nUse it.\n\n<pre><code>" + c
	+ "</code></pre>\n\n**Why it works**: w\n**Verify**: v",
	"raw HTML details pre": lambda c: "**Diagnosis**: d\n**Fix**\n<details><pre>" + c
	+ "</pre></details>\n\n**Why it works**: w\n**Verify**: v",
}


def _pre_blocks(html):
	return re.findall(r"<pre[^>]*>(.*?)</pre>", html, re.S)


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_hostile_code_anywhere_is_caught_and_never_rendered(shape):
	text = _SHAPES[shape](_BAD)
	vs = G.verify_fix(text, source_lines=[])
	assert {"raw-ddl", "manual-commit"} <= {v.code for v in vs}, shape
	assert G.strips_code(vs), shape  # suggest_fix stores this as guardrail.fallback
	out = G.apply_fallback(text, vs)
	assert _pre_blocks(_markdown_to_safe_html(out)) == [], shape  # R-RENDERED: 0 <pre> left
	body = out.split("> **Profiler note:**")[0]
	assert "ALTER TABLE" not in body and "frappe.db.commit()" not in body, shape  # removed, not demoted


def test_grammar_locates_nested_fences_and_indented_runs():
	# The grammar is the locator strip_code uses; the renderer check is the backstop.
	assert [b.info for b in G.code_blocks(_SHAPES["list-nested 4-space fence"](_BAD))] == ["python"]
	assert [b.fenced for b in G.code_blocks(_SHAPES["indented code block"](_BAD))] == [False]


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_clean_code_anywhere_keeps_its_code(shape):
	text = _SHAPES[shape](_OK)
	vs = G.verify_fix(text, source_lines=[])
	assert vs == [] and not G.strips_code(vs), (shape, vs)
	out = G.apply_fallback(text, vs)
	assert out == text
	if shape != "tilde fence":  # markdown2 renders ~~~ as a paragraph, not as <pre>
		# ("frappe.get", not "get_all": markdown2 still applies emphasis inside raw HTML <pre>)
		assert any("frappe.get" in pre for pre in _pre_blocks(_markdown_to_safe_html(out))), shape


def test_list_continuation_prose_is_not_code():
	# A 4-space continuation paragraph of a list renders as a paragraph, not <pre>:
	# prose that mentions frappe.db.commit() must not raise manual-commit (R-RENDERED).
	text = ("**Diagnosis**: d\n**Fix**: batch the reads.\n**Why it works**\n\n1. One query replaces N.\n\n"
		"    Frappe commits at the end of the request, so no frappe.db.commit() call is needed here.\n\n"
		"2. Fewer round trips.\n\n**Verify**: v")
	assert G.verify_fix(text, source_lines=[]) == []


@pytest.mark.parametrize("info", [
	"bash", "shell", "console", "sh", "text", "sqlite", "tsql", "plsql", "postgresql", "pgsql", "json",
])
def test_ddl_in_any_fence_is_raw_ddl(info):
	body = 'ALTER TABLE `tabX` ADD INDEX idx_a (a);' if info != "json" else '{"sql": "ALTER TABLE tabX ADD INDEX idx_a (a)"}'
	assert "raw-ddl" in codes(H.format(f"```{info}\n{body}\n```")), info


def test_ddl_through_bench_mariadb_is_raw_ddl():
	cmd = 'bench --site x mariadb -e "ALTER TABLE `tabSales Invoice` ADD INDEX idx_c (customer)"'
	assert "raw-ddl" in codes(H.format("```bash\n" + cmd + "\n```"))


def test_moved_lines_are_not_introduced():
	# R-INTRODUCED: a line whose text is among the diff's - lines is moved, not introduced,
	# so a moved frappe.throw('x') or frappe.db.commit() raises nothing.
	src = ["for d in docs:", "    frappe.throw('x')", "    frappe.db.commit()"]
	diff = ("```diff\n-for d in docs:\n-    frappe.throw('x')\n-    frappe.db.commit()\n"
		"+frappe.throw('x')\n+frappe.db.commit()\n+for d in docs:\n+    pass\n```")
	assert codes(H.format(diff), src) == set()
	assert codes(H.format("```python\nfrappe.throw('x')\n```"), src) == set()  # verbatim from the window


def test_quoted_source_in_diagnosis_is_context_not_new_code():
	src = ['    frappe.db.sql("select name from `tabUser`")', "    frappe.db.commit()"]
	text = ("**Diagnosis**: the loop runs\n```python\n" + "\n".join(ln.strip() for ln in src) + "\n```\n"
		"**Fix**\n```diff\n-" + src[0] + "\n+    rows = frappe.get_all(\"User\")\n```\n**Why it works**: w\n**Verify**: v")
	assert G.verify_fix(text, source_lines=src) == []


def test_customize_form_is_a_note_never_a_reask():
	vs = G.verify_fix(H.format("Tick Search Index on the field in Customize Form."), source_lines=[])
	assert [(v.code, v.action) for v in vs] == [("customize-form-index", "note")]


# ---------------------------------------------------------------- grounding (A1)
def test_grounding_and_no_source():
	assert "ungrounded" in codes(H.format("```diff\n-x = 1\n+x = 2\n```"), ["y = 1"])
	assert "no-source-diff" in codes(H.format("```diff\n-x = 1\n+x = 2\n```"))
	assert codes(H.format("```diff\n+frappe.db.add_index('SI', ['customer'])\n```")) == set()  # +only ok
	assert codes(H.format("```diff\n-  280:     x = 1\n+    x = 2\n```"), ["    x = 1"]) == set()  # lineno copied


def test_verbatim_hoist_of_preexisting_fstring_sql_not_blamed():
	line = '    x = frappe.db.sql(f"select 1 from `tab{dt}`")'
	src = ["for i in r:", line]
	hoist = "```diff\n-for i in r:\n-" + line + "\n+" + line.strip() + "\n+for i in r:\n+    pass\n```"
	assert codes(H.format(hoist), src) == set()


# ---------------------------------------------------------------- structure
def test_headings_line_anchored_and_ordered():
	assert "headings" in codes("**Diagnosis**: see the **Fix** below\n**Why it works**: y\n**Verify**: z")
	assert "headings" in codes("**Fix**: a\n**Diagnosis**: b\n**Why it works**: y\n**Verify**: z")
	assert codes("**Diagnosis:** a\n**Fix:** b\n**Why it works:** y\n**Verify:** z") == set()


def test_long_fence_owns_inner_short_fence():
	assert "raw-sql" in codes(H.format("````python\nx = 1\n```\nr = frappe.db.sql(q)\n````"))


# ---------------------------------------------------------------- truncation (A6)
def test_truncation_never_reasks_and_fallback_strips():
	vs = G.verify_fix("**Diagnosis**: x\n**Fix**\n```diff\n+r = frappe.get_all('X'", source_lines=[])
	assert [v.action for v in vs] == ["truncated"]
	out = G.apply_fallback(
		"**Diagnosis**: x\n**Fix**\n```diff\n+r = 1\n```\n**Why it works**: y\n**Verify**: z",
		[G.Violation("raw-sql")],
	)
	assert "```" not in out and "Profiler note" in out


def test_finish_reason_length_is_truncated_not_reask():
	text = "**Diagnosis**: x\n**Fix**: y\n**Why it works**: y\n**Verify**: z"
	vs = G.verify_fix(text, source_lines=[], finish_reason="length")
	assert [(v.code, v.action) for v in vs] == [("truncated", "truncated")]
	vs = G.verify_fix(text, source_lines=[], finish_reason="max_tokens")
	assert [v.code for v in vs] == ["truncated"]


# ---------------------------------------------------------------- risky constructs (re-asked, then stripped)
_RISKY = (
	("data = pickle.loads(blob)", "unsafe-deserialize"),
	("data = marshal.loads(blob)", "unsafe-deserialize"),
	("subprocess.run(['ls'])", "shell-exec"),
	("os.system('ls')", "shell-exec"),
	("mod = __import__(name)", "dynamic-import"),
	("mod = importlib.import_module(name)", "dynamic-import"),
	("frappe.set_user('Administrator')", "set-user-admin"),
)


@pytest.mark.parametrize("snippet,code", _RISKY)
def test_risky_code_constructs_are_flagged(snippet, code):
	# pickle / marshal loads, shell commands and set_user("Administrator") are block;
	# a dynamic import is advise (a note, the code is kept).
	vs = G.verify_fix(H.format("```python\n" + snippet + "\n```"), source_lines=[])
	want = "advise" if code == "dynamic-import" else "block"
	assert [(v.code, v.action) for v in vs] == [(code, want)], snippet


def test_risky_code_is_named_in_the_reask_and_stripped_by_the_fallback():
	text = H.format("```python\nrows = frappe.get_all('X')\ndata = pickle.loads(blob)\n```")
	vs = G.verify_fix(text, source_lines=[])
	assert "`pickle`" in G.reask_message(vs)
	out = G.apply_fallback(text, vs)
	body, note = out.split("> **Profiler note:**")
	assert "pickle" not in body and "```" not in body
	assert "Do not deserialise with `pickle`" in note  # the rule in plain English


def test_risky_calls_that_only_move_are_not_blamed():
	# A verbatim move of pre-existing code is not introduced by the fix.
	line = "    obj = pickle.loads(blob)"
	hoist = "```diff\n-for b in blobs:\n-" + line + "\n+obj = pickle.loads(blob)\n+for b in blobs:\n+    pass\n```"
	assert codes(H.format(hoist), ["for b in blobs:", line]) == set()


def test_set_user_to_another_user_is_not_flagged():
	assert codes(H.format("```python\nfrappe.set_user(doc.owner)\n```")) == set()


# ---------------------------------------------------------------- markup (notes)
def test_markup_notes_and_fallback_neutralises_them():
	text = H.format(
		"See ![chart](https://evil.example/x.png) and [docs](https://docs.frappe.io/framework) "
		"and [manual](https://docs.erpnext.com/docs/user/manual) "
		"and [this](https://evil.example/p) and <https://evil.example/q>. <data-a1b2c3 kind=\"sql\">x</data-a1b2c3>"
	)
	vs = G.verify_fix(text, source_lines=[])
	assert {v.code for v in vs} == {"markdown-image", "external-link", "echoed-data-tag"}
	assert all(v.action == "note" for v in vs)
	out = G.apply_fallback(text, vs)
	body = out.split("> **Profiler note:**")[0]
	assert "evil.example/x.png" not in body and "<data-" not in out  # image and echoed tag removed
	html = _markdown_to_safe_html(out)  # PR-0c drops the off-allowlist hrefs when it renders
	assert "evil.example" not in "".join(re.findall(r'href="([^"]*)"', html))
	assert 'href="https://docs.frappe.io/framework"' in html
	assert 'href="https://docs.erpnext.com/docs/user/manual"' in html
	assert out.count("> **Profiler note:**") == 3


def test_the_link_pattern_has_only_bounded_repeats():
	# The reply is capped only by the max_tokens a provider honours: a server that
	# ignores it can return any size. An unbounded repeat in the link pattern made
	# unclosed "[...](" text rescan the rest of the reply from every "[" (60,000
	# characters took about 35 s). Every repeat must have an upper bound.
	import re._parser as parser

	def unbounded(items):
		for op, arg in items:
			if op in (parser.MAX_REPEAT, parser.MIN_REPEAT, parser.POSSESSIVE_REPEAT):
				if arg[1] == parser.MAXREPEAT:
					return True
				if unbounded(arg[2]):
					return True
			elif op == parser.SUBPATTERN and unbounded(arg[3]):
				return True
			elif op == parser.BRANCH and any(unbounded(branch) for branch in arg[1]):
				return True
		return False

	assert not unbounded(parser.parse(G._MD_LINK.pattern))


def test_a_reply_of_unclosed_links_is_checked_quickly():
	import time

	for hostile in (
		"[" * 10_000 + "](" * 10_000,  # link texts that never close
		("[x](" + "a" * 96) * 2_000,  # destinations that never close
	):
		start = time.perf_counter()
		G.check_markup(hostile)
		G.apply_fallback(hostile, [G.Violation("markdown-image")])
		assert time.perf_counter() - start < 10


def test_a_long_link_and_a_long_image_are_still_noted():
	url = "https://evil.example/" + "a" * 1900
	vs = G.check_markup(f"see [{'t' * 900}]({url}) and ![{'a' * 900}]({url}.png)")
	assert {v.code for v in vs} == {"external-link", "markdown-image"}
	# whitespace, a line break included, around the destination and a title still match
	vs = G.check_markup('see [x](\n  https://evil.example/p "t"\n) and ![i]( https://evil.example/i.png )')
	assert {v.code for v in vs} == {"external-link", "markdown-image"}


def test_entity_encoded_allowed_link_is_not_flagged():
	# The renderer decodes entities before PR-0c's matcher sees the href; so does the note.
	assert "external-link" not in codes(H.format("See [docs](https://docs.frappe.io&#47;framework)."))


def test_note_codes_never_strip_code():
	text = H.format("```python\nrows = frappe.get_all('X')\n```\nSee ![chart](https://evil.example/x.png).")
	out = G.apply_fallback(text, G.verify_fix(text, source_lines=[]))
	assert "```python" in out and "frappe.get_all('X')" in out
	assert "evil.example" not in out


# ---------------------------------------------------------------- contract
def test_every_emitted_code_is_registered():
	src = open(G.__file__).read()
	emitted = set(re.findall(r'_v\(\s*"([a-z-]+)"', src))
	assert emitted <= G.CODES, emitted - G.CODES
	assert emitted | {"context-truncated"} == G.CODES


def test_actions_are_the_four_known_values():
	assert set(G.CODE_ACTIONS.values()) == {"block", "truncated", "advise", "note"}


def test_reask_message_lists_only_block_rules_with_detail():
	msg = G.reask_message(
		[
			G.Violation("ungrounded", "`x = 1`"),
			G.Violation("raw-sql"),
			G.Violation("dynamic-import", "__import__"),
			G.Violation("markdown-image"),
		]
	)
	assert msg.startswith(ai_prompts.REASK_HEADER) and msg.endswith(ai_prompts.REASK_FOOTER)
	assert "`x = 1`" in msg
	assert "()" not in msg  # an empty detail leaves no empty parentheses
	assert "image" not in msg and "normal import" not in msg  # advise and note rules are never sent
	assert msg.count("\n- ") == 2


def test_rule_text_braces_are_not_format_fields():
	# RULE_TEXT is rendered with str.replace, so literal braces in rule text are safe.
	msg = G.reask_message([G.Violation("modify-not-saved", "on_submit")])
	assert "`on_submit`" in msg


@pytest.mark.parametrize("case", CORPUS, ids=[c["name"] for c in CORPUS])
def test_verify_fix_is_fast(case):
	import time

	t = time.perf_counter()
	G.verify_fix(case["suggestion"], source_lines=case["source_lines"])
	assert time.perf_counter() - t < 0.05


# Whole-line and multiline rules must agree with the eval scanner.
_PARITY = [
    ("moved", ["if bad:", "    frappe.throw('Missing')"],
     "```diff\n-if bad:\n-    frappe.throw('Missing')\n+frappe.throw('Missing')\n```", set()),
    ("reshaped", ["if bad: frappe.throw('Missing')"],
     "```diff\n-if bad: frappe.throw('Missing')\n+frappe.throw('Missing')\n```", {"untranslated"}),
    ("quoted", ["frappe.throw('Missing')"],
     "```python\nfrappe.throw('Missing')\n```", set()),
    ("sql-kept", ["frappe.db.sql(", "    query, values", ")"],
     '```diff\n frappe.db.sql(\n-    query, values\n+    f"select {name}"\n )\n```',
     {"sql-format-injection", "raw-sql"}),
    ("sql-quoted", ["frappe.db.sql(", "    query, values", ")"],
     '```python\nfrappe.db.sql(\n    f"select {name}"\n)\n```',
     {"sql-format-injection", "raw-sql"}),
    ("sql-parameters", ["frappe.db.sql(", '    f"select {name}"', ")"],
     '```diff\n frappe.db.sql(\n-    f"select {name}"\n+    "select %(name)s", {"name": name}\n )\n```', set()),
    ("enqueue-kept", ["def on_submit(self):", "    frappe.enqueue(", '        "app.job", enqueue_after_commit=True', "    )"],
     '```diff\n def on_submit(self):\n     frappe.enqueue(\n-        "app.job", enqueue_after_commit=True\n+        "app.job"\n     )\n```',
     {"enqueue-without-after-commit"}),
]


@pytest.mark.parametrize("name,source,body,expected", _PARITY, ids=[c[0] for c in _PARITY])
def test_introduced_lines_and_node_spans(name, source, body, expected):
    assert codes(H.format(body), source) == expected
    units = load("_semgrep").units(H.format(body), source)
    assert bool(units) == (name not in {"moved", "quoted"})


@pytest.mark.semgrep
def test_introduced_parity_with_pinned_semgrep():
    from optimus.tests.ai_eval_support import require_semgrep

    texts = {name: H.format(body) for name, source, body, expected in _PARITY}
    sources = {name: source for name, source, body, expected in _PARITY}
    result = load("_semgrep").scan_texts(texts, require_semgrep(), sources)
    for name, source, _body, expected in _PARITY:
        assert result[name].unparsed == 0
        assert bool(result[name].hits) == bool(expected), name
        assert codes(texts[name], source) == expected
