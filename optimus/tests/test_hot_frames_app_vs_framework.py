# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Hot frames are split into your app vs framework from the full file path."""

from optimus.analyzers import call_tree
from optimus.renderer import _internal as renderer

APP_FILE = "/bench/apps/myapp/myapp/sales/invoice.py"
FRAPPE_FILE = "/bench/apps/frappe/frappe/core/doctype/user/user.py"
OPTIMUS_FILE = "/bench/apps/optimus/optimus/capture.py"
SITE_PKG_FILE = "/env/lib/python3.12/site-packages/foo/bar.py"


def _n(function, filename, self_ms, children=()):
	return {
		"function": function, "filename": filename, "lineno": 1, "kind": "python",
		"cumulative_ms": self_ms, "self_ms": self_ms, "children": list(children),
	}


def _board(files, tracked=None, installed=None):
	tree = _n("<root>", "", 0, [_n(f"fn{i}", f, 100 + i) for i, f in enumerate(files)])
	_, board = call_tree._aggregate_hot_frames([tree], tracked, installed)
	return {r["function"].split("::")[0]: r["is_framework"] for r in board}


def _split(files, tracked=None, installed=None):
	tree = _n("<root>", "", 0, [_n(f"fn{i}", f, 100 + i) for i, f in enumerate(files)])
	_, board = call_tree._aggregate_hot_frames([tree], tracked, installed)
	custom, framework = renderer._split_hot_frames(board, tracked or ())
	return {r["function"] for r in custom}, {r["function"] for r in framework}


class TestAnalyzerFlag:
	def test_exclusion_mode_classifies_from_full_path(self):
		flags = _board([APP_FILE, FRAPPE_FILE], installed=frozenset({"myapp", "frappe"}))
		assert flags == {"sales/invoice.py": False, "user/user.py": True}

	def test_inclusion_mode_tracked_app_is_custom(self):
		# Short key "sales/invoice.py" would be misread as a non-tracked app.
		flags = _board([APP_FILE, FRAPPE_FILE], tracked=("myapp",))
		assert flags["sales/invoice.py"] is False

	def test_optimus_never_custom_even_if_tracked(self):
		fn = call_tree._is_framework_frame_file
		assert fn(OPTIMUS_FILE, None, frozenset({"optimus"})) is True
		assert fn(OPTIMUS_FILE, ("optimus",), None) is True

	def test_site_packages_is_framework(self):
		assert call_tree._is_framework_frame_file(SITE_PKG_FILE, None, None) is True
		assert call_tree._is_framework_frame_file(SITE_PKG_FILE, ("myapp",), None) is True


class TestRendererSplit:
	def test_flag_wins_over_misleading_short_key(self):
		rows = [
			{"function": "user/user.py::g", "is_framework": True},
			{"function": "sales/invoice.py::f", "is_framework": False},
		]
		custom, framework = renderer._split_hot_frames(rows)
		assert [r["function"] for r in custom] == ["sales/invoice.py::f"]
		assert [r["function"] for r in framework] == ["user/user.py::g"]

	def test_legacy_rows_fall_back_to_key_classification(self):
		rows = [{"function": "frappe/core/doctype/user/user.py::run"}, {"function": "myapp/hooks.py::h"}]
		custom, framework = renderer._split_hot_frames(rows)
		assert [r["function"] for r in custom] == ["myapp/hooks.py::h"]
		assert [r["function"] for r in framework] == ["frappe/core/doctype/user/user.py::run"]

	def test_empty_and_none(self):
		assert renderer._split_hot_frames([]) == ([], [])
		assert renderer._split_hot_frames(None) == ([], [])

	def test_only_one_kind(self):
		only_fw = [{"function": "a/b.py::f", "is_framework": True}]
		assert renderer._split_hot_frames(only_fw) == ([], only_fw)
		only_app = [{"function": "a/b.py::f", "is_framework": False}]
		assert renderer._split_hot_frames(only_app) == (only_app, [])

	def test_end_to_end_both_modes(self):
		files = [APP_FILE, FRAPPE_FILE]
		custom, framework = _split(files, installed=frozenset({"myapp", "frappe"}))
		assert custom == {"sales/invoice.py::fn0"}
		assert framework == {"user/user.py::fn1"}
		# Tracked Apps mode skips non-tracked frames, so the framework group is empty here.
		custom, framework = _split(files, tracked=("myapp",))
		assert custom == {"sales/invoice.py::fn0"}
		assert framework == set()


