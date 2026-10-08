# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""ai_grounding.hot_line_gate (owner decision A4): a Hot Line gets no AI call when it
sits in framework code, when Phase 1 named the callee that holds its time, or when
Phase 2 measured at least HOT_LINE_CALLEE_US per hit and the line's statement calls a
non-builtin. The note names the right callee and advises a Phase 2 re-run only for a
function that can be the developer's. Also the Redundant Call callsite stamp."""

import json

import pytest

from optimus import ai_grounding as g

_USER_FILE = "/home/b/apps/myapp/myapp/controllers/order.py"


def _hot_line(content, *, per_hit_us=0.0, file=_USER_FILE, **extra):
	detail = {"file": file, "lineno": 9, "line_content": content, "per_hit_us": per_hit_us}
	detail.update(extra)
	return {"finding_type": "Hot Line", "technical_detail": detail}


class TestMeasuredGate:
	@pytest.mark.parametrize("content,per_hit,callee", [
		("\t\tsuper().validate()", 132809.5, "super().validate"),
		("\t\tn = frappe.db.count('GL Entry', {'company': c})", 450000.0, "frappe.db.count"),
		("\t\tdoc.insert()", 300000.0, "doc.insert"),
		("\t\tv = frappe.cache.get('k')", 120000.0, "frappe.cache.get"),
		("\t\tr = requests.get(url, timeout=5)", 800000.0, "requests.get"),
		("\t\tdata = json.loads(blob)", 250000.0, "json.loads"),
		("\t\t\tself.set_rate(d)", 4000.0, "self.set_rate"),
	])
	def test_a_costly_call_is_gated_and_named(self, content, per_hit, callee):
		"""P13: these went to the AI or were misnamed with the name-list gate."""
		note = g.hot_line_gate(_hot_line(content, per_hit_us=per_hit))
		assert note.startswith(f"Most of this line's time is spent inside {callee},")

	@pytest.mark.parametrize("content,per_hit", [
		("            rebuilt = rebuilt + ch", 0.25),
		("        sq += n", 0.27),
		("\t\t\tuom = frappe.get_cached_value('Item', d.item_code, 'stock_uom')", 18.0),
		("\t\t\titem = frappe.get_doc('Item', d.item_code)", 900.0),
		("\t\t\tqty = row.get('qty')", 0.3),
		("\t\trows = sorted(rows, key=lambda r: r.idx)", 90000.0),
		("\t\t\tif d.name in seen_list:", 3.0),
		("\t\telse:", 50000.0),
	])
	def test_cheap_or_call_free_lines_reach_the_ai(self, content, per_hit):
		assert g.hot_line_gate(_hot_line(content, per_hit_us=per_hit)) is None

	def test_the_threshold_is_inclusive(self):
		assert g.hot_line_gate(_hot_line("\t\tself.run()", per_hit_us=g.HOT_LINE_CALLEE_US))
		assert g.hot_line_gate(_hot_line("\t\tself.run()", per_hit_us=g.HOT_LINE_CALLEE_US - 0.01)) is None

	def test_multi_line_call_opener_over_the_threshold_is_gated(self):
		"""Review Focus 4 (E-I2): the opener of a multi-line call."""
		note = g.hot_line_gate(_hot_line("\t\tresult = frappe.get_all(", per_hit_us=700000.0))
		assert note.startswith("Most of this line's time is spent inside frappe.get_all,")

	def test_a_multi_line_for_header_opener_is_gated(self):
		note = g.hot_line_gate(_hot_line("\tfor d in frappe.get_all('Item', filters={", per_hit_us=5000.0))
		assert "inside frappe.get_all," in note

	def test_a_continuation_line_with_a_call(self):
		note = g.hot_line_gate(_hot_line("\t\t\tfilters={'item': get_item_code(row)},", per_hit_us=5000.0))
		assert "inside get_item_code," in note

	def test_a_subscript_receiver_is_named(self):
		note = g.hot_line_gate(_hot_line("\t\tself.items[i].db_update()", per_hit_us=9000.0))
		assert "inside self.items[].db_update," in note

	def test_a_raise_constructor_is_skipped_when_naming_the_callee(self):
		note = g.hot_line_gate(_hot_line("\t\traise ValidationError(get_message(doc))", per_hit_us=9000.0))
		assert "inside get_message," in note

	def test_elif_and_match_headers(self):
		assert "inside get_rate," in g.hot_line_gate(_hot_line("elif get_rate(d):", per_hit_us=5000.0))
		assert "inside classify," in g.hot_line_gate(_hot_line("match classify(d):", per_hit_us=5000.0))

	def test_phase1_hint_names_the_callee_whatever_the_time(self):
		note = g.hot_line_gate(_hot_line(
			"bin = get_bin(item, wh)", phase1_hint={"next_hot_callee": "myapp.stock.utils.get_bin"},
		))
		assert "inside myapp.stock.utils.get_bin," in note
		assert "Re-run Phase 2 with myapp.stock.utils.get_bin picked" in note

	def test_technical_detail_json_string_is_accepted(self):
		finding = {"finding_type": "Hot Line", "technical_detail_json": json.dumps(
			{"file": _USER_FILE, "line_content": "self.validate_items()", "per_hit_us": 2500.0},
		)}
		assert "inside self.validate_items," in g.hot_line_gate(finding)

	def test_an_unparseable_fragment_with_a_parenthesis_is_gated_without_a_name(self):
		note = g.hot_line_gate(_hot_line('\t\t"""SELECT name FROM `tabItem` WHERE x IN (', per_hit_us=5000.0))
		assert note.startswith("Most of this line's time is spent inside a function it calls,")


