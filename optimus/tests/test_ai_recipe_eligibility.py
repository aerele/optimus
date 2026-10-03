# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Framework N+1 and index-family findings never reach the LLM; a Hot
Line whose line only calls another function or sits in framework code never
reaches it either. Covers the chokepoint (ai_fix.llm_gate_note), the
suggest_fix guard and both analyze paths."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from optimus import ai_fix, analyze

_USER_FILE = "/home/b/apps/myapp/myapp/order.py"
_RESULT = {"suggestion": "**Fix**\n\ndo X", "model": "m", "provider": "OpenAI-compatible",
           "generated_at": "2026-09-24T00:00:00+00:00"}


def _cfg(**kw):
	base = {"ai_enabled": True, "ai_auto_suggest": True, "ai_auto_suggest_max": 0,
	        "ai_suggest_findings": True, "tracked_apps": (), "ai_excluded_finding_types": ()}
	base.update(kw)
	return SimpleNamespace(**base)


def _finding(ftype, detail=None):
	return {
		"finding_type": ftype, "severity": "High", "title": ftype, "customer_description": "d",
		"estimated_impact_ms": 100, "affected_count": 1, "action_ref": "0",
		"technical_detail_json": json.dumps(detail or {"normalized_query": "SELECT 1"}),
	}


def _hot(content, file=_USER_FILE):
	return _finding("Hot Line", {"file": file, "lineno": 9, "line_content": content})


class _FakeDB:
	def __init__(self):
		self.writes = []

	def set_value(self, dt, name, field, val, **kwargs):
		self.writes.append(name)

	def commit(self):
		pass


def _fake_frappe():
	return SimpleNamespace(db=_FakeDB(), log_error=lambda *a, **k: None)


def _row(name, ftype, detail=None):
	return SimpleNamespace(name=name, llm_fix_json=None, **{
		k: v for k, v in _finding(ftype, detail).items()
	})


class TestEligibleSet:
	def test_framework_n_plus_one_is_not_ai_eligible(self):
		assert "Framework N+1" not in ai_fix.AI_ELIGIBLE_FINDING_TYPES

	def test_exact_eligible_set(self):
		assert ai_fix.AI_ELIGIBLE_FINDING_TYPES == frozenset(
			{"N+1 Query", "Slow Query", "Redundant Call", "Hot Line"},
		)


class TestGateNote:
	def test_notes(self):
		with patch("optimus.settings.get_config", return_value=_cfg()):
			assert ai_fix.llm_gate_note(_finding("N+1 Query")) is None
			assert ai_fix.llm_gate_note(_hot("total = total + flt(r.qty)")) is None
			assert "framework code" in ai_fix.llm_gate_note(_finding("Framework N+1"))
			assert "without the AI" in ai_fix.llm_gate_note(_finding("Missing Index"))
			assert "inside super().validate" in ai_fix.llm_gate_note(_hot("super().validate()"))
			assert ai_fix.llm_gate_note(_finding("Memory Pressure")) is not None


class TestAutoSuggestPath:
	def _run(self, findings):
		return [row["finding_type"] for row in analyze.eligible_findings(findings, _cfg(), include_outdated=False)]

	def test_auto_suggest_never_sends_framework_n_plus_one_or_index_findings(self):
		sent = self._run([_finding("Framework N+1"), _finding("Missing Index"), _finding("N+1 Query")])
		assert sent == ["N+1 Query"]

	def test_auto_suggest_skips_gated_hot_lines(self):
		sent = self._run([
			_hot("super().validate()"),
			_hot("x = 1", file="/home/b/apps/erpnext/erpnext/controllers/selling_controller.py"),
			_hot("total = total + flt(row.qty) * flt(row.rate)"),
		])
		assert sent == ["Hot Line"]  # only the pure-Python line


class TestBackfillPath:
	def test_backfill_skips_framework_n_plus_one_and_gated_hot_lines(self):
		rows = [
			_row("fw", "Framework N+1"),
			_row("hl_gated", "Hot Line", {"file": _USER_FILE, "line_content": "self.run_all()"}),
			_row("hl_ok", "Hot Line", {"file": _USER_FILE, "line_content": "n = len(rows)"}),
			_row("n1", "N+1 Query"),
		]
		selected = analyze.eligible_findings(rows, _cfg())
		assert sorted(row.name for row in selected) == ["hl_ok", "n1"]


class TestSuggestFixGuard:
	@pytest.mark.parametrize("finding", [
		{"finding_type": "Framework N+1", "technical_detail": {}},
		{"finding_type": "Missing Index", "technical_detail": {"table": "tabX", "column": "a"}},
		{"finding_type": "Hot Line", "technical_detail": {"file": _USER_FILE, "line_content": "super().validate()"}},
	])
	def test_refuses_before_any_provider_or_http_call(self, finding, monkeypatch):
		calls = []
		monkeypatch.setattr(ai_fix, "_resolve_provider", lambda *a, **k: calls.append("provider"))
		monkeypatch.setattr(ai_fix, "_dispatch_call", lambda *a, **k: calls.append("http"))
		with patch("optimus.settings.get_config", return_value=_cfg()):
			with pytest.raises(ai_fix.AiFixError) as exc:
				ai_fix.suggest_fix(finding)
		assert exc.value.kind == "not_eligible"
		assert calls == []