class TestPerGroupCap:
	def test_framework_group_not_starved_by_app_frames(self):
		kids = [_n(f"app{i}", f"/bench/apps/myapp/myapp/m{i}/f.py", 500 + i) for i in range(25)]
		kids += [_n("fw", "/bench/apps/frappe/frappe/core/doctype/user/user.py", 0)]
		kids[-1]["cumulative_ms"] = 900
		tree = _n("<root>", "", 0, kids)
		_, board = call_tree._aggregate_hot_frames([tree], None, frozenset({"myapp", "frappe"}))
		assert sum(1 for r in board if not r["is_framework"]) == 20
		assert [r["function"] for r in board if r["is_framework"]] == ["user/user.py::fw"]

	def test_framework_group_capped_by_cumulative_time(self):
		kids = []
		for i in range(25):
			node = _n(f"fw{i}", f"/bench/apps/frappe/frappe/core/m{i}/f.py", 0)
			node["cumulative_ms"] = 100 + i
			kids.append(node)
		tree = _n("<root>", "", 0, kids)
		_, board = call_tree._aggregate_hot_frames([tree], None, frozenset({"frappe"}))
		fw = [r for r in board if r["is_framework"]]
		assert len(fw) == 20
		assert {r["total_cumulative_ms"] for r in fw} == {float(100 + i) for i in range(5, 25)}


class TestRenderedGroups:
	def _html(self, rows):
		import json

		from optimus import renderer
		from optimus.tests.test_v5_panels_render import _fake_session_doc

		doc = _fake_session_doc({
			"infra_timeline": [], "infra_summary": {}, "frontend_xhr_matched": [],
			"frontend_vitals_by_page": {}, "frontend_orphans": [], "frontend_summary": {},
		})
		doc.hot_frames_json = json.dumps(rows)
		return renderer.render(doc, recordings=[])

	def test_both_groups_labelled_app_first(self):
		html = self._html([
			{"function": "a/b.py::app_fn", "total_ms": 5, "is_framework": False},
			{"function": "c/d.py::fw_fn", "total_ms": 0, "total_cumulative_ms": 9, "is_framework": True},
		])
		app_at = html.index('id="hot-frames-app"')
		fw_at = html.index('id="hot-frames-framework"')
		assert app_at < html.index("a/b.py::app_fn") < fw_at < html.index("c/d.py::fw_fn")

	def test_only_framework_has_no_app_heading(self):
		html = self._html([{"function": "c/d.py::fw_fn", "total_cumulative_ms": 9, "is_framework": True}])
		assert 'id="hot-frames-app"' not in html
		assert 'id="hot-frames-framework"' in html
		assert "Framework code (" not in html
		assert "all frames ran in framework code" in html

	def test_only_app_has_no_framework_heading(self):
		html = self._html([{"function": "a/b.py::app_fn", "total_ms": 5, "is_framework": False}])
		assert 'id="hot-frames-framework"' not in html
		assert 'id="hot-frames-app"' in html


class TestKeyCollision:
	FW = "/bench/apps/frappe/frappe/core/doctype/user/user.py"
	APP = "/bench/apps/myapp/myapp/user/user.py"

	def _rows(self, fw_ms=900, app_ms=10, order=("fw", "app")):
		nodes = {"fw": _n("fmt", self.FW, fw_ms), "app": _n("fmt", self.APP, app_ms)}
		tree = _n("<root>", "", 0, [nodes[k] for k in order])
		_, board = call_tree._aggregate_hot_frames([tree], None, frozenset({"myapp", "frappe"}))
		return board

	def test_same_short_key_stays_two_rows_with_own_times(self):
		for order in (("fw", "app"), ("app", "fw")):
			board = self._rows(order=order)
			assert len(board) == 2
			by_fw = {r["is_framework"]: r for r in board}
			assert by_fw[True]["total_ms"] == 900
			assert by_fw[False]["total_ms"] == 10

	def test_colliding_labels_are_disambiguated(self):
		labels = [r["function"] for r in self._rows()]
		assert len(set(labels)) == 2

	def test_app_row_lands_in_app_group(self):
		board = self._rows()
		custom, framework = renderer._split_hot_frames(board, ())
		assert [r["total_ms"] for r in custom] == [10]
		assert [r["total_ms"] for r in framework] == [900]

	def test_unique_labels_keep_short_form(self):
		tree = _n("<root>", "", 0, [_n("a", self.APP, 5)])
		_, board = call_tree._aggregate_hot_frames([tree], None, None)
		assert board[0]["function"] == "user/user.py::a"

	def test_identical_tail_paths_fall_back_to_full_path(self):
		a, b = "/x/a/user/user.py", "/y/a/user/user.py"
		tree = _n("<root>", "", 0, [_n("fmt", a, 5), _n("fmt", b, 6)])
		_, board = call_tree._aggregate_hot_frames([tree], None, None)
		assert len({r["function"] for r in board}) == 2

	def test_per_group_cap_still_applies(self):
		files = [f"/bench/apps/myapp/myapp/m{i}.py" for i in range(25)]
		tree = _n("<root>", "", 0, [_n("f", f, 100 + i) for i, f in enumerate(files)])
		_, board = call_tree._aggregate_hot_frames([tree], None, frozenset({"myapp"}))
		assert len(board) == call_tree.HOT_FRAMES_LEADERBOARD_SIZE