class TestStatementShapes:
	"""Carried from the Task 4 review: the statement parser on continuation lines,
	decorators, ``raise ... from ...`` and gettext's ``_``."""

	@pytest.mark.parametrize("line,calls,callee", [
		# a continuation line that starts by closing the previous line's brackets
		("\t) + foo(x)", True, "foo"),
		("\t\t]) + get_rows(a)", True, "get_rows"),
		("\t\t}] - discount(d)", True, "discount"),
		# a fragment that stays unparseable still counts as calling, unnamed
		("\t\t}, as_dict=compute_flag(d))", True, None),
		("\t\t)", False, None),
		# a decorator line is parsed without its @
		("@decorator(arg)", True, "decorator"),
		("\t@frappe.whitelist()", True, "frappe.whitelist"),
		("@property", False, None),
		# raise: only the raised exception's constructor is skipped, never its cause
		("raise Foo(x) from bar(y)", True, "bar"),
		("raise Foo from bar(y)", True, "bar"),
		("raise Foo(get_message(d)) from err", True, "get_message"),
		("raise Foo(x) from err", False, None),
		# gettext's _ is a translation lookup, never the callee that holds the time
		("raise frappe.ValidationError(_('Bad row'))", False, None),
		("msg = _('Row {0} is invalid')", False, None),
		("msg = _(build_message(d))", True, "build_message"),
		("frappe.throw(_('Bad row'))", True, "frappe.throw"),
		("_ = foo()", True, "foo"),
	])
	def test_statement_calls(self, line, calls, callee):
		assert tuple(g.statement_calls(line)) == (calls, callee)

	@pytest.mark.parametrize("per_hit_us", [float("nan"), "abc", None, "", [], {}])
	def test_an_unmeasured_per_hit_time_never_gates(self, per_hit_us):
		assert g.hot_line_gate(_hot_line("\t\tx = foo(1)", per_hit_us=per_hit_us)) is None

	def test_a_numeric_string_per_hit_time_is_measured(self):
		assert "inside foo," in g.hot_line_gate(_hot_line("\t\tx = foo(1)", per_hit_us="5000"))

	@pytest.mark.parametrize("callee,tracked,phase2_offered", [
		# Tracked Apps unset: Phase 1 only names user-code descendants, so any app is yours
		("otherapp.utils.get_rate", (), True),
		("myapp.utils.get_rate", (), True),
		# Tracked Apps set: only a tracked app is yours, any other app is not the developer's
		("otherapp.utils.get_rate", ("myapp",), False),
		("myapp.utils.get_rate", ("myapp",), True),
		# framework and stdlib are never yours, either way
		("erpnext.stock.utils.get_bin", (), False),
		("erpnext.stock.utils.get_bin", ("myapp",), False),
		("json.loads", ("myapp",), False),
	])
	def test_phase1_callee_scope_follows_tracked_apps(self, callee, tracked, phase2_offered):
		note = g.hot_line_gate(
			_hot_line("x = get_rate(d)", phase1_hint={"next_hot_callee": callee}), tracked_apps=tracked,
		)
		assert f"inside {callee}," in note
		assert ("Re-run Phase 2 with " + callee + " picked" in note) is phase2_offered
		assert ("is standard library, framework or third-party code" in note) is not phase2_offered

	def test_a_phase1_method_on_self_stays_conditional_with_tracked_apps(self):
		note = g.hot_line_gate(
			_hot_line("self.set_rate(d)", phase1_hint={"next_hot_callee": "self.set_rate"}), tracked_apps=("myapp",),
		)
		assert "If self.set_rate is defined in your app, re-run Phase 2 with it picked" in note


