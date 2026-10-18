"""Observable safety and partial-improvement regressions; never execute proposed code."""

import copy
import json

import pytest

from optimus import ai_guardrails as G
from optimus import ai_prompts as P

H = "**Diagnosis**: repeated work.\n**Fix**\n{}\n**Why it works**: less work.\n**Verify**: re-profile."


def answer(before, after):
	return H.format("```diff\n" + "\n".join(["-" + s for s in before] + ["+" + s for s in after]) + "\n```")


def violations(before, after):
	return G.verify_fix(answer(before, after), source_lines=before, finish_reason="stop")


PERMISSION_BEFORE = [
	"def _verify_permissions(doc):", "    for _ in range(120):",
	'        frappe.has_permission("User", "read", doc=frappe.session.user)',
	'        frappe.has_permission("User", "write", doc=frappe.session.user)',
]
PERMISSION_AFTER = [
	"def _verify_permissions(doc):",
	'    frappe.has_permission("User", "read", doc=frappe.session.user, throw=True)',
	'    frappe.has_permission("User", "write", doc=frappe.session.user, throw=True)',
]


@pytest.mark.parametrize("call", [
	'frappe.db.delete("ToDo")', 'frappe.db.delete("ToDo", {})',
	'frappe.db.delete(doctype="ToDo", filters=None)', 'frappe.db.delete("ToDo", [])',
	'frappe.db.delete("ToDo", "")', 'frappe.db.delete("ToDo", dict())',
	'frappe.db.delete("ToDo", False)', 'frappe.db.delete("ToDo", 0)',
	'frappe.db.set_value("ToDo", {}, "status", "Closed")',
	'frappe.db.set_value(dt="ToDo", dn=[], field="status", val="Closed")',
	'frappe.db.truncate("ToDo")',
	'frappe.qb.update(T).set(T.status, "Closed").run()',
	'frappe.qb.from_(T).delete().run()',
])
def test_unbounded_writes_are_withheld(call):
	vs = violations(["old_work()"], call.splitlines())
	assert "unbounded-write" in {v.code for v in vs}
	assert G.strips_code(vs)


@pytest.mark.parametrize("call", [
	'frappe.db.delete("ToDo", {"status": "Closed"})',
	'frappe.db.delete("ToDo", filters)',
	'frappe.db.set_value("ToDo", name, "status", "Closed")',
	'frappe.db.set_value("ToDo", {"name": ("in", names)}, "status", "Closed")',
	'frappe.qb.from_(T).delete().where(T.name == name).run()',
	'message = \'frappe.db.delete("ToDo", {})\'',
])
def test_filtered_writes_and_literal_mentions_are_allowed(call):
	assert not violations(["old_work()"], [call])


@pytest.mark.parametrize("throw", ["False", "0", "None", "flag"])
def test_discarded_nonthrowing_permission_result_is_rejected(throw):
	assert "unchecked-permission" in {v.code for v in violations([], [f'frappe.has_permission("User", throw={throw})'])}


def test_saved_permission_change_is_rejected_without_source_and_without_mutation():
	text = answer(PERMISSION_BEFORE, PERMISSION_AFTER)
	fix = dict(suggestion=text, prompt_version=P.PROMPT_VERSION, completion_status="complete",
	           received_responses=[{"text": text}], tokens={"total_tokens": 2474})
	before = copy.deepcopy(fix)
	assert "permission-behavior-change" in {v.code for v in violations(PERMISSION_BEFORE, PERMISSION_AFTER)}
	assert P.fix_state(json.dumps(fix))[0] == "outdated"
	assert fix == before


def test_discarded_permission_check_can_be_moved_verbatim():
	after = [PERMISSION_BEFORE[0], *[line[4:] for line in PERMISSION_BEFORE[2:]]]
	assert not violations(PERMISSION_BEFORE, after)


@pytest.mark.parametrize("header", ["for row in rows:", "if private:", "try:"])
def test_enforced_check_cannot_be_moved_outside_execution_boundary(header):
	call = 'frappe.has_permission("User", throw=True)'
	before = [header, "    " + call]
	if header == "try:":
		before += ["except Exception:", "    pass"]
	assert "permission-behavior-change" in {v.code for v in violations(before, [call])}


