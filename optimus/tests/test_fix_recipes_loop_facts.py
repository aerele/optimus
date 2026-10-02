# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""fix_recipes.loop_facts over the lateral corpus windows (cases.json)."""

from optimus.renderer import fix_recipes as fr

# ugly_code/python/common.py:20-30 (cases 3q1nfc4d2l, 3q1efl686s)
CHECK_USER = [
	"def _check_user_exists(doc):",
	"    for i in range(150):",
	"        user = frappe.get_doc(\"User\", frappe.session.user)",
	"        roles = frappe.db.sql(",
	"            \"SELECT role FROM `tabHas Role` WHERE parent=%s\",",
	"            (frappe.session.user,),",
	"            as_dict=True,",
	"        )",
	"        if user.enabled:",
	"            for _r in roles:",
	"                pass",
]
# common.py:202-206 (cases 3q1qg8btng, 3q1fsdhl5p)
VERIFY_PERMS = [
	"def _verify_permissions(doc):",
	"    for _ in range(120):",
	"        frappe.has_permission(\"User\", \"read\", doc=frappe.session.user)",
	"        frappe.has_permission(\"User\", \"write\", doc=frappe.session.user)",
]
# common.py:275-284 cut inside the try block (case 3q1dsfrmti): exercises the repair path
RECHECK_CUT = [
	"def bg_recheck_users(doc_name=None):",
	"    for i in range(50):",
	"        try:",
	"            user = frappe.get_doc(\"User\", frappe.session.user)",
	"            frappe.db.sql(",
	"                \"SELECT role FROM `tabHas Role` WHERE parent=%s\",",
	"                (frappe.session.user,),",
	"            )",
	"            if user.enabled:",
]
# common.py:358-375 (case 3q1ln8bv2o), a while loop
LONG_JOB = [
	"    deadline = time.monotonic() + (target_minutes * 60)",
	"    iters = 0",
	"    while time.monotonic() < deadline:",
	"        iters += 1",
	"        blob = _slow_serialize({\"job\": \"long\", \"doc\": doc_name or \"?\", \"iter\": iters})",
	"        try:",
	"            user = frappe.get_doc(\"User\", frappe.session.user)",
	"            frappe.db.sql(",
	"                \"SELECT name, email FROM `tabUser` LIMIT 20\", as_dict=True,",
	"            )",
	"        except Exception:",
	"            pass",
]
# synthetic_batch.py (cases syn-1, syn-3), tab-indented like Frappe apps
DEMO_ORDER = [
	"class DemoOrder(Document):",
	"\tdef validate(self):",
	"\t\tfor d in self.items:",
	"\t\t\tuom = frappe.db.get_value(\"Item\", d.item_code, \"stock_uom\")",
	"\t\t\td.uom = uom",
	"\t\tfor d in self.items:",
	"\t\t\titem = frappe.get_doc(\"Item\", d.item_code)",
	"\t\t\titem.last_ordered = frappe.utils.today()",
	"\t\t\titem.save()",
]


def _facts(call, *, kind="for", line, deps=(), writes=(), used=True):
	return {
		"in_loop": True, "loop_kind": kind, "loop_line": line, "call": call,
		"depends_on_loop_vars": list(deps), "loop_writes": list(writes), "result_used": used,
	}


class TestCorpus:
	def test_invariant_sql_in_loop(self):
		assert fr.loop_facts(CHECK_USER, 4) == _facts("frappe.db.sql", line=2)

	def test_invariant_get_doc_in_loop(self):
		assert fr.loop_facts(CHECK_USER, 3) == _facts("frappe.get_doc", line=2)

	def test_ignored_has_permission_result(self):
		assert fr.loop_facts(VERIFY_PERMS, 4) == _facts("frappe.has_permission", line=2, used=False)

	def test_window_cut_inside_try_is_repaired(self):
		assert fr.loop_facts(RECHECK_CUT, 5) == _facts("frappe.db.sql", line=2, used=False)

	def test_while_loop(self):
		assert fr.loop_facts(LONG_JOB, 8) == _facts("frappe.db.sql", kind="while", line=3, used=False)

	def test_call_depends_on_the_loop_variable(self):
		assert fr.loop_facts(DEMO_ORDER, 4) == _facts("frappe.db.get_value", line=3, deps=["d"])

	def test_loop_writes_are_reported(self):
		assert fr.loop_facts(DEMO_ORDER, 7) == _facts("frappe.get_doc", line=6, deps=["d"], writes=["item.save"])


class TestShapes:
	def test_comprehension(self):
		lines = [
			"def f(codes):",
			"\tnames = [frappe.db.get_value('Item', d, 'item_name') for d in codes]",
			"\treturn names",
		]
		assert fr.loop_facts(lines, 2) == _facts(
			"frappe.db.get_value", kind="comprehension", line=2, deps=["d"],
		)

	def test_multi_line_loop_header(self):
		lines = [
			"def f():",
			"\tfor d in frappe.get_all(",
			"\t\t'Item',",
			"\t\tfilters={'disabled': 0},",
			"\t):",
			"\t\tfoo(d.name)",
			"\treturn 1",
		]
		assert fr.loop_facts(lines, 6) == _facts("foo", line=2, deps=["d"], used=False)

	def test_statement_after_a_loop_is_not_in_it(self):
		lines = ["def f(items):", "\tfor d in items:", "\t\tbar(d)", "\tfoo()"]
		assert fr.loop_facts(lines, 4) == {"in_loop": False}

	def test_continuation_line_after_a_loop_is_not_in_it(self):
		lines = ["def f(y):", "\tfor d in y:", "\t\tpass", "\tx = (", "\t\tfoo()", "\t)"]
		assert fr.loop_facts(lines, 5) == {"in_loop": False}

	def test_header_outside_the_window_is_unknown(self):
		assert fr.loop_facts(["\t\tx = 1", "\t\tfrappe.db.get_value('Item', x)", "\t\ty = 2"], 2) == {}

	def test_out_of_range_and_blank_targets(self):
		assert fr.loop_facts(["a = 1"], 3) == {}
		assert fr.loop_facts(["", "a = 1"], 1) == {}


class TestFormat:
	def test_sentences_use_file_line_numbers(self):
		text = fr.format_loop_facts(fr.loop_facts(DEMO_ORDER, 4), line_offset=100)
		assert text == (
			"Loop facts computed by the profiler from the code shown: the marked line runs inside "
			"the for loop that starts on line 103. The call frappe.db.get_value uses these loop "
			"variables: d. The result of frappe.db.get_value is used. The loop makes no database "
			"write that the profiler can see."
		)

	def test_not_in_loop_and_unknown(self):
		assert "is not inside a for or while loop" in fr.format_loop_facts({"in_loop": False})
		assert fr.format_loop_facts({}) == ""

	def test_writes_and_unused_result(self):
		text = fr.format_loop_facts(_facts("frappe.get_doc", line=1, writes=["item.save"], used=False))
		assert "The result of frappe.get_doc is not used." in text
		assert "The loop also writes through: item.save." in text
		assert "\u2014" not in text