class TestCalleeAdvice:
	def test_a_stdlib_callee_is_never_sent_to_phase_2(self):
		"""P13: the Phase 2 picker cannot pick json.loads."""
		note = g.hot_line_gate(_hot_line("\t\tdata = json.loads(blob)", per_hit_us=250000.0))
		assert "json.loads is standard library, framework or third-party code" in note
		assert "re-run phase 2" not in note.lower() and "call it less often" in note

	def test_a_framework_callee_is_never_sent_to_phase_2(self):
		note = g.hot_line_gate(_hot_line("\t\tn = frappe.db.count('GL Entry')", per_hit_us=450000.0))
		assert "re-run phase 2" not in note.lower()

	def test_an_erpnext_phase1_callee_is_framework(self):
		note = g.hot_line_gate(_hot_line(
			"bin = get_bin(item, wh)", phase1_hint={"next_hot_callee": "erpnext.stock.utils.get_bin"},
		))
		assert "inside erpnext.stock.utils.get_bin," in note and "re-run phase 2" not in note.lower()

	def test_a_method_on_self_is_conditional(self):
		note = g.hot_line_gate(_hot_line("\t\t\tself.set_rate(d)", per_hit_us=4000.0))
		assert "If self.set_rate is defined in your app, re-run Phase 2 with it picked" in note

	def test_a_parent_class_method_is_conditional(self):
		"""super() is a builtin name, but the method it reaches can be the developer's."""
		note = g.hot_line_gate(_hot_line("\t\tsuper().validate()", per_hit_us=132809.5))
		assert "If super().validate is defined in your app, re-run Phase 2 with it picked" in note

	def test_a_tracked_apps_module_call_is_yours(self):
		note = g.hot_line_gate(_hot_line("\t\tmyapp.pricing.compute(d)", per_hit_us=4000.0), tracked_apps=("myapp",))
		assert "Re-run Phase 2 with myapp.pricing.compute picked" in note


class TestFrameworkGate:
	def test_erpnext_file_is_gated_even_for_pure_python(self):
		note = g.hot_line_gate(_hot_line("x = 1", file="/home/b/apps/erpnext/erpnext/controllers/selling_controller.py"))
		assert note.startswith("This line is in framework or library code (erpnext/erpnext/controllers/")

	def test_untracked_app_is_framework_in_inclusion_mode(self):
		note = g.hot_line_gate(_hot_line("x = 1", file="/home/b/apps/otherapp/otherapp/y.py"), tracked_apps=("myapp",))
		assert note.startswith("This line is in framework or library code")

	def test_site_packages_is_gated(self):
		note = g.hot_line_gate(_hot_line("x = 1", file="/venv/lib/python3.14/site-packages/pandas/core/frame.py"))
		assert note.startswith("This line is in framework or library code")

	def test_tracked_user_app_is_not_gated(self):
		assert g.hot_line_gate(_hot_line("x = 1"), tracked_apps=("myapp",)) is None


def test_other_finding_types_are_never_gated():
	assert g.hot_line_gate({"finding_type": "N+1 Query", "technical_detail": {
		"file": _USER_FILE, "line_content": "super().validate()", "per_hit_us": 99999.0,
	}}) is None


def test_notes_have_no_dash_or_backticks():
	for finding in (
		_hot_line("super().validate()", per_hit_us=132809.5),
		_hot_line("json.loads(blob)", per_hit_us=250000.0),
		_hot_line("x = 1", file="/home/b/apps/erpnext/erpnext/x.py"),
	):
		note = g.hot_line_gate(finding)
		assert "\u2014" not in note and "\u2013" not in note and "`" not in note


@pytest.mark.parametrize("stamp,old", [(None, True), ("innermost_first", True), ("outermost_first", False)])
@pytest.mark.parametrize("serialized", [False, True])
def test_callsite_stamp_is_exact_in_both_finding_shapes(stamp, old, serialized):
	detail = {} if stamp is None else {"callsite_walk": stamp}
	finding = {"finding_type": "Redundant Call"}
	finding["technical_detail_json" if serialized else "technical_detail"] = json.dumps(detail) if serialized else detail
	assert g.analyzed_before_callsite_fix(finding) is old


@pytest.mark.parametrize("detail", [None, "invalid json", "[]", "null", "12"])
def test_unreadable_callsite_stamp_is_conservative(detail):
	assert g.analyzed_before_callsite_fix({"finding_type": "Redundant Call", "technical_detail_json": detail})
	assert not g.analyzed_before_callsite_fix({"finding_type": "Slow Query", "technical_detail_json": detail})