def test_cheaper_read_still_in_loop_is_advice_without_code_removal():
	before = ['for name in names:', '    rows.append(frappe.get_doc("User", name).email)']
	after = ['for name in names:', '    rows.append(frappe.db.get_value("User", name, "email"))']
	vs = violations(before, after)
	assert any(v.code == "loop-query" and v.action == G.ADVISE for v in vs)
	assert not G.reaskable(vs) and not G.strips_code(vs)
	out = G.apply_fallback(answer(before, after), vs)
	assert "```diff" in out and "Profiler note" in out


@pytest.mark.parametrize("after", [
	'rows = frappe.get_all("User", fields=["name"])',
	'rows = frappe.get_all("User", fields=["name"], limit=0)',
	'rows = frappe.get_all("User", fields=["name"], limit=50)',
])
def test_dropped_or_changed_literal_limit_is_blocked(after):
	before = ['rows = frappe.db.sql("SELECT name FROM `tabUser` LIMIT 20", as_dict=True)']
	assert "sql-semantics-dropped" in {v.code for v in violations(before, [after])}


def test_equivalent_explicit_limit_is_allowed():
	before = ['rows = frappe.db.sql("SELECT name FROM `tabUser` LIMIT 20", as_dict=True)']
	after = ['rows = frappe.get_all("User", fields=["name"], limit=20, order_by="")']
	assert not violations(before, after)


@pytest.mark.parametrize("before,after", [
	(["db = frappe.db", "old_work()"], ["db = frappe.db", 'db.delete("ToDo", {})']),
	(["old_work()"], ['db = frappe.db; db.delete("ToDo", {})']),
	(["old_work()"], ['from frappe import db', 'db.delete("ToDo", {})']),
	(["else:", "    old_work()"], ["else:", '    frappe.db.delete("ToDo", {})']),
	(['if enabled:', '    frappe.db.delete("ToDo", {})'], ['frappe.db.delete("ToDo", {})']),
	(["old_work()"], ['frappe.db.delete(', '    "ToDo",', '    {},', ')']),
])
def test_write_aliases_fragments_and_moves_are_checked(before, after):
	text = answer(before, after)
	assert "unbounded-write" in {v.code for v in violations(before, after)}
	assert P.fix_state(dict(suggestion=text, prompt_version=4))[0] == "outdated"


def test_alias_binding_cannot_leak_into_an_unrelated_function():
	after = ['def first():', '    db = frappe.db', '    return db',
	         'def second(db):', '    db.delete("thing", {})']
	assert not violations([], after)


@pytest.mark.parametrize("old,new", [
	('frappe.has_permission("User", "read", doc, user, False)', 'frappe.has_permission("User", "read", doc, user, True)'),
	('frappe.has_permission("User", doc=doc, throw=True)', 'frappe.has_permission("User", throw=True)'),
	('frappe.has_permission("User", "write", throw=True)', 'frappe.has_permission("User", "read", throw=True)'),
	('frappe.has_permission("User")', 'ok = frappe.has_permission("User")'),
])
def test_permission_argument_and_result_use_changes_are_blocked(old, new):
	assert "permission-behavior-change" in {v.code for v in violations([old], [new])}


@pytest.mark.parametrize("call", [
	'frappe.has_permission("User", "read", doc, user, True)',
	'frappe.has_permission("User", throw=1)',
	'ok = frappe.has_permission("User", throw=False)',
	'if frappe.has_permission("User"):\n    use_access()',
])
def test_permission_arguments_match_frappe_signature(call):
	assert not violations([], call.splitlines())


def test_partial_permission_removal_is_blocked():
	before = ['frappe.has_permission("User", "read", throw=True)', 'frappe.has_permission("User", "write", throw=True)']
	assert "permission-behavior-change" in {v.code for v in violations(before, before[:1])}
	before = ['frappe.has_permission("User", doc=first, throw=True)', 'frappe.has_permission("User", doc=second, throw=True)']
	assert "permission-behavior-change" in {v.code for v in violations(before, before[:1])}


def test_changed_execution_condition_checks_unchanged_permission_call():
	before = ['if access:', '    frappe.has_permission("User", throw=True)']
	text = H.format('```diff\n-if access:\n+if True:\n     frappe.has_permission("User", throw=True)\n```')
	assert "permission-behavior-change" in {v.code for v in G.verify_fix(text, source_lines=before)}
	assert "permission-behavior-change" in {v.code for v in G.stored_policy_violations(text)}


