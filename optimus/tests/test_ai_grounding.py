# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""ai_grounding: the one source-window helper, and the loop chain around a callsite
from the whole file's AST (owner decision A3), formatted only for the lines the prompt
still shows (P12)."""

import ast

import pytest

from optimus import ai_grounding as g


def _facts(lines, target):
	return g.loop_facts_from_tree(ast.parse("\n".join(lines)), target)


def _text(lines, target, *, first=1, last=None, caller_hint=False):
	return g.format_loop_facts(
		_facts(lines, target), first_line=first, last_line=last or len(lines), caller_hint=caller_hint,
	)


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
NESTED = [
	"def post(invoices):",
	"\tfor inv in invoices:",
	"\t\tfor row in inv.items:",
	"\t\t\trate = frappe.db.get_value('Item Price', {'item_code': row.item_code, 'price_list': inv.price_list}, 'rate')",
	"\t\t\trow.rate = rate",
	"\t\tinv.db_update()",
]


class TestGroundingWindow:
	def test_a_small_function_is_shown_whole(self):
		lines = ["import os", "", "def f(doc):", "\treturn doc.owner", "", "def g():", "\tpass"]
		window = g.grounding_window(lines, 4, 24, 24)
		assert [r["lineno"] for r in window.rows] == [3, 4] and (window.start, window.end) == (3, 4)
		assert [r["lineno"] for r in window.rows if r["is_target"]] == [4]
		assert isinstance(window.tree, ast.Module)

	def test_decorators_and_the_outer_function_are_included(self):
		lines = ["@decorator", "def outer(items):", "\tfor item in items:", "\t\tdef inner():",
			"\t\t\treturn item.name", "\t\tinner()"]
		assert [r["lineno"] for r in g.grounding_window(lines, 5, 24, 24).rows] == list(range(1, 7))

	def test_an_80_line_function_is_whole_and_81_falls_back(self):
		"""E-I4: the max_lines boundary."""
		fits = ["def f():"] + ["\tx = 1"] * 79
		window = g.grounding_window(fits + ["", "y = 2"], 40, 24, 24)
		assert (window.start, window.end) == (1, 80)
		too_big = ["def f():"] + ["\tx = 1"] * 80
		window = g.grounding_window(too_big, 40, 24, 24)
		assert (window.start, window.end) == (16, 64)

	def test_the_fallback_uses_before_and_after_as_given(self):
		"""E-I4: an asymmetric fallback."""
		window = g.grounding_window(["value = 0"] * 121, 61, 10, 30)
		assert (window.start, window.end, len(window.rows)) == (51, 91, 41)

	def test_max_lines_is_honoured(self):
		lines = ["def f():"] + ["\tx = 1"] * 9
		window = g.grounding_window(lines, 5, 2, 2, max_lines=10)
		assert (window.start, window.end) == (1, 10)
		window = g.grounding_window(lines, 5, 2, 2, max_lines=9)
		assert (window.start, window.end) == (3, 7)

	def test_an_unparseable_file_falls_back_and_has_no_tree(self):
		# A real syntax error: ast.parse accepts a module-level "return" (only compile rejects it).
		lines = [f"x{i} = {i}" for i in range(60)] + ["def broken(:"] + [f"y{i} = {i}" for i in range(60)]
		window = g.grounding_window(lines, 61, 24, 24)
		assert (window.start, window.end, window.tree) == (37, 85, None)

	def test_a_long_line_is_capped(self):
		window = g.grounding_window(["def f():", "\tvalue = '" + "a" * 400 + "'"], 2, 24, 24, max_line_chars=200)
		assert len(window.rows[1]["content"]) == 203 and window.rows[1]["content"].endswith("...")

	@pytest.mark.parametrize("target", [0, -1, 3, True, "1", None])
	def test_an_invalid_target_has_no_rows(self, target):
		assert g.grounding_window(["def f():", "\tpass"], target, 24, 24).rows == []


class TestLoopChain:
	def test_invariant_sql_in_a_loop(self):
		facts = _facts(CHECK_USER, 4)
		assert (facts["call"], facts["call_line"], [loop["line"] for loop in facts["loops"]]) == ("frappe.db.sql", 4, [2])
		assert _text(CHECK_USER, 4) == (
			"The marked line runs inside the for loop on line 2. The call frappe.db.sql uses no variable "
			"that changes in that loop. The result of frappe.db.sql is used. The profiler sees no database "
			"write inside this loop in the code shown."
		)

	def test_the_call_depends_on_the_loop_variable(self):
		assert _text(DEMO_ORDER, 4) == (
			"The marked line runs inside the for loop on line 3. The call frappe.db.get_value uses "
			"variables that change in that loop: d. The result of frappe.db.get_value is used. The profiler "
			"sees no database write inside this loop in the code shown."
		)

	def test_writes_in_the_loop_are_reported(self):
		assert "Inside this loop the code also writes through: item.save." in _text(DEMO_ORDER, 7)

	def test_nested_loops_report_the_whole_chain(self):
		"""E-I3: the outer loop's variable is reported, not only the innermost loop's."""
		assert _text(NESTED, 4) == (
			"The marked line runs inside the for loop on line 3, which runs inside the for loop on line 2. "
			"The call frappe.db.get_value uses variables that change in the loop on line 3: row. "
			"The call frappe.db.get_value uses variables that change in the loop on line 2: inv, row. "
			"The result of frappe.db.get_value is used. "
			"Inside these loops the code also writes through: inv.db_update."
		)

	def test_a_walrus_while_header_is_in_the_loop(self):
		"""P11a."""
		lines = [
			"def drain():",
			"\twhile (row := frappe.db.get_value('Queue', {'status': 'Open'}, 'name')):",
			"\t\tfrappe.db.set_value('Queue', row, 'status', 'Done')",
		]
		facts = _facts(lines, 2)
		assert facts["in_loop"] and facts["loops"][0]["kind"] == "while"
		assert ["frappe.db.set_value", 3] in facts["loops"][0]["writes"]

	def test_the_element_line_of_a_multi_line_comprehension(self):
		"""P11a."""
		lines = ["def names(codes):", "\treturn [", "\t\tfrappe.db.get_value('Item', d, 'item_name')",
			"\t\tfor d in codes", "\t]"]
		assert "uses variables that change in that loop: d." in _text(lines, 3)
		assert _facts(lines, 3)["loops"][0]["kind"] == "comprehension"

	def test_a_comprehension_inside_a_for_loop_keeps_the_outer_loop(self):
		"""P11a: the statement holding a comprehension no longer hides the for loop."""
		lines = ["def f(orders):", "\tfor o in orders:",
			"\t\tnames = [frappe.db.get_value('Item', d.item_code, 'item_name') for d in o.items]"]
		assert [(loop["kind"], loop["line"]) for loop in _facts(lines, 3)["loops"]] == [("comprehension", 3), ("for", 2)]

	def test_an_attribute_target_binds_no_name(self):
		"""P11b: self.total += d.amount does not make self loop-variant."""
		lines = ["def total(self):", "\tfor d in self.items:", "\t\tself.total += d.amount",
			"\t\tfrappe.db.get_value('Company', self.company, 'default_currency')"]
		text = _text(lines, 4)
		assert "uses no variable that changes in that loop" in text
		assert "The result of frappe.db.get_value is not used." in text

	def test_a_subscript_receiver_write_is_seen(self):
		"""P11c."""
		lines = ["def save_rows(self):", "\tfor i in range(len(self.items)):",
			"\t\tfrappe.get_doc('Item', self.items[i].item_code)", "\t\tself.items[i].db_update()"]
		text = _text(lines, 3)
		assert "self.items[].db_update" in text and "in that loop: i." in text

	def test_formatted_sql_writes_are_seen(self):
		"""P11c: f-string, .format and % SQL writes."""
		lines = [
			"def mark(names):",
			"\tfor n in names:",
			"\t\tfrappe.get_doc('Item', n)",
			"\t\tfrappe.db.sql(f\"UPDATE `tabItem` SET disabled = 1 WHERE name = '{n}'\")",
			"\t\tfrappe.db.sql(\"DELETE FROM `tabBin` WHERE item_code = '{}'\".format(n))",
			"\t\tfrappe.db.sql(\"INSERT INTO `tabLog` VALUES ('%s')\" % n)",
		]
		assert "frappe.db.sql(DELETE), frappe.db.sql(INSERT), frappe.db.sql(UPDATE)" in _text(lines, 3)

	def test_async_for_with_match(self):
		"""E-I4: async and match shapes."""
		lines = ["async def f(rows):", "\tasync for r in rows:", "\t\tmatch r.kind:", "\t\t\tcase 'item':",
			"\t\t\t\tawait frappe.get_doc('Item', r.name)"]
		facts = _facts(lines, 5)
		assert facts["loops"][0]["kind"] == "for" and facts["result_used"] is False
		assert "in that loop: r." in _text(lines, 5)

	def test_a_generator_yield_uses_the_result(self):
		"""E-I4: generator shape."""
		lines = ["def rows(names):", "\tfor n in names:", "\t\tyield frappe.get_doc('Item', n)"]
		assert _facts(lines, 3)["result_used"] is True

	def test_a_call_in_the_for_iterable_runs_once(self):
		assert _facts(["def f():", "\tfor d in frappe.get_all('Item'):", "\t\tpass"], 2) == {"in_loop": False}

	def test_a_lambda_inside_a_loop_stops_the_walk(self):
		lines = ["def f(rows):", "\tfor r in rows:", "\t\tkey = lambda x: frappe.get_doc('Item', x)"]
		assert _facts(lines, 3) == {"in_loop": False}

	def test_a_call_in_a_comprehension_filter_runs_every_pass(self):
		"""Kept from the deleted test_fix_recipes_boundaries.py."""
		facts = _facts(["def f(names):", "\treturn [name for name in names if frappe.db.exists('Item', name)]"], 2)
		assert (facts["call"], facts["loops"][0]["kind"], facts["loops"][0]["bound"]) == (
			"frappe.db.exists", "comprehension", [["name", 2]],
		)

	def test_the_loop_receiver_is_a_variable_the_call_uses(self):
		"""Kept from the deleted test_fix_recipes_boundaries.py."""
		assert "in that loop: doc." in _text(["def f(docs):", "\tfor doc in docs:", "\t\tdoc.reload()"], 3)

	def test_a_multi_line_for_header(self):
		"""Kept from the deleted test_fix_recipes_loop_facts.py."""
		lines = ["def f():", "\tfor d in frappe.get_all(", "\t\t'Item',", "\t\tfilters={'disabled': 0},", "\t):",
			"\t\tfoo(d.name)", "\treturn 1"]
		facts = _facts(lines, 6)
		assert (facts["call"], facts["loops"][0]["line"], facts["result_used"]) == ("foo", 2, False)

	def test_code_after_the_loop_is_not_in_it(self):
		"""Kept from the deleted test_fix_recipes_loop_facts.py."""
		assert _facts(["def f(items):", "\tfor d in items:", "\t\tbar(d)", "\tfoo()"], 4) == {"in_loop": False}
		lines = ["def f(y):", "\tfor d in y:", "\t\tpass", "\tx = (", "\t\tfoo()", "\t)"]
		assert _facts(lines, 5) == {"in_loop": False}

	def test_list_insert_and_a_nested_function_are_not_loop_writes(self):
		lines = ["def f(rows):", "\tout = []", "\tfor r in rows:", "\t\tfrappe.get_doc('Item', r)",
			"\t\tout.insert(0, r)", "\t\tif r:", "\t\t\tdef later():", "\t\t\t\tr.save()",
			"\t\tcallbacks.append(lambda: r.db_update())"]
		assert _facts(lines, 4)["loops"][0]["writes"] == []

	def test_unknown_targets(self):
		assert g.loop_facts_from_tree(ast.parse("x = 1"), 5) == {}
		assert g.loop_facts_from_tree(None, 1) == {}
		assert g.loop_facts_from_tree(ast.parse("x = 1"), True) == {}


class TestFormatting:
	def test_not_in_a_loop_gets_the_caller_hint_only_when_asked(self):
		"""P7: the loop may be in a caller that is not shown."""
		lines = ["def get_user(name):", "\treturn frappe.get_doc('User', name)"]
		assert "The repetition may come from a caller that is not shown" in _text(lines, 2, caller_hint=True)
		assert "caller" not in _text(lines, 2)

	def test_a_loop_header_above_the_shown_lines_is_left_out(self):
		"""P12: only facts about lines still shown."""
		text = _text(NESTED, 4, first=3)
		assert text.startswith("The marked line runs inside the for loop on line 3.")
		assert "line 2" not in text and "inv" not in text

	def test_no_shown_loop_gives_no_facts(self):
		assert _text(NESTED, 4, first=4) == ""

	def test_writes_on_lines_not_shown_are_left_out(self):
		assert "no database write" in _text(DEMO_ORDER, 7, last=8)

	def test_the_window_fallback_uses_file_line_numbers(self):
		rows = [{"lineno": 40 + i, "content": text, "is_target": i == 3} for i, text in enumerate(DEMO_ORDER[:5])]
		facts = g.loop_facts_from_window(rows, 43)
		assert facts["loops"][0]["line"] == 42 and facts["call_line"] == 43
