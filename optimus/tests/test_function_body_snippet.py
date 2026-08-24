# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""_expand_self_time_snippets: narrow a no-deeper-frame Slow Hot Path snippet to the decorator(s)+def, flagged for a Line-Level Drilldown note."""

from optimus import renderer
from optimus.renderer import finding_enrichment as fe


def _func_finding(filename, def_lineno, *, drilldown_chain, finding_type="Slow Hot Path"):
	return {
		"finding_type": finding_type,
		"technical_detail": {
			"callsite": {
				"filename": filename,
				"lineno": def_lineno,
				"function": "bg_recheck_users",
				"source_snippet": [{"lineno": def_lineno, "content": "def x():"}],  # the ±2 stand-in
			},
			"drilldown_chain": drilldown_chain,
		},
	}


def _snippet(finding):
	return finding["technical_detail"]["callsite"]["source_snippet"]


_SAMPLE = (
	"import os\n"                       # 1
	"\n"                                 # 2
	"def bg_recheck_users(doc=None):\n"  # 3  <- def
	"    for i in range(15):\n"          # 4
	"        try:\n"                      # 5
	"            do_work(i)\n"            # 6
	"        except Exception:\n"         # 7
	"            pass\n"                  # 8
	"    return None\n"                  # 9
	"\n"                                 # 10
	"def next_function():\n"             # 11
	"    pass\n"                          # 12
)


def _callsite(finding):
	return finding["technical_detail"]["callsite"]


class TestExpandSelfTimeSnippets:
	def test_empty_chain_self_time_narrows_to_def_and_flags(self, tmp_path):
		src = tmp_path / "m.py"
		src.write_text(_SAMPLE)
		f = _func_finding(str(src), 3, drilldown_chain=[])
		renderer._expand_self_time_snippets([f], file_cache=None)
		# Narrowed to just the def/signature line (Phase-1 can't pinpoint a line).
		snippet = _snippet(f)
		assert len(snippet) == 1 and snippet[0]["lineno"] == 3
		# Flag set so the card renders the "run a Line-Level Drilldown" note.
		assert _callsite(f)["self_time_no_pinpoint"] is True

	def test_non_empty_chain_left_unchanged(self, tmp_path):
		src = tmp_path / "m.py"
		src.write_text(_SAMPLE)
		f = _func_finding(str(src), 3, drilldown_chain=[{"function": "inner", "lineno": 6}])
		renderer._expand_self_time_snippets([f], file_cache=None)
		assert _snippet(f) == [{"lineno": 3, "content": "def x():"}]  # untouched
		assert "self_time_no_pinpoint" not in _callsite(f)

	def test_other_finding_types_left_unchanged(self, tmp_path):
		src = tmp_path / "m.py"
		src.write_text(_SAMPLE)
		f = _func_finding(str(src), 3, drilldown_chain=[], finding_type="N+1 Query")
		renderer._expand_self_time_snippets([f], file_cache=None)
		assert _snippet(f) == [{"lineno": 3, "content": "def x():"}]
		assert "self_time_no_pinpoint" not in _callsite(f)

	def test_unreadable_file_still_flags_and_keeps_snippet(self):
		# Body can't be read → existing snippet stays, but it's still a self-time
		# finding, so the note flag is set regardless.
		f = _func_finding("/nonexistent/x.py", 3, drilldown_chain=[])
		renderer._expand_self_time_snippets([f], file_cache=None)
		assert _snippet(f) == [{"lineno": 3, "content": "def x():"}]
		assert _callsite(f)["self_time_no_pinpoint"] is True

	def test_decorated_function_shows_decorator_and_def(self, tmp_path):
		# pyinstrument anchors a decorated function on its FIRST decorator line
		# (CPython 3.11+), so the recorded lineno (3) is `@frappe.whitelist()`,
		# not the `def`. The card must show the decorator AND the def below it,
		# with the highlight + callsite lineno moved onto the def (option A).
		src = tmp_path / "m.py"
		src.write_text(
			"import frappe\n"                  # 1
			"\n"                               # 2
			"@frappe.whitelist()\n"            # 3  <- recorded (decorator) line
			"def calculate_discount(doc):\n"   # 4  <- real def
			"    return 1\n"                   # 5
		)
		f = _func_finding(str(src), 3, drilldown_chain=[])
		renderer._expand_self_time_snippets([f], file_cache=None)
		snippet = _snippet(f)
		assert [r["lineno"] for r in snippet] == [3, 4]
		assert snippet[0]["content"] == "@frappe.whitelist()"
		assert snippet[1]["content"] == "def calculate_discount(doc):"
		# highlight + card file:line move to the def line
		assert _callsite(f)["lineno"] == 4
		assert _callsite(f)["self_time_no_pinpoint"] is True

	def test_multiple_decorators_span_to_def(self, tmp_path):
		# Two decorators above the def: both are shown, def is the anchor.
		src = tmp_path / "m.py"
		src.write_text(
			"@frappe.whitelist()\n"            # 1
			"@another_decorator\n"             # 2
			"def handler():\n"                 # 3
			"    return 1\n"                   # 4
		)
		f = _func_finding(str(src), 1, drilldown_chain=[])
		renderer._expand_self_time_snippets([f], file_cache=None)
		assert [r["lineno"] for r in _snippet(f)] == [1, 2, 3]
		assert _callsite(f)["lineno"] == 3

	def test_out_of_range_lineno_keeps_existing_snippet(self, tmp_path):
		# lineno past EOF → helper returns (None, ln) → snippet untouched, but
		# it's still a self-time finding so the note flag is set.
		src = tmp_path / "m.py"
		src.write_text("def x():\n    pass\n")
		f = _func_finding(str(src), 999, drilldown_chain=[])
		renderer._expand_self_time_snippets([f], file_cache=None)
		assert _snippet(f) == [{"lineno": 999, "content": "def x():"}]  # untouched
		assert _callsite(f)["self_time_no_pinpoint"] is True