@pytest.mark.parametrize("after", [
	['rows = frappe.get_all("User", filters={"name": ("in", names)})', 'for name in names:', '    use(rows)'],
	['for name in names:', '    email = frappe.get_cached_value("User", name, "email")'],
	['for name in names:', '    if name not in memo:', '        memo[name] = frappe.db.get_value("User", name, "email")'],
])
def test_batched_cached_and_memoized_improvements_are_not_flagged(after):
	before = ['for name in names:', '    doc = frappe.get_doc("User", name)']
	assert not violations(before, after)


@pytest.mark.parametrize("query", [
	"SELECT 'LIMIT 20' FROM `tabUser`",
	"SELECT name FROM `tabUser` /* LIMIT 20 */",
	"SELECT name FROM (SELECT name FROM `tabUser` LIMIT 20) u",
])
def test_limit_in_literal_comment_or_subquery_is_not_an_outer_limit(query):
	before = [f'rows = frappe.db.sql({query!r}, as_dict=True)']
	assert not violations(before, ['rows = frappe.get_all("User", fields=["name"])'])


def test_unrelated_queries_are_not_paired():
	before = ['a = frappe.db.sql("SELECT name FROM `tabUser` LIMIT 20")', 'b = frappe.db.sql("SELECT name FROM `tabToDo`")']
	after = ['a = frappe.get_all("User")', 'b = frappe.get_all("ToDo")']
	assert not violations(before, after)


@pytest.mark.parametrize("query,extra", [
	('SELECT name FROM `tabUser` LIMIT 20 OFFSET 5', 'limit=20'),
	('SELECT name FROM `tabUser` ORDER BY name LIMIT 20', 'limit=20'),
	('SELECT name FROM `tabUser` ORDER BY name', 'order_by=""'),
])
def test_visible_offset_and_ordering_loss_is_blocked(query, extra):
	before = [f'rows = frappe.db.sql({query!r}, as_dict=True)']
	assert "sql-semantics-dropped" in {v.code for v in violations(before, [f'rows = frappe.get_all("User", fields=["name"], {extra})'])}


def test_prose_only_saved_answers_do_not_invoke_markdown_parser(monkeypatch):
	def forbidden(*args):
		raise AssertionError("no code to parse")
	monkeypatch.setattr(G, "rendered_blocks", forbidden)
	assert G.stored_policy_violations(H.format("Keep the existing limit and delete no rows.")) == []


@pytest.mark.parametrize("options", ["limit=None, limit_page_length=20", "limit_page_length=0, page_length=20"])
def test_null_limit_uses_explicit_legacy_limit(options):
	before = ['rows = frappe.db.sql("SELECT name FROM `tabUser` LIMIT 20")']
	after = [f'rows = frappe.get_all("User", {options})']
	assert not violations(before, after)


def test_unchanged_context_and_removed_destructive_code_remain_allowed():
	for diff in [' frappe.db.delete("ToDo", {})\n-old_work()\n+new_work()', '-frappe.db.delete("ToDo", {})']:
		text = H.format("```diff\n" + diff + "\n```")
		assert not G.verify_fix(text, source_lines=['frappe.db.delete("ToDo", {})', 'old_work()'])
		assert G.stored_policy_violations(text) == []


def test_saved_answer_render_and_refresh_preserve_private_original():
	from optimus import renderer
	from optimus.tests.test_ai_selection_contracts import pick, row
	from optimus.tests.test_finding_card_ai_fix import _doc, _finding

	text = answer(PERMISSION_BEFORE, PERMISSION_AFTER)
	fix = dict(suggestion=text, prompt_version=4, completion_status="complete",
	           received_responses=[{"text": "private-received-sentinel"}], tokens={"total_tokens": 2474})
	finding = _finding(llm_fix=fix)
	before = finding.llm_fix_json
	for render in (renderer.render_raw, renderer.render):
		html = render(_doc([finding]), [])
		assert "Safety review needed" in html
		assert "changes permission behavior" in html
		assert "private-received-sentinel" not in html
		assert "Why it works" not in html
		assert "throw=True" not in html
		assert finding.llm_fix_json == before
	rows = [row("affected", before), row("good", json.dumps(dict(suggestion=H.format("Keep the existing index."), prompt_version=4)))]
	assert pick(rows) == ["affected"]
	assert pick(rows, include_outdated=False) == []


