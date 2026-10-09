# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The AI fix prompt is grounded on the whole enclosing function when it fits
80 lines (ai_grounding.grounding_window), else on the historical
+/-24-line window, which is also what unparseable, module-level or oversized
code gets. The fallback preserves the previous window size."""

import json
import types

from optimus import ai_grounding, analyze


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
	payload = analyze._ai_payload_for_finding(_row(str(src), 6, "small_fn"), {})
	assert payload["loop_facts"]["loops"][0]["line"] == 5  # from the whole file's tree


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
	assert [r["lineno"] for r in ai_grounding.grounding_window(lines, 5, 24, 24).rows] == [1, 2, 3, 4, 5, 6]
	assert ai_grounding.grounding_window(lines, 99, 24, 24).rows == []


def test_grounding_window_reads_through_the_real_helpers_and_parses_once(tmp_path, monkeypatch):
	"""T3 + PF3: analyze._ai_grounding_window over the real _source_lines and
	grounding_window; two findings of one file share a single whole-file parse."""
	src = tmp_path / "mod.py"
	src.write_text(
		"def f(items):\n\tfor d in items:\n\t\ta = frappe.get_doc('A', d)\n\t\tb = frappe.get_doc('B', d)\n"
	)
	parses = []
	real_parse = ai_grounding.ast.parse
	monkeypatch.setattr(ai_grounding.ast, "parse", lambda *a, **k: parses.append(1) or real_parse(*a, **k))
	cache = {}
	first = analyze._ai_grounding_window(str(src), 3, cache)
	second = analyze._ai_grounding_window(str(src), 4, cache)
	assert [r["lineno"] for r in first.rows] == [1, 2, 3, 4]
	assert [r["lineno"] for r in second.rows if r["is_target"]] == [4]
	assert first.tree is second.tree and first.parent is second.parent
	assert len(parses) == 1


def test_a_changed_line_list_in_the_cache_is_parsed_again(tmp_path):
	src = tmp_path / "mod.py"
	src.write_text("def f():\n\treturn 1\n")
	cache = {}
	first = analyze._ai_grounding_window(str(src), 2, cache)
	cache[str(src)] = ["def g():", "\treturn 2"]
	second = analyze._ai_grounding_window(str(src), 2, cache)
	assert second.tree is not first.tree and second.rows[0]["content"] == "def g():"


def test_the_parse_failure_of_a_file_is_remembered_too(tmp_path, monkeypatch):
	src = tmp_path / "srv.py"
	src.write_text("return 1 +\n" * 3)
	parses = []
	real_parse = ai_grounding.ast.parse
	monkeypatch.setattr(ai_grounding.ast, "parse", lambda *a, **k: parses.append(1) or real_parse(*a, **k))
	cache = {}
	analyze._ai_grounding_window(str(src), 1, cache)
	analyze._ai_grounding_window(str(src), 2, cache)
	assert len(parses) == 1


def test_form_feed_and_unicode_separators_keep_python_line_numbers(tmp_path):
	"""E4: str.splitlines would split on the form feed and U+2028 and shift every line."""
	src = tmp_path / "ff.py"
	src.write_text(
		"import frappe\n\x0c\ndef f(xs):\n\tnote = 'a\u2028b'\n\tfor x in xs:\n"
		"\t\tfrappe.db.get_value('A', x)\n",
		encoding="utf-8",
	)
	window = analyze._ai_grounding_window(str(src), 6, {})
	assert [r["lineno"] for r in window.rows if r["is_target"]] == [6]
	target = next(r for r in window.rows if r["is_target"])
	assert "frappe.db.get_value" in target["content"]
	assert ai_grounding.loop_facts_from_tree(window.tree, 6, parent=window.parent)["loops"][0]["line"] == 5


def test_the_tree_memo_keeps_only_the_last_few_files(tmp_path):
	"""F4: a long run does not keep every file's tree and parent map alive."""
	cache = {}
	for i in range(analyze._AI_AST_MEMO_MAX + 3):
		src = tmp_path / f"m{i}.py"
		src.write_text("def f():\n\treturn 1\n")
		analyze._ai_grounding_window(str(src), 2, cache)
	memo = cache[("optimus_ast",)]
	assert len(memo) == analyze._AI_AST_MEMO_MAX
	assert str(tmp_path / "m0.py") not in memo and str(tmp_path / f"m{analyze._AI_AST_MEMO_MAX + 2}.py") in memo


def test_two_findings_of_one_file_build_the_parent_map_once(tmp_path, monkeypatch):
	src = tmp_path / "mod.py"
	src.write_text("def f(items):\n\tfor d in items:\n\t\tfrappe.get_doc('A', d)\n\t\tfrappe.get_doc('B', d)\n")
	built = []
	real = ai_grounding._parent_map
	monkeypatch.setattr(ai_grounding, "_parent_map", lambda tree: built.append(1) or real(tree))
	cache = {}
	for line in (3, 4):
		analyze._ai_payload_for_finding(_row(str(src), line, "f", finding_type="N+1 Query"), cache)
	assert len(built) == 1


def test_the_tree_memo_is_a_true_lru(tmp_path, monkeypatch):
	"""Visit A B C D A E: the revisit of A keeps it, so E evicts B; only B parses again."""
	parsed = []
	real = ai_grounding.parse_source
	monkeypatch.setattr(ai_grounding, "parse_source", lambda lines: parsed.append(lines[0]) or real(lines))
	files = {}
	for name in "ABCDE":
		files[name] = tmp_path / f"{name}.py"
		files[name].write_text(f"# {name}\ndef f():\n\treturn 1\n")
	cache = {}

	def visit(names):
		parsed.clear()
		for name in names:
			analyze._ai_grounding_window(str(files[name]), 3, cache)
		return list(parsed)

	assert visit("ABCDAE") == ["# A", "# B", "# C", "# D", "# E"]
	assert visit("A") == []
	assert visit("B") == ["# B"]
