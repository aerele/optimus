# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The 'single hottest line' (Hot Line) finding shows the hot line with
surrounding context, like call-tree findings.

``_finding_to_dict`` reads a ±2 window around the hot line while keeping the
profiled line's text authoritative, falling back to the single stored line when
the file can't be read at render.
"""

import json
import types

from optimus import renderer


def _hot_line_row(file, lineno, line_content):
	row = types.SimpleNamespace()
	row.finding_type = "Hot Line"
	row.severity = "High"
	row.title = f"myapp.mod.func:{lineno} consumed 378ms (100 hits) single hottest line"
	row.customer_description = "the dominant time sink"
	row.estimated_impact_ms = 378.0
	row.affected_count = 100
	row.action_ref = "0"
	row.technical_detail_json = json.dumps({
		"dotted_path": "myapp.mod.func",
		"file": file,
		"lineno": lineno,
		"line_content": line_content,
		"total_ms": 378.0,
		"hits": 100,
	})
	return row


def _snippet(row):
	return renderer._finding_to_dict(row)["technical_detail"]["callsite"]["source_snippet"]


class TestHotLineSnippetContext:
	def test_shows_pm2_context_window(self, tmp_path):
		src = tmp_path / "mod.py"
		src.write_text("l1\nl2\nl3_hot\nl4\nl5\n")
		snippet = _snippet(_hot_line_row(str(src), 3, "l3_hot"))
		assert [r["lineno"] for r in snippet] == [1, 2, 3, 4, 5]
		hot = next(r for r in snippet if r["lineno"] == 3)
		assert hot["content"] == "l3_hot"

	def test_profiled_line_content_is_authoritative(self, tmp_path):
		"""If the file's hot line drifted since the run, the profiled text wins
		for the hot line; context lines still come from the current file."""
		src = tmp_path / "mod.py"
		src.write_text("a\nb\nFILE_VERSION\nd\ne\n")
		snippet = _snippet(_hot_line_row(str(src), 3, "PROFILED_TEXT"))
		hot = next(r for r in snippet if r["lineno"] == 3)
		assert hot["content"] == "PROFILED_TEXT"
		assert any(r["content"] == "b" for r in snippet)  # context from file
		assert any(r["content"] == "d" for r in snippet)

	def test_unreadable_file_falls_back_to_single_line(self):
		snippet = _snippet(_hot_line_row("/nonexistent/zzz.py", 20, "user = frappe.get_doc(...)"))
		assert snippet == [{"lineno": 20, "content": "user = frappe.get_doc(...)"}]

	def test_out_of_range_lineno_falls_back_to_single_line(self, tmp_path):
		src = tmp_path / "mod.py"
		src.write_text("a\nb\nc\n")
		snippet = _snippet(_hot_line_row(str(src), 99, "PROFILED"))
		assert snippet == [{"lineno": 99, "content": "PROFILED"}]