@pytest.mark.parametrize("status,expected", [("incomplete", "incomplete"), ("unknown", "incomplete"), ("blocked", "blocked"), ("processing_limited", "processing_limited")])
def test_completion_status_wins_over_new_saved_checks(status, expected):
	fix = dict(suggestion=answer(PERMISSION_BEFORE, PERMISSION_AFTER), prompt_version=4, completion_status=status)
	assert P.fix_state(fix)[0] == expected


@pytest.mark.parametrize("repaired", [False, True])
def test_permission_repair_is_single_and_keeps_both_paid_answers(monkeypatch, repaired):
	from optimus.ai_fix import Usage
	from optimus.tests.test_ai_fix import TestGuardedCompletion, _post_sequence

	bad = answer(PERMISSION_BEFORE, PERMISSION_AFTER)
	good = answer(PERMISSION_BEFORE, [PERMISSION_BEFORE[0], *[line[4:] for line in PERMISSION_BEFORE[2:]]])
	runner = TestGuardedCompletion()
	runner._FINDING = {**runner._FINDING, "source_window": [
		{"lineno": 202 + i, "content": line, "is_target": i == 2} for i, line in enumerate(PERMISSION_BEFORE)]}
	usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
	reply = good if repaired else bad
	fake = _post_sequence(runner._resp(bad, usage=usage), runner._resp(reply, usage=usage))
	totals = Usage()
	result = runner._run(fake, monkeypatch, usage_out=totals)
	assert len(fake.calls) == 2 and totals["total_tokens"] == 300
	assert [r["text"] for r in result["received_responses"]] == [bad, reply]
	assert result["guardrail"]["fallback"] is not repaired


def test_partial_loop_improvement_does_not_spend_on_repair(monkeypatch):
	from optimus.tests.test_ai_fix import TestGuardedCompletion, _post_sequence
	before = ['for name in names:', '    rows.append(frappe.get_doc("User", name).email)']
	after = ['for name in names:', '    rows.append(frappe.db.get_value("User", name, "email"))']
	text = answer(before, after)
	runner = TestGuardedCompletion()
	runner._FINDING = {**runner._FINDING, "source_window": [
		{"lineno": 1 + i, "content": line, "is_target": i == 1} for i, line in enumerate(before)]}
	fake = _post_sequence(runner._resp(text))
	result = runner._run(fake, monkeypatch)
	assert len(fake.calls) == 1
	assert result["guardrail"]["fallback"] is False
	assert "```diff" in result["suggestion"] and "Profiler note" in result["suggestion"]
	assert P.fix_state(result)[0] == "current"


def test_claude_f1_all_permission_checks_removed_are_outdated():
	before = ['for row in rows:', '    frappe.has_permission("User", doc=row, throw=True)', '    process(row)']
	after = ['for row in rows:', '    process(row)']
	assert "permission-behavior-change" in {v.code for v in violations(before, after)}
	assert "permission-behavior-change" in {v.code for v in G.stored_policy_violations(answer(before, after))}


@pytest.mark.parametrize("denial", ['raise PermissionError()', 'return False', 'frappe.throw(_("Not permitted"), frappe.PermissionError)'])
def test_claude_f2_direct_denial_branch_cannot_be_removed(denial):
	header = 'if not frappe.has_permission("User", doc=doc):'
	before = [header, '    ' + denial]
	text = H.format(f'```diff\n {header}\n-    {denial}\n+    pass\n```')
	assert "permission-behavior-change" in {v.code for v in G.verify_fix(text, source_lines=before)}
	assert "permission-behavior-change" in {v.code for v in G.stored_policy_violations(text)}


@pytest.mark.parametrize("before,after", [
	(PERMISSION_BEFORE, PERMISSION_AFTER),
	(['for row in rows:', '    frappe.has_permission("User", throw=True)'], ['frappe.has_permission("User", throw=True)']),
	(['rows = frappe.db.sql("SELECT name FROM `tabUser` LIMIT 20")'], ['rows = frappe.get_all("User")']),
])
def test_claude_f3_nondiff_sensitive_replacements_require_comparison(before, after):
	text = H.format('```python\n' + '\n'.join(after) + '\n```')
	assert any(v.code == "unverified-safety" and v.detail == "show permission or SQL changes as a grounded diff"
	           for v in G.verify_fix(text, source_lines=before))


