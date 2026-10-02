# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The AI fix prompt is grounded on the whole enclosing function when it fits
80 lines (fix_recipes.enclosing_function_window), else on the historical
+/-24-line window, which is also what unparseable, module-level or oversized
code gets. The fallback preserves the previous window size."""

import json
import types

from optimus import analyze
from optimus.renderer import fix_recipes


def _row(filename, lineno, function, finding_type="Redundant Call"):
	return types.SimpleNamespace(
		finding_type=finding_type, severity="Medium", title="x", customer_description="y",
		estimated_impact_ms=90.0, affected_count=150, action_ref="0", llm_fix_json=None,
		technical_detail_json=json.dumps(
			{"callsite": {"filename": filename, "lineno": lineno, "function": function}},
		),
	)


def test_uses_whole_enclosing_function(tmp_path):
	src = tmp_path / "mod.py"
	src.write_text(
		"import frappe\n\n\n"
		"def small_fn(doc):\n"
		"\tfor i in range(5):\n"
		"\t\tuser = frappe.get_doc(\"User\", doc.owner)\n"
		"\t\tuser.check_permission(\"read\")\n\n\n"
		"def other():\n"
		"\treturn 1\n"
	)
	window = analyze._ai_payload_for_finding(_row(str(src), 6, "small_fn"), {})["source_window"]
	assert [r["lineno"] for r in window] == [4, 5, 6, 7]
	assert [r["lineno"] for r in window if r["is_target"]] == [6]


def test_too_big_function_falls_back_to_49_lines(tmp_path):
	src = tmp_path / "big.py"
	src.write_text("\n".join(["def big():"] + [f"\tx = {i}" for i in range(200)] + ["\treturn x"]) + "\n")
	window = analyze._ai_payload_for_finding(_row(str(src), 100, "big"), {})["source_window"]
	assert (len(window), window[0]["lineno"], window[-1]["lineno"]) == (49, 76, 124)


def test_unparseable_falls_back_to_49_lines(tmp_path):
	# A Server Script body: top-level code with a bare return does not parse.
	src = tmp_path / "srv.py"
	src.write_text(
		"\n".join([f"x{i} = {i}" for i in range(60)] + ["return x1"] + [f"y{i} = {i}" for i in range(60)]) + "\n"
	)
	window = analyze._ai_payload_for_finding(_row(str(src), 61, "?"), {})["source_window"]
	assert (len(window), window[0]["lineno"], window[-1]["lineno"]) == (49, 37, 85)


def test_unreadable_file_has_no_window():
	payload = analyze._ai_payload_for_finding(_row("/nonexistent/path/nope.py", 5, "x"), {})
	assert not payload.get("source_window")


def test_applies_to_any_ai_eligible_type(tmp_path):
	src = tmp_path / "mod.py"
	src.write_text("def small_fn(doc):\n\treturn doc.owner\n")
	window = analyze._ai_payload_for_finding(
		_row(str(src), 2, "small_fn", finding_type="Slow Query"), {},
	)["source_window"]
	assert window[0]["content"].startswith("def small_fn")


def test_long_line_truncated_like_read_source_window(tmp_path):
	src = tmp_path / "mod.py"
	src.write_text("def f():\n\tx = '" + "a" * 400 + "'\n")
	window = analyze._ai_payload_for_finding(_row(str(src), 2, "f"), {})["source_window"]
	assert window[1]["content"].endswith("...") and len(window[1]["content"]) == 203


def test_decorators_and_the_outer_function_are_included():
	lines = [
		"@frappe.whitelist()",
		"def outer(items):",
		"\tfor d in items:",
		"\t\tdef inner():",
		"\t\t\treturn frappe.get_doc('Item', d)",
		"\t\tinner()",
	]
	window = fix_recipes.enclosing_function_window(lines, 5)
	assert [r["lineno"] for r in window] == [1, 2, 3, 4, 5, 6]
	assert fix_recipes.enclosing_function_window(lines, 99) == []
