# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Pure source-window and finding-stamp helpers, before pipeline wiring."""

import json

import pytest

from optimus.renderer import fix_recipes as fr


def test_small_function_excludes_unrelated_code():
	lines = ["import os", "", "def f(doc):", "\treturn doc.owner", "", "def g():", "\tpass"]
	window = fr.enclosing_function_window(lines, 4)
	assert [r["lineno"] for r in window] == [3, 4]
	assert [r["lineno"] for r in window if r["is_target"]] == [4]


def test_decorators_and_outer_function_are_included():
	lines = ["@decorator", "def outer(items):", "\tfor item in items:", "\t\tdef inner():",
		"\t\t\treturn item.name", "\t\tinner()"]
	assert [r["lineno"] for r in fr.enclosing_function_window(lines, 5)] == list(range(1, 7))


@pytest.mark.parametrize("lines,target", [
	(["def large():"] + ["\tpass"] * 200, 100),
	(["value = 0"] * 60 + ["if ("] + ["value = 1"] * 60, 61),
	(["value = 0"] * 121, 61),
])
def test_fallback_keeps_historical_window(lines, target):
	window = fr.enclosing_function_window(lines, target)
	assert len(window) == 49
	assert (window[0]["lineno"], window[-1]["lineno"]) == (target - 24, target + 24)


def test_long_source_line_is_capped_after_parsing():
	window = fr.enclosing_function_window(["def f():", "\tvalue = '" + "a" * 400 + "'"], 2, max_line_chars=200)
	assert len(window[1]["content"]) == 203
	assert window[1]["content"].endswith("...")


@pytest.mark.parametrize("target", [0, -1, 3, True, "1", None])
def test_invalid_source_target_has_no_window(target):
	assert fr.enclosing_function_window(["def f():", "\tpass"], target) == []


@pytest.mark.parametrize("stamp,old", [(None, True), ("innermost_first", True), ("outermost_first", False)])
@pytest.mark.parametrize("serialized", [False, True])
def test_callsite_stamp_is_exact_in_both_finding_shapes(stamp, old, serialized):
	detail = {} if stamp is None else {"callsite_walk": stamp}
	finding = {"finding_type": "Redundant Call"}
	finding["technical_detail_json" if serialized else "technical_detail"] = json.dumps(detail) if serialized else detail
	assert fr.analyzed_before_callsite_fix(finding) is old


@pytest.mark.parametrize("detail", [None, "invalid json", "[]", "null", "12"])
def test_unreadable_callsite_stamp_is_conservative(detail):
	assert fr.analyzed_before_callsite_fix({"finding_type": "Redundant Call", "technical_detail_json": detail})
	assert not fr.analyzed_before_callsite_fix({"finding_type": "Slow Query", "technical_detail_json": detail})