class TestIntermediateTrim:
	def test_framework_rows_survive_trim_with_many_app_frames(self):
		cap = call_tree.HOT_FRAMES_INTERMEDIATE_CAP
		fw = _n("fw", "/bench/apps/frappe/frappe/core/fw.py", 0)
		fw["cumulative_ms"] = 900
		trees = [_n("<root>", "", 0, [fw])]
		for i in range(cap * 2 + 50):
			node = _n(f"app{i}", f"/bench/apps/myapp/myapp/m{i}.py", 50)
			trees.append(_n("<root>", "", 0, [node]))
		_, board = call_tree._aggregate_hot_frames(trees, None, frozenset({"myapp", "frappe"}))
		fw_rows = [r for r in board if r["is_framework"]]
		assert [r["total_cumulative_ms"] for r in fw_rows] == [900]

	def test_trim_keeps_top_of_each_group(self):
		cap = call_tree.HOT_FRAMES_INTERMEDIATE_CAP
		occ, cls = {}, {}
		for i in range(cap):
			occ[("a", f"app{i}")] = [(0, float(i), 0)]
			cls[("a", f"app{i}")] = False
			occ[("f", f"fw{i}")] = [(0, 0, float(i))]
			cls[("f", f"fw{i}")] = True
		kept = call_tree._trim_occurrences(occ, cls)
		assert len(kept) == cap
		assert ("a", f"app{cap - 1}") in kept and ("f", f"fw{cap - 1}") in kept
		assert ("a", "app0") not in kept and set(cls) == set(kept)


class TestIdentityNormalisation:
	def test_relative_and_absolute_spellings_are_one_row(self):
		abs_p = "/bench/apps/myapp/myapp/sales/invoice.py"
		rel_p = "apps/myapp/myapp/sales/invoice.py"
		tree = _n("<root>", "", 0, [_n("f", abs_p, 5), _n("f", rel_p, 7)])
		_, board = call_tree._aggregate_hot_frames([tree], None, frozenset({"myapp"}))
		assert len(board) == 1 and board[0]["total_ms"] == 12

	def test_leading_slash_does_not_duplicate_labels(self):
		tree = _n("<root>", "", 0, [_n("f", "/a/b.py", 5), _n("f", "a/b.py", 6)])
		_, board = call_tree._aggregate_hot_frames([tree], None, None)
		assert len(board) == 1

	def test_empty_filename_is_framework(self):
		assert call_tree._is_framework_frame_file("", ("myapp",), None) is True
		assert call_tree._is_framework_frame_file("", None, frozenset({"myapp"})) is True


class TestCollidingAbsolutePaths:
	def test_unresolvable_collision_gets_short_hashed_labels(self):
		a = "/srv/lib/pkg/sub//util.py"
		b = "/srv/lib/pkg/sub/util.py"
		tree = _n("<root>", "", 0, [_n("f", a, 105), _n("f", b, 106)])
		_, board = call_tree._aggregate_hot_frames([tree], None, None)
		labels = [r["function"] for r in board]
		assert len(board) == 2 and len(set(labels)) == 2
		for label in labels:
			assert "srv" not in label
			assert "#" in label

	def test_hash_label_is_stable(self):
		ident = ("opt/x/y/z/util.py", "f")
		assert call_tree._hashed_label(ident) == call_tree._hashed_label(ident)
		assert call_tree._hashed_label(ident).startswith("y/z/util.py::f#")
