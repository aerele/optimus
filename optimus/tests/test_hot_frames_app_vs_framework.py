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
	def test_shared_short_key_is_framework_only_if_all_files_are(self):
		fw = _n("fmt", "/bench/apps/frappe/frappe/core/doctype/user/user.py", 100)
		app = _n("fmt", "/bench/apps/myapp/myapp/user/user.py", 100)
		for order in ([fw, app], [app, fw]):
			tree = _n("<root>", "", 0, order)
			_, board = call_tree._aggregate_hot_frames([tree], None, frozenset({"myapp", "frappe"}))
			assert [r["is_framework"] for r in board] == [False]
