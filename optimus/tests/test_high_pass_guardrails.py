"""Real suggestion gates: fluent QB reads, malformed fragments and Frappe aliases."""

import json

import pytest

from optimus import ai_guardrails as G
from optimus import ai_prompts as P
from optimus.tests.test_ai_guardrail_gaps import answer, violations


@pytest.mark.parametrize("update", [
	"q = q.where(T.enabled == 1)",
	"if enabled:\n    q = q.where(T.enabled == 1)",
	"for condition in conditions:\n    q = q.where(condition)",
])
def test_normal_fluent_read_is_visible_and_saved_as_current(update):
	code = "q = frappe.qb.from_(T).select(T.name)\n" + update + "\nq.run(as_dict=True)"
	vs = violations(["old_work()"], code.splitlines())
	assert not G.strips_code(vs), [v.code for v in vs]
	stored = {"suggestion": answer(["old_work()"], code.splitlines()),
		"prompt_version": P.PROMPT_VERSION, "completion_status": "complete"}
	assert P.fix_state(json.dumps(stored))[0] == "current"


def test_conditional_where_does_not_prove_a_write_bounded():
	code = "q = frappe.qb.update(T).set(T.enabled, 0)\nif enabled:\n    q = q.where(T.name == name)\nq.run()"
	assert "unbounded-write" in {v.code for v in violations([], code.splitlines())}


@pytest.mark.parametrize("code", [
	"q = frappe.qb.from_(T).select(T.name)\nq = other()\nq.run()",
	"q = frappe.qb.from_(T).select(T.name)\nif enabled:\n    q = q.delete()\nq.run()",
	"a = frappe.qb.from_(T).select(T.name)\na = b\nb = a.where(T.name == name)\na.run()",
	"q = frappe.qb.from_(T).select(T.name)\n" + "q = q.where(T.name == name)\n" * 17 + "q.run()",
	"q = frappe.qb.get_query('User', delete=True)\nq.run()",
	"q = frappe.qb.get_query('User', update=True)\nq.run()",
])
def test_unproven_builder_variants_stay_withheld(code):
	assert G.strips_code(violations([], code.splitlines()))


@pytest.mark.parametrize("sink", [
	'eval("payload")', 'os.system("payload")', 'subprocess.run("payload", shell=True)',
	'pickle.loads(data)', 'doc.save(ignore_permissions=True)', 'frappe.set_user("Administrator")',
])
@pytest.mark.parametrize("shape", ["{}\n# trailing comment:", "7: {}", "{}\ninvalid !!! line"])
def test_broken_python_never_skips_the_safety_gate(sink, shape):
	code = shape.format(sink)
	assert G.strips_code(violations([], code.splitlines()))


def test_safe_trailing_comment_colon_does_not_make_code_unparseable():
	assert not G.strips_code(violations([], ["print('hello')", "# trailing comment:"]))


@pytest.mark.parametrize("code,expected", [
	("from frappe.utils.safe_exec import safe_exec as run\nrun(code)", "eval-exec"),
	("frappe.utils.safe_exec.safe_exec(code)", "eval-exec"),
	("from frappe.utils.background_jobs import enqueue as queue\nqueue('example.run')", "enqueue-without-after-commit"),
	("from frappe.database.database import Database\nDatabase.commit(db)", "manual-commit"),
	("from frappe import whitelist as allow\n@allow()\ndef run(value):\n    return value", "whitelist-type-hints"),
	("from frappe import throw as deny\ndeny('Permission denied')", "untranslated"),
	("doc.flags.ignore_permissions = True", "ignore-permissions"),
	("doc.insert(True)", "ignore-permissions"),
	("frappe.session.user = 'Administrator'", "set-user-admin"),
])
def test_policy_shaped_frappe_aliases_and_assignments_are_checked(code, expected):
	assert expected in {v.code for v in violations([], code.splitlines())}


def test_get_all_cannot_replace_permission_checked_get_list():
	before = ['rows = frappe.get_list("User", fields=["name"])']
	after = ['rows = frappe.get_all("User", fields=["name"])']
	assert G.strips_code(violations(before, after))


def test_conditional_write_cannot_hide_behind_unchanged_run():
 before = ["q = frappe.qb.update(T).set(T.enabled, 0).where(T.name == name)", "q.run()"]
 after = ["q = frappe.qb.update(T).set(T.enabled, 0)", "if enabled:", "    q = q.where(T.name == name)", "q.run()"]
 assert G.strips_code(violations(before, after))

@pytest.mark.parametrize("code", ["frappe.safe_eval(code)", "frappe.utils.safe_eval(code)", "from frappe import safe_eval as run\nrun(code)", "frappe.local.db.commit()"])
def test_qualified_evaluation_and_local_db_are_blocked(code):
 assert G.strips_code(violations([], code.splitlines()))