# The pure snippet-shaping helper, exercised directly across the header shapes a
# strict reviewer will ask about. ``_decorator_through_def_rows`` returns
# ``(rows, def_lineno)``; ``rows`` is None only when the file can't be read.
class TestDecoratorThroughDefRows:
	def _rows(self, tmp_path, text, lineno):
		src = tmp_path / "m.py"
		src.write_text(text)
		return fe._decorator_through_def_rows(str(src), lineno, cache=None)

	def test_undecorated_def_anchor_single_line(self, tmp_path):
		rows, dl = self._rows(tmp_path, "def foo():\n    return 1\n", 1)
		assert [r["lineno"] for r in rows] == [1]
		assert dl == 1

	def test_single_decorator(self, tmp_path):
		rows, dl = self._rows(
			tmp_path, "@frappe.whitelist()\ndef foo():\n    return 1\n", 1
		)
		assert [r["content"] for r in rows] == ["@frappe.whitelist()", "def foo():"]
		assert dl == 2

	def test_multiline_decorator_args_span_to_def(self, tmp_path):
		# A decorator whose args wrap across lines — every continuation line is
		# included through the def.
		text = (
			"@frappe.whitelist(\n"      # 1
			'    methods=["POST"],\n'   # 2
			")\n"                       # 3
			"def foo():\n"              # 4
			"    return 1\n"            # 5
		)
		rows, dl = self._rows(tmp_path, text, 1)
		assert [r["lineno"] for r in rows] == [1, 2, 3, 4]
		assert dl == 4

	def test_comment_between_decorator_and_def(self, tmp_path):
		text = (
			"@frappe.whitelist()\n"  # 1
			"# a note\n"             # 2
			"def foo():\n"           # 3
			"    return 1\n"         # 4
		)
		rows, dl = self._rows(tmp_path, text, 1)
		assert [r["lineno"] for r in rows] == [1, 2, 3]
		assert dl == 3

	def test_async_def(self, tmp_path):
		rows, dl = self._rows(
			tmp_path, "@deco\nasync def foo():\n    return 1\n", 1
		)
		assert [r["content"] for r in rows] == ["@deco", "async def foo():"]
		assert dl == 2

	def test_indented_method_decorator(self, tmp_path):
		# Decorator + def indented inside a class body.
		text = (
			"class C:\n"                 # 1
			"    @frappe.whitelist()\n"  # 2  <- recorded
			"    def m(self):\n"         # 3
			"        return 1\n"         # 4
		)
		rows, dl = self._rows(tmp_path, text, 2)
		assert [r["lineno"] for r in rows] == [2, 3]
		assert dl == 3

	def test_class_before_def_is_not_grabbed(self, tmp_path):
		# A decorated class (no def in its header) must NOT sweep up the first
		# method's def — fall back to the single recorded line.
		text = (
			"@register\n"        # 1  <- recorded
			"class Widget:\n"    # 2
			"    def m(self):\n" # 3
			"        return 1\n" # 4
		)
		rows, dl = self._rows(tmp_path, text, 1)
		assert [r["lineno"] for r in rows] == [1]
		assert dl == 1

	def test_body_line_anchor_does_not_grab_next_function(self, tmp_path):
		# A non-header recorded line (neither def nor decorator) must show just
		# itself, never the following function's def.
		text = (
			"def a():\n"      # 1
			"    x = 1\n"     # 2  <- recorded (body line)
			"def b():\n"      # 3
			"    return 1\n"  # 4
		)
		rows, dl = self._rows(tmp_path, text, 2)
		assert [r["lineno"] for r in rows] == [2]
		assert dl == 2

	def test_no_def_within_window_falls_back(self, tmp_path):
		# A decorator with no def within the block cap → single recorded line.
		text = "@deco\n" + "".join(f"# filler {i}\n" for i in range(30)) + "def foo():\n"
		rows, dl = self._rows(tmp_path, text, 1)
		assert [r["lineno"] for r in rows] == [1]
		assert dl == 1

	def test_long_line_truncated(self, tmp_path):
		long_def = "def foo(" + ", ".join(f"a{i}" for i in range(200)) + "):\n"
		rows, dl = self._rows(tmp_path, "@deco\n" + long_def + "    pass\n", 1)
		assert dl == 2
		assert rows[1]["content"].endswith("...")

	def test_unreadable_returns_none_rows(self):
		rows, dl = fe._decorator_through_def_rows("/nonexistent/x.py", 3, cache=None)
		assert rows is None and dl == 3

	def test_bad_lineno_returns_none_rows(self, tmp_path):
		src = tmp_path / "m.py"
		src.write_text("def x():\n    pass\n")
		assert fe._decorator_through_def_rows(str(src), None, cache=None) == (None, None)
		assert fe._decorator_through_def_rows(str(src), 0, cache=None) == (None, 0)

	def test_idempotent_when_re_fed_the_def_line(self, tmp_path):
		# Feeding the def line (as a re-render on a CPython<=3.10 anchor would)
		# still yields the function name — never a crash or an empty snippet.
		text = "@frappe.whitelist()\ndef foo():\n    return 1\n"
		rows, dl = self._rows(tmp_path, text, 2)  # 2 == the def line
		assert [r["content"] for r in rows] == ["def foo():"]
		assert dl == 2


class TestFindHeaderDefLine:
	def test_def_line_returns_itself(self):
		assert fe._find_header_def_line(["def foo():", "    pass"], 1) == 1

	def test_decorator_scans_to_def(self):
		assert fe._find_header_def_line(["@deco", "def foo():"], 1) == 2

	def test_non_header_returns_none(self):
		assert fe._find_header_def_line(["x = 1", "def foo():"], 1) is None

	def test_class_before_def_returns_none(self):
		assert fe._find_header_def_line(["@deco", "class C:", "    def m(self):"], 1) is None
