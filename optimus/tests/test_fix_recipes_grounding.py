# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Pure finding-stamp helpers, before pipeline wiring (the source window moved to ai_grounding)."""

import json

import pytest

from optimus.renderer import fix_recipes as fr


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
