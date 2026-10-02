# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""A suggested DocType hook includes every import it needs."""

import sys
from types import ModuleType, SimpleNamespace

from optimus.renderer import fix_recipes
from optimus.tests.test_fix_recipes_index import _explain, _meta


def test_own_app_hook_can_run_in_a_module_without_a_frappe_import(monkeypatch):
	frappe = ModuleType("frappe")
	calls = []
	frappe.db = SimpleNamespace(add_index=lambda *args: calls.append(args))
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	recipe = fix_recipes.index_recipe(
		_explain("Full Table Scan", "SELECT name FROM `tabSales Invoice` WHERE customer = ? AND status = ?"),
		meta_lookup=_meta("myapp", own=True, fields={"customer": ("Link", 0), "status": ("Data", 0)}),
	)
	namespace = {}
	exec(compile(recipe["code"], "generated_hook.py", "exec"), namespace)
	namespace["on_doctype_update"]()
	assert calls == [("Sales Invoice", ["customer", "status"])]
