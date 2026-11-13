# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Apps and folders merely named like Optimus stay visible; Optimus's own frames stay hidden."""

import pytest

from optimus.analyzers.call_tree import (
	_is_framework_frame,
	_is_pure_helper_frame,
	_short_callsite,
	_strip_profiler_frames,
)
from optimus.line_profile import picker

USER_FILES = [
	"acme_optimus/acme_optimus/engine.py",
	"myapp/myapp/optimus/report.py",
	"/b/apps/myapp/myapp/optimus/x.py",
	"apps/myapp/myapp/optimus/x.py",
]
OPTIMUS_FILES = [
	"optimus/optimus/capture.py",
	"optimus/capture.py",
	"apps/optimus/optimus/capture.py",
	"/home/frappe/bench/apps/optimus/optimus/capture.py",
	"/env/lib/python3.14/site-packages/optimus/capture.py",
]


def _node(function, filename, ms, children=(), lineno=10):
	return {"function": function, "filename": filename, "lineno": lineno, "kind": "python",
		"cumulative_ms": ms, "self_ms": ms - sum(c["cumulative_ms"] for c in children), "children": list(children)}


def _tree(*children):
	return {"function": "<root>", "filename": "", "kind": "python", "cumulative_ms": 500, "self_ms": 0,
		"children": list(children)}


@pytest.mark.parametrize("filename", USER_FILES)
def test_user_paths_are_not_framework_or_plumbing(filename):
	node = _node("compute", filename, 300)
	assert not _is_framework_frame(node)
	assert not _is_pure_helper_frame(node)


@pytest.mark.parametrize("filename", OPTIMUS_FILES)
def test_real_optimus_paths_are_framework_and_plumbing(filename):
	node = _node("compute", filename, 300)
	assert _is_framework_frame(node)
	assert _is_pure_helper_frame(node)


@pytest.mark.parametrize("filename", USER_FILES)
def test_short_callsite_names_a_user_file_with_optimus_in_its_path(filename):
	stack = [{"filename": filename, "lineno": 3}, {"filename": "frappe/frappe/database/database.py", "lineno": 9}]
	assert _short_callsite(stack) == f"{filename}:3"


@pytest.mark.parametrize("filename", OPTIMUS_FILES)
def test_short_callsite_skips_real_optimus_frames(filename):
	stack = [
		{"filename": "myapp/myapp/api.py", "lineno": 3},
		{"filename": filename, "lineno": 8},
		{"filename": "frappe/frappe/database/database.py", "lineno": 9},
	]
	assert _short_callsite(stack) == "myapp/myapp/api.py:3"


@pytest.mark.parametrize("filename", USER_FILES)
def test_strip_keeps_user_frames_with_optimus_in_their_path(filename):
	tree = _tree(_node("compute", filename, 300))
	assert [c["function"] for c in _strip_profiler_frames(tree)["children"]] == ["compute"]


@pytest.mark.parametrize("filename", OPTIMUS_FILES)
def test_strip_removes_real_optimus_frames_in_every_path_shape(filename):
	inner = _node("heavy", "myapp/myapp/x.py", 300)
	tree = _tree(_node("before_request", filename, 400, [inner]))
	assert [c["function"] for c in _strip_profiler_frames(tree)["children"]] == ["heavy"]


@pytest.mark.parametrize("filename", USER_FILES)
def test_picker_list_offers_a_user_function_under_an_optimus_named_path(filename):
	tree = _tree(_node("compute", filename, 300))
	assert [c["qualname"] for c in picker._build_tree_indented_candidates([tree])] == ["compute"]


def test_picker_list_offers_a_user_function_named_after_optimus():
	tree = _tree(_node("optimus_report", "myapp/myapp/reports.py", 300))
	assert [c["dotted_path"] for c in picker._build_tree_indented_candidates([tree])] == [
		"myapp.reports.optimus_report"
	]


@pytest.mark.parametrize("filename", OPTIMUS_FILES)
def test_picker_list_never_offers_real_optimus_frames(filename):
	tree = _tree(_node("compute", filename, 300))
	assert picker._build_tree_indented_candidates([tree]) == []
