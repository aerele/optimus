# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""fix_recipes.hot_line_gate: no AI call for a Hot Line whose time is spent
inside a function it calls or that sits in framework code (spec N1d)."""

import json

from optimus.renderer import fix_recipes as fr

_USER_FILE = "/home/b/apps/myapp/myapp/controllers/order.py"


def _hot_line(content, *, file=_USER_FILE, **extra):
	detail = {"file": file, "lineno": 9, "line_content": content}
	detail.update(extra)
	return {"finding_type": "Hot Line", "technical_detail": detail}


class TestCalleeGate:
	def test_super_call_is_gated(self):
		note = fr.hot_line_gate(_hot_line("        super().validate()"))
		assert note.startswith("Most of this line's time is spent inside super().validate,")

	def test_method_call_is_gated(self):
		assert "inside self.set_missing_values," in fr.hot_line_gate(_hot_line("self.set_missing_values()"))

	def test_elif_with_a_call_is_gated(self):
		assert "inside get_rate," in fr.hot_line_gate(_hot_line("elif get_rate(d):"))

	def test_phase1_hint_names_the_callee(self):
		note = fr.hot_line_gate(_hot_line(
			"bin = get_bin(item, wh)", phase1_hint={"next_hot_callee": "erpnext.stock.utils.get_bin"},
		))
		assert "inside erpnext.stock.utils.get_bin," in note

	def test_cheap_calls_do_not_gate(self):
		for content in (
			"total = total + flt(row.qty) * flt(row.rate)",
			"out.append(d.name)",
			"x = [d.qty for d in self.items if d.qty > 0]",
			"if d.item_code in seen:",
			"value = row.get('rate') or 0",
			"n = len(rows)",
		):
			assert fr.hot_line_gate(_hot_line(content)) is None, content

	def test_technical_detail_json_string_is_accepted(self):
		finding = {"finding_type": "Hot Line", "technical_detail_json": json.dumps(
			{"file": _USER_FILE, "line_content": "self.validate_items()"},
		)}
		assert "inside self.validate_items," in fr.hot_line_gate(finding)


class TestFrameworkGate:
	def test_erpnext_file_is_gated_even_for_pure_python(self):
		note = fr.hot_line_gate(_hot_line(
			"x = 1", file="/home/b/apps/erpnext/erpnext/controllers/selling_controller.py",
		))
		assert note.startswith("This line is in framework or library code (erpnext/erpnext/controllers/")

	def test_untracked_app_is_framework_in_inclusion_mode(self):
		note = fr.hot_line_gate(
			_hot_line("x = 1", file="/home/b/apps/otherapp/otherapp/y.py"), tracked_apps=("myapp",),
		)
		assert note.startswith("This line is in framework or library code")

	def test_site_packages_is_gated(self):
		note = fr.hot_line_gate(_hot_line("x = 1", file="/venv/lib/python3.14/site-packages/pandas/core/frame.py"))
		assert note.startswith("This line is in framework or library code")

	def test_tracked_user_app_is_not_gated(self):
		assert fr.hot_line_gate(_hot_line("x = 1"), tracked_apps=("myapp",)) is None


def test_other_finding_types_are_never_gated():
	assert fr.hot_line_gate({"finding_type": "N+1 Query", "technical_detail": {
		"file": _USER_FILE, "line_content": "super().validate()",
	}}) is None


def test_notes_have_no_em_dash_or_backticks():
	for finding in (
		_hot_line("super().validate()"),
		_hot_line("x = 1", file="/home/b/apps/erpnext/erpnext/x.py"),
	):
		note = fr.hot_line_gate(finding)
		assert "\u2014" not in note and "`" not in note
