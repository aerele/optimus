# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Conservative handling of malformed index inputs and incomplete loop facts."""

from optimus.renderer import fix_recipes as fr


def test_loop_receiver_is_a_variant_even_without_arguments():
	facts = fr.loop_facts(["def f(docs):", "\tfor doc in docs:", "\t\tdoc.reload()"], 3)
	assert facts["depends_on_loop_vars"] == ["doc"]
	assert "no variable that changes" not in fr.format_loop_facts(facts)


def test_comprehension_filter_call_is_inside_the_loop():
	facts = fr.loop_facts(["def f(names):", "\treturn [name for name in names if frappe.db.exists('Item', name)]"], 2)
	assert facts["in_loop"] and facts["loop_kind"] == "comprehension"
	assert facts["call"] == "frappe.db.exists"
	assert facts["depends_on_loop_vars"] == ["name"]
