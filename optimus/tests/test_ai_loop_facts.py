# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Loop facts reach the user message for N+1 Query / Redundant Call / Hot Line only,
are computed for the window lines still shown, sit inside a data block (P12), and a
line in no loop of its function gets the caller hint for N+1 and Redundant Call (P7)."""

import ast

from optimus import ai_fix, ai_grounding, ai_prompts

_DEMO = [
	"class DemoOrder(Document):",
	"\tdef validate(self):",
	"\t\tfor d in self.items:",
	"\t\t\tuom = frappe.db.get_value(\"Item\", d.item_code, \"stock_uom\")",
	"\t\t\td.uom = uom",
]


def _window(first=40, target_index=3):
	return [
		{"lineno": first + i, "content": text, "is_target": i == target_index}
		for i, text in enumerate(_DEMO)
	]


def _finding(ftype="N+1 Query", window=None):
	return {
		"finding_type": ftype, "severity": "High", "title": "t",
		"technical_detail": {"callsite": {"filename": "apps/myapp/myapp/order.py", "lineno": 43, "function": "validate"}},
		"source_window": window if window is not None else _window(),
	}


def test_loop_facts_text_uses_file_line_numbers():
	text = ai_fix._loop_facts_text(_finding())
	assert text.startswith("The marked line runs inside the for loop on line 42.")
	assert "The call frappe.db.get_value uses variables that change in that loop: d." in text


def test_only_loop_shaped_types_get_facts():
	for ftype in ("N+1 Query", "Redundant Call", "Hot Line"):
		assert ai_fix._loop_facts_text(_finding(ftype))
	for ftype in ("Slow Query", "Missing Index", "Framework N+1"):
		assert ai_fix._loop_facts_text(_finding(ftype)) == ""


def test_gapped_or_targetless_window_gives_nothing():
	gapped = _window()
	gapped[2]["lineno"] += 5
	assert ai_fix._loop_facts_text(_finding(window=gapped)) == ""
	no_target = [dict(r, is_target=False) for r in _window()]
	no_target_finding = _finding(window=no_target)
	no_target_finding["technical_detail"]["callsite"]["lineno"] = 999
	assert ai_fix._loop_facts_text(no_target_finding) == ""


def test_the_facts_sit_inside_a_data_block():
	"""P12: the identifiers no longer bypass the untrusted-data fence."""
	content = ai_fix._build_messages(_finding())[1][-1]["content"]
	head, after = content.split(ai_fix._LOOP_FACTS_HEAD, 1)
	first, rest = after.split("\n", 2)[1], after.split("\n", 2)[2]
	assert first.startswith("<data-") and 'kind="loop-facts"' in first
	assert "frappe.db.get_value" in rest.split("</data-", 1)[0]
	assert "frappe.db.get_value" not in ai_fix._LOOP_FACTS_HEAD


def test_slow_query_gets_no_loop_facts():
	content = ai_fix._build_messages(_finding("Slow Query"))[1][-1]["content"]
	assert ai_fix._LOOP_FACTS_HEAD not in content


def test_facts_follow_the_trimmed_window():
	"""P12: when the budget trims the loop header out of the window, no loop fact is sent."""
	lines = ["def f(items):", "\tfor d in items:"] + [f"\t\tx_{i} = '{'a' * 140}'" for i in range(76)]
	lines += ["\t\tfrappe.db.get_value('Item', d, 'name')", "\t\tpass"]
	window = [{"lineno": i + 1, "content": text, "is_target": i + 1 == 79} for i, text in enumerate(lines)]
	finding = _finding(window=window)
	finding["technical_detail"]["callsite"]["lineno"] = 79
	finding["loop_facts"] = ai_grounding.loop_facts_from_tree(ast.parse("\n".join(lines)), 79)
	wide = ai_fix._build_messages(finding, context_tokens=200000)[1][-1]["content"]
	assert ai_fix._LOOP_FACTS_HEAD in wide and "for loop on line 2" in wide
	narrow = ai_fix._build_messages(finding, context_tokens=4096)[1][-1]["content"]
	assert ">> 79:" in narrow
	assert "for loop on line 2" not in narrow


def test_whole_file_facts_cover_a_window_that_does_not_parse_alone():
	"""A3: a window cut inside a try block does not parse on its own; the facts analyze
	computed from the whole file still reach the prompt."""
	lines = [
		"def bg_recheck_users(doc_name=None):",
		"\tfor i in range(50):",
		"\t\ttry:",
		"\t\t\tuser = frappe.get_doc('User', frappe.session.user)",
		"\t\t\tfrappe.db.sql('SELECT role FROM `tabHas Role` WHERE parent=%s', (frappe.session.user,))",
		"\t\texcept Exception:",
		"\t\t\tpass",
	]
	window = [{"lineno": i + 1, "content": text, "is_target": i + 1 == 5} for i, text in enumerate(lines[:5])]
	finding = _finding(window=window)
	assert ai_fix._loop_facts_text(finding) == ""
	finding["loop_facts"] = ai_grounding.loop_facts_from_tree(ast.parse("\n".join(lines)), 5)
	assert ai_fix._loop_facts_text(finding).startswith("The marked line runs inside the for loop on line 2.")


def test_carried_facts_need_the_callsite_line_in_the_rows():
	finding = _finding(window=[dict(r, is_target=False) for r in _window()])
	finding["loop_facts"] = ai_grounding.loop_facts_from_window(_window(), 43)
	assert ai_fix._loop_facts_text(finding)  # the callsite line 43 is one of the rows
	finding["technical_detail"]["callsite"]["lineno"] = 999
	assert ai_fix._loop_facts_text(finding) == ""


def test_a_line_in_no_loop_gets_the_caller_hint_for_n_plus_one_only():
	"""P7."""
	window = [
		{"lineno": 10, "content": "def get_user(name):", "is_target": False},
		{"lineno": 11, "content": "\treturn frappe.get_doc('User', name)", "is_target": True},
	]
	assert "caller that is not shown" in ai_fix._loop_facts_text(_finding("N+1 Query", window))
	assert "caller that is not shown" not in ai_fix._loop_facts_text(_finding("Hot Line", window))


def test_prompt_version_bumped():
	assert ai_prompts.PROMPT_VERSION == 4
