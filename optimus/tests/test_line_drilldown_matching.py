# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The finding-card "Line-Level Drilldown" callout links a finding to its
phase-2 hot line via (file-basename, function-name). The index is keyed on the
phase-2 result's ``qualname`` while the lookup uses the callsite ``function``,
and the two can disagree on the module/class prefix (``common.bg_recheck_users``
vs bare ``bg_recheck_users``). These tests pin that matching stays robust to the
prefix on both sides.
"""

import json
from types import SimpleNamespace

from optimus import renderer
from optimus.renderer.line_drilldown import _render_phase2_diff_table


def _doc(qualname, file_path="/abs/apps/ugly_code/ugly_code/python/common.py", total_ms=300.0):
	return SimpleNamespace(phase_2_runs=[SimpleNamespace(
		run_uuid="r1", status="Ready",
		results_json=json.dumps([{
			"dotted_path": "ugly_code.python.common.bg_recheck_users",
			"qualname": qualname,
			"file": file_path,
			"lines": [{"lineno": 204, "content": "x", "total_ms": total_ms, "hits": 100}],
		}]),
	)])


def _lookup(doc, filename, function):
	idx = renderer._build_line_drilldown_callsite_index(doc)
	return renderer._make_line_drilldown_lookup(idx)(filename, function)


class TestDrilldownMatchRobustness:
	def test_bare_function_matches_prefixed_index_qualname(self):
		# Index qualname carries a module prefix; finding looks up the bare name.
		hit = _lookup(_doc("common.bg_recheck_users"),
			"ugly_code/python/common.py", "bg_recheck_users")
		assert hit and hit["lineno"] == 204

	def test_prefixed_function_matches_bare_index_qualname(self):
		# Reverse: index has the bare name, finding looks up a prefixed one.
		hit = _lookup(_doc("bg_recheck_users"),
			"ugly_code/python/common.py", "common.bg_recheck_users")
		assert hit and hit["lineno"] == 204

	def test_exact_match_still_works(self):
		hit = _lookup(_doc("bg_recheck_users"),
			"ugly_code/python/common.py", "bg_recheck_users")
		assert hit and hit["lineno"] == 204

	def test_different_function_does_not_match(self):
		assert _lookup(_doc("bg_recheck_users"),
			"ugly_code/python/common.py", "something_else") is None

	def test_different_basename_does_not_match(self):
		assert _lookup(_doc("bg_recheck_users"),
			"ugly_code/python/other.py", "bg_recheck_users") is None


class TestDiffDeltaCellStyling:
	"""The cross-run diff Δ-ms cell must not carry the amber ``time-high`` alarm:
	the row is already tinted green (faster) / red (slower), so alarming the delta
	value made a 1.6s improvement look identical to a 1.6s regression."""

	def _improvement_row(self):
		return {
			"status": "matched", "prev_lineno": 10, "curr_lineno": 10,
			"prev_ms": 5000.0, "curr_ms": 3400.0, "delta_ms": -1600.0,
			"content": "value = compute()",
		}

	def test_improvement_delta_is_plain_not_alarmed(self):
		html = _render_phase2_diff_table([self._improvement_row()], 1000.0)
		# The delta rolls over to seconds but is NOT wrapped in the alarm span.
		assert "-1.60s" in html
		assert 'class="time-high">-1.60s' not in html
		# The improvement still reads as an improvement via the row tint.
		assert 'class="added"' in html

	def test_regression_delta_also_plain(self):
		row = self._improvement_row()
		row.update(prev_ms=3400.0, curr_ms=5000.0, delta_ms=1600.0)
		html = _render_phase2_diff_table([row], 1000.0)
		assert "1.60s" in html
		assert 'class="time-high">1.60s' not in html
		assert 'class="removed"' in html
