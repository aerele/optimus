# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Redundant Call findings analyzed before the L5 callsite fix may point at the outer
hook instead of the loop. The fixed analyzer stamps what it builds
(technical_detail["callsite_walk"] = "outermost_first", owner decision D-STAMP); a
Redundant Call finding without that exact stamp never reaches the AI ("re-record the
flow"). Stamped findings and every other finding type are unaffected."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from optimus import ai_fix, analyze
from optimus.analyzers import redundant_calls
from optimus.analyzers.base import AnalyzeContext
from optimus.renderer import fix_recipes

_CALLSITE = {"filename": "apps/myapp/myapp/x.py", "lineno": 9, "function": "f"}
_UNSTAMPED = {"fn_name": "get_doc", "callsite": _CALLSITE}
_STAMPED = dict(_UNSTAMPED, callsite_walk="outermost_first")


def _rc(detail):
	return {"finding_type": "Redundant Call", "technical_detail_json": json.dumps(detail)}


class TestPreL5Gate:
	def test_unstamped_redundant_call_is_gated_with_the_re_record_note(self):
		note = ai_fix.llm_gate_note(_rc(_UNSTAMPED))
		assert note == fix_recipes.PRE_L5_REDUNDANT_CALL_NOTE
		assert "analyzed before the callsite fix" in note and "re-record the flow" in note

	def test_stamped_redundant_call_is_eligible_in_both_shapes(self):
		assert ai_fix.llm_gate_note(_rc(_STAMPED)) is None
		assert ai_fix.llm_gate_note({"finding_type": "Redundant Call", "technical_detail": dict(_STAMPED)}) is None

	def test_any_other_stamp_value_is_pre_fix(self):
		assert ai_fix.llm_gate_note(_rc(dict(_STAMPED, callsite_walk="innermost_first"))) is not None
		assert ai_fix.llm_gate_note({"finding_type": "Redundant Call", "technical_detail": {}}) is not None

	def test_other_finding_types_ignore_the_stamp(self):
		assert ai_fix.llm_gate_note({"finding_type": "N+1 Query", "technical_detail_json": json.dumps(_UNSTAMPED)}) is None
		assert ai_fix.llm_gate_note({"finding_type": "Slow Query", "technical_detail": {}}) is None

	def test_gate_input_carries_the_row_detail(self):
		row = SimpleNamespace(finding_type="Redundant Call", technical_detail_json=json.dumps(_STAMPED))
		assert ai_fix.gate_input(row) == {"finding_type": "Redundant Call", "technical_detail_json": json.dumps(_STAMPED)}
		assert ai_fix.llm_gate_note(ai_fix.gate_input(row)) is None


def test_fresh_analyzer_output_reaches_the_ai():
	"""What the fixed analyzer builds is stamped, so the gate lets it through (dropping
	the analyzer's stamp line fails this test and the corpus anchors)."""
	stack = [  # innermost-first, as capture builds it
		{"filename": "apps/myapp/myapp/orders.py", "lineno": 24, "function": "check_rows"},
		{"filename": "apps/myapp/myapp/orders.py", "lineno": 9, "function": "validate_hook"},
		{"filename": "apps/frappe/frappe/app.py", "lineno": 120, "function": "application"},
	]
	recording = {"uuid": "r", "calls": [], "pyi_session": None, "sidecar": [
		{"fn_name": "get_doc", "identifier_raw": ["User", "a"], "identifier_safe": ["User", "h"], "caller_stack": stack}
		for _ in range(150)
	]}
	findings = redundant_calls.analyze([recording], AnalyzeContext(session_uuid="t", docname="t")).findings
	rc = [f for f in findings if f["finding_type"] == "Redundant Call"]
	assert len(rc) == 1
	assert json.loads(rc[0]["technical_detail_json"])["callsite_walk"] == fix_recipes.CALLSITE_WALK_FIXED
	assert ai_fix.llm_gate_note(rc[0]) is None


class _FakeDB:
	def __init__(self):
		self.writes = []

	def set_value(self, dt, name, field, val, *a, **k):
		self.writes.append(name)

	def commit(self):
		pass


def test_refresh_selection_skips_pre_fix_redundant_calls():
	rows = [
		SimpleNamespace(
			name=name, finding_type=ftype, severity="High", title="t", customer_description="d",
			estimated_impact_ms=100, affected_count=1, action_ref="0",
			technical_detail_json=detail, llm_fix_json=None,
		)
		for name, ftype, detail in (
			("rc_old", "Redundant Call", json.dumps(_UNSTAMPED)),
			("rc_new", "Redundant Call", json.dumps(_STAMPED)),
			("n1", "N+1 Query", "{}"),
		)
	]
	cfg = SimpleNamespace(ai_excluded_finding_types=())
	selected = analyze.eligible_findings(rows, cfg)
	assert sorted(r.name for r in selected) == ["n1", "rc_new"]


def test_only_redundant_call_findings_can_be_pre_fix():
	assert fix_recipes.analyzed_before_callsite_fix(_rc(_UNSTAMPED)) is True
	assert fix_recipes.analyzed_before_callsite_fix(_rc(_STAMPED)) is False
	assert fix_recipes.analyzed_before_callsite_fix({"finding_type": "N+1 Query", "technical_detail": {}}) is False
