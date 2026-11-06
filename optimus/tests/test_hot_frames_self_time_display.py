# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Hot frames: the user table shows Total and Self time, the framework table is unchanged.

Stored rows come in three shapes: reports up to 0.7.0 (only ``total_ms`` = cumulative),
reports from 2026-05-20 to 0.12.142 (``total_ms`` 0 + ``total_cumulative_ms``) and rows
from 0.12.146 on (``self_time_measured``, ``total_ms`` = self, ``total_cumulative_ms`` = Total).
"""

import json
import re

import pytest

from optimus import renderer
from optimus.analyzers import call_tree
from optimus.renderer.call_tree_renderer import _render_call_tree_node

LEGACY_NOTE = "Self time was not recorded for this report. Record the flow again with the current version"
DASH_TITLE = "Self time not recorded for this report"

GEN_A = {
	"function": "a/b.py::gen_a",
	"total_ms": 600,
	"occurrences": 3,
	"distinct_actions": 2,
	"is_framework": False,
}
GEN_B = {
	"function": "a/b.py::gen_b",
	"total_ms": 0,
	"total_cumulative_ms": 500,
	"occurrences": 4,
	"distinct_actions": 2,
	"is_framework": False,
}
NEW = {
	"function": "a/b.py::new_fn",
	"total_ms": 120,
	"total_cumulative_ms": 700,
	"self_time_measured": True,
	"occurrences": 5,
	"distinct_actions": 3,
	"is_framework": False,
}
NEW_ZERO_SELF = {
	"function": "a/b.py::wrapper",
	"total_ms": 0,
	"total_cumulative_ms": 300,
	"self_time_measured": True,
	"occurrences": 2,
	"distinct_actions": 1,
	"is_framework": False,
}
FRAMEWORK = {
	"function": "c/d.py::fw_fn",
	"total_ms": 0,
	"total_cumulative_ms": 900,
	"occurrences": 6,
	"distinct_actions": 2,
	"is_framework": True,
}


def _html(rows):
	from optimus.tests.test_v5_panels_render import _fake_session_doc

	doc = _fake_session_doc(
		{
			"infra_timeline": [],
			"infra_summary": {},
			"frontend_xhr_matched": [],
			"frontend_vitals_by_page": {},
			"frontend_orphans": [],
			"frontend_summary": {},
		}
	)
	doc.hot_frames_json = json.dumps(rows)
	return renderer.render(doc, recordings=[])


def _table(html, heading_id):
	"""The ``<table>`` that follows the given heading id."""
	start = html.index(f'id="{heading_id}"')
	table_start = html.index("<table", start)
	return html[table_start : html.index("</table>", table_start)]


def _row(table, name):
	match = re.search(rf"<tr[^>]*>(?:(?!</tr>).)*{re.escape(name)}.*?</tr>", table, re.S)
	assert match, name
	return match.group(0)


def _cells(row):
	return [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]


class TestBuilder:
	def test_gen_a_row_total_is_total_ms_and_self_unavailable(self):
		out = renderer.build_hot_frames_table([GEN_A], is_hot=True)
		assert out[0]["total_ms"] == 600
		assert out[0]["self_ms"] is None

	def test_gen_b_row_total_reads_cumulative_never_zero(self):
		out = renderer.build_hot_frames_table([GEN_B], is_hot=True)
		assert out[0]["total_ms"] == 500
		assert out[0]["self_ms"] is None

	def test_new_row_has_both_values(self):
		out = renderer.build_hot_frames_table([NEW], is_hot=True)
		assert out[0]["total_ms"] == 700
		assert out[0]["self_ms"] == 120

	def test_measured_zero_self_stays_zero_not_none(self):
		out = renderer.build_hot_frames_table([NEW_ZERO_SELF], is_hot=True)
		assert out[0]["self_ms"] == 0
		assert out[0]["total_ms"] == 300

	def test_user_rows_sort_by_total(self):
		out = renderer.build_hot_frames_table([GEN_B, NEW, GEN_A, NEW_ZERO_SELF], is_hot=True)
		assert [r["total_ms"] for r in out] == [700, 600, 500, 300]

	def test_user_rows_sort_by_self_when_every_row_has_it(self):
		low_self_high_total = {**NEW, "function": "a/b.py::low_self", "total_ms": 50, "total_cumulative_ms": 900}
		out = renderer.build_hot_frames_table([low_self_high_total, NEW, NEW_ZERO_SELF], is_hot=True)
		assert [r["self_ms"] for r in out] == [120, 50, 0]

	def test_user_rows_sort_by_total_when_any_row_lacks_self(self):
		low_self_high_total = {**NEW, "function": "a/b.py::low_self", "total_ms": 50, "total_cumulative_ms": 900}
		out = renderer.build_hot_frames_table([NEW, low_self_high_total, GEN_A], is_hot=True)
		assert [r["total_ms"] for r in out] == [900, 700, 600]

	def test_framework_rows_unchanged(self):
		out = renderer.build_hot_frames_table(
			[FRAMEWORK, {**FRAMEWORK, "function": "c/d.py::small", "total_cumulative_ms": 100}], is_hot=False
		)
		assert [r["total_ms"] for r in out] == [900, 100]
		assert all(r["is_hot"] is False for r in out)


class TestRendered:
	def test_user_table_has_five_columns_and_framework_four(self):
		html = _html([NEW, FRAMEWORK])
		user = _table(html, "hot-frames-app")
		fw = _table(html, "hot-frames-framework")
		assert len(re.findall(r"<th[ >]", user)) == 5
		assert len(re.findall(r"<th[ >]", fw)) == 4
		assert "Self time" in user and "Self time" not in fw

	def test_header_tooltips(self):
		user = _table(_html([NEW]), "hot-frames-app")
		assert "recursion counted once" in user
		assert "time in the function's own lines" in user.lower()

	def test_new_row_shows_total_and_self(self):
		user = _table(_html([NEW]), "hot-frames-app")
		cells = _cells(_row(user, "new_fn"))
		assert cells[:3] == ["a/b.py::new_fn", "700ms", "120ms"]

	def test_measured_zero_self_shows_0ms(self):
		cells = _cells(_row(_table(_html([NEW_ZERO_SELF]), "hot-frames-app"), "wrapper"))
		assert cells[1:3] == ["300ms", "0ms"]

	def test_gen_a_row_shows_total_and_a_dash(self):
		html = _html([GEN_A])
		cells = _cells(_row(_table(html, "hot-frames-app"), "gen_a"))
		assert cells[1:3] == ["600ms", "- not recorded"]
		assert DASH_TITLE in html
		assert '<span class="sr-only"> not recorded</span>' in html
		assert ".sr-only" in html

	def test_gen_b_row_shows_total_never_0ms(self):
		cells = _cells(_row(_table(_html([GEN_B]), "hot-frames-app"), "gen_b"))
		assert cells[1:3] == ["500ms", "- not recorded"]
		assert "0ms" not in cells[1:3]

	def test_legacy_note_only_when_self_is_unavailable(self):
		assert LEGACY_NOTE in _html([GEN_A, NEW])
		assert LEGACY_NOTE in _html([GEN_B])
		assert LEGACY_NOTE not in _html([NEW, NEW_ZERO_SELF])
		assert LEGACY_NOTE not in _html([FRAMEWORK])

	def test_legacy_note_says_the_list_is_ordered_by_total(self):
		html = _html([GEN_A, GEN_B])
		assert "ordered by Total time, so it may not be the true top 20" in html

	def test_recursion_tooltip_only_when_self_time_was_measured(self):
		new = _table(_html([NEW]), "hot-frames-app")
		assert "recursion counted once" in new
		for rows in ([GEN_A], [GEN_B], [GEN_A, NEW]):
			assert "recursion counted once" not in _table(_html(rows), "hot-frames-app")

	def test_user_rows_render_in_self_order_when_measured(self):
		low = {**NEW, "function": "a/b.py::low_self", "total_ms": 20, "total_cumulative_ms": 900}
		user = _table(_html([low, NEW]), "hot-frames-app")
		assert user.index("new_fn") < user.index("low_self")

	def test_legacy_note_shown_once(self):
		assert _html([GEN_A, GEN_B]).count(LEGACY_NOTE) == 1

	def test_framework_table_values_and_note(self):
		html = _html([NEW, FRAMEWORK])
		fw = _table(html, "hot-frames-framework")
		assert _cells(_row(fw, "fw_fn")) == ["c/d.py::fw_fn", "900ms", "6", "2"]
		assert "would round to 0ms" not in html

	def test_user_rows_render_in_total_order(self):
		user = _table(_html([GEN_B, NEW, GEN_A]), "hot-frames-app")
		assert user.index("new_fn") < user.index("gen_a") < user.index("gen_b")


class TestMobileScroll:
	def test_each_hot_frames_table_sits_in_a_scroll_box(self):
		html = _html([NEW, FRAMEWORK])
		for heading_id in ("hot-frames-app", "hot-frames-framework"):
			before = html[: html.index("<table", html.index(f'id="{heading_id}"'))]
			assert before.rstrip().endswith('<div class="hot-frames-scroll">')
		assert html.count('class="hot-frames-scroll"') == 2

	def test_scroll_box_css_scrolls_on_screen_and_not_in_print(self):
		html = _html([NEW])
		assert re.search(r"\.hot-frames-scroll\s*\{[^}]*overflow-x:\s*auto", html)
		assert re.search(r"@media print\s*\{\s*\.hot-frames-scroll\s*\{\s*overflow-x:\s*visible", html)


class TestSelfNodesAreGone:
	def _tree(self):
		def fn(name, own_ms, kids=()):
			selfs = [_Frame("[self]", own_ms / 2000.0) for _ in range(2)]
			return _Frame(name, own_ms / 1000.0 + sum(k.time for k in kids), selfs + list(kids))

		root = fn("<module>", 0, [fn("heavy", 300, [fn("helper", 100)])])
		return call_tree._pyi_to_dict_tree(_Session(root))

	def test_drilldown_chain_has_no_self_step(self):
		chain = renderer._walk_drilldown_chain(
			self._tree(), {"filename": "my_app/work.py", "function": "heavy"}
		)
		assert [level["function"] for level in chain] == ["helper"]

	def test_call_tree_has_no_self_row_and_shows_the_self_label(self):
		tree = self._tree()
		html = _render_call_tree_node(tree["children"][0], parent_ms=400)
		assert "[self]" not in html
		assert "self 300ms" in html


class _Frame:
	def __init__(self, function, time_s, children=()):
		self.function = function
		self.file_path_short = "my_app/work.py"
		self.line_no = 1
		self.time = time_s
		self.children = list(children)


class _Session:
	def __init__(self, root):
		self._root = root

	def root_frame(self):
		return self._root


@pytest.mark.parametrize("row", [GEN_A, GEN_B])
def test_legacy_rows_never_render_a_zero_total(row):
	cells = _cells(_row(_table(_html([row]), "hot-frames-app"), row["function"].split("::")[1]))
	assert cells[1] != "0ms"