def test_claude_f4_saved_nondiff_permissions_have_no_before_evidence():
	text = H.format('```python\ndef validate(self):\n    frappe.has_permission(self.doctype, "write", self, throw=True)\n    <rest unchanged>\n```')
	assert G.stored_policy_violations(text) == []
	assert P.fix_state(dict(suggestion=text, prompt_version=4))[0] == "current"


@pytest.mark.parametrize("separator", ['\x0c', '\u2028'])
def test_claude_f5_special_literal_separators_do_not_break_rows(separator):
	before = ['else:', '    text = "' + separator + '"', '    frappe.has_permission("User", throw=True)']
	after = ['else:', '    text = "' + separator + '"', '    frappe.has_permission("User", throw=False)']
	assert G.strips_code(violations(before, after))
	assert P.fix_state(dict(suggestion=answer(before, after), prompt_version=4))[0] == "outdated"


def test_claude_f5_checker_exception_withholds_code_and_keeps_report_usable(monkeypatch, caplog):
	from optimus import ai_fix_checks, renderer
	from optimus.tests.test_finding_card_ai_fix import _doc, _finding
	def broken(*args, **kwargs):
		raise IndexError('malformed private source')
	monkeypatch.setattr(ai_fix_checks, 'View', broken)
	text = answer([], ['frappe.db.delete("ToDo", {"status": "Closed"})'])
	fix = dict(suggestion=text, prompt_version=4)
	assert "unverified-safety" in {v.code for v in G.verify_fix(text, source_lines=[])}
	assert P.fix_state(fix)[0] == "outdated"
	html = renderer.render_raw(_doc([_finding(llm_fix=fix)]), [])
	assert 'could not be checked safely' in html
	assert 'malformed private source' not in html
	assert 'AI safety checker failed: IndexError' in caplog.text
	assert 'malformed private source' not in caplog.text


@pytest.mark.parametrize("extra", ['parent_doctype="Parent"', 'ignore_share_permissions=True', 'debug=True'])
def test_claude_f6_extra_permission_keywords_are_preserved(extra):
	before = [f'frappe.has_permission("User", throw=True, {extra})']
	after = ['frappe.has_permission("User", throw=True)']
	assert "permission-behavior-change" in {v.code for v in violations(before, after)}


@pytest.mark.parametrize("call", ['frappe.has_permission(*args, throw=True)', 'frappe.has_permission("User", **options)'])
def test_claude_f6_dynamic_permission_arguments_need_explicit_evidence(call):
	assert any(v.code == "unverified-safety" and v.detail == "dynamic or unsupported permission arguments"
	           for v in violations([], [call]))


def test_claude_f7_repair_preserves_preexisting_write_as_context():
	assert "unchanged diff context; do not remove it" in P.RULE_TEXT['unbounded-write']


def test_repaired_frappe_fragment_keeps_code_but_notes_structure_limit():
	vs = violations(['else:', '    old_work()'], ['else:', '    frappe.db.delete("ToDo", {"name": name})'])
	assert {v.code for v in vs} == {"unverified-structure"}
	assert not G.strips_code(vs) and not G.reaskable(vs)


@pytest.mark.parametrize("language,body", [("sql", "ALTER TABLE tabUser ADD INDEX idx_name (name)"), ("bash", "bench version"), ("json", '{"result": 1}')])
def test_claude_n1_nonpython_blocks_do_not_require_permission_diff(language, body):
	text = H.format(f'```{language}\n{body}\n```')
	vs = G.verify_fix(text, source_lines=PERMISSION_BEFORE)
	assert "unverified-safety" not in {v.code for v in vs}
	# Existing raw-DDL policy still rejects SQL index commands independently.
	if language == "sql":
		assert "raw-ddl" in {v.code for v in vs}


def test_claude_n1_identical_python_context_does_not_require_diff():
	text = H.format('```python\n' + '\n'.join(PERMISSION_BEFORE) + '\n```')
	assert "unverified-safety" not in {v.code for v in G.verify_fix(text, source_lines=PERMISSION_BEFORE)}
