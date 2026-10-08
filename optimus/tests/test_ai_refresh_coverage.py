# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Refresh AI suggestions and the analyze-time AI step: what is selected, in what order,
and what is counted (P10, R-I1, O-I2)."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from optimus import ai_fix, ai_prompts, analyze


class _FakeDB:
	def __init__(self):
		self.writes = []

	def set_value(self, dt, name, field, val, *args, **kwargs):
		self.writes.append(name)

	def commit(self):
		pass


def _row(name, ftype="N+1 Query", *, impact=100.0, version=None, detail="{}"):
	fix = None if version is None else json.dumps({"suggestion": "old", "model": "m", "prompt_version": version})
	return SimpleNamespace(
		name=name, finding_type=ftype, severity="High", title=name, customer_description="d",
		estimated_impact_ms=impact, affected_count=1, action_ref="0", technical_detail_json=detail,
		llm_fix_json=fix,
	)


def _cfg(**kw):
	base = {
		"ai_enabled": True, "ai_auto_suggest": True, "ai_auto_suggest_max": 0, "ai_suggest_findings": True,
		"tracked_apps": (), "ai_excluded_finding_types": (),
	}
	base.update(kw)
	return SimpleNamespace(**base)


@pytest.fixture
def backfill(monkeypatch):
	"""A virtual clock (every call takes 25 s, so three fit the 60 s refresh budget)."""
	clock = SimpleNamespace(now=0.0)
	sent = []

	def suggest(payload, **kwargs):
		sent.append(payload["name"])
		clock.now += 25.0
		return {"suggestion": "new", "model": "m", "prompt_version": ai_prompts.PROMPT_VERSION}

	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(db=_FakeDB(), log_error=lambda *a, **k: None, local=SimpleNamespace()))
	monkeypatch.setattr(analyze, "time", SimpleNamespace(monotonic=lambda: clock.now))
	monkeypatch.setattr(analyze, "_ai_payload_for_finding", lambda row, *a, **k: {"name": row.name, "finding_type": row.finding_type})
	monkeypatch.setattr(analyze, "_phase2_index_for", lambda doc: {})
	monkeypatch.setattr(ai_fix, "is_available", lambda section=None: True)
	monkeypatch.setattr(ai_fix, "suggest_fix", suggest)
	return SimpleNamespace(clock=clock, sent=sent)


def _doc(rows):
	return SimpleNamespace(findings=rows, session_uuid="u", name="PS-1")


def test_three_refreshes_make_every_finding_current(backfill):
	"""Review Focus 5 (R-I1): nine eligible findings, three fit one refresh."""
	rows = [_row(f"F{i}", impact=100.0 - i, version=3) for i in range(9)]
	with patch("optimus.settings.get_config", return_value=_cfg()):
		for _ in range(3):
			out = analyze._run_ai_backfill(_doc(rows), cap=0, regenerate_all=True)
	assert out["skipped_time"] == 6
	assert all(json.loads(r.llm_fix_json)["prompt_version"] == ai_prompts.PROMPT_VERSION for r in rows)
	assert len(backfill.sent) == 9 and len(set(backfill.sent)) == 9


def test_missing_and_outdated_fixes_go_first(backfill):
	rows = [
		_row("current_big", impact=900.0, version=ai_prompts.PROMPT_VERSION),
		_row("old_small", impact=10.0, version=1),
		_row("none_mid", impact=50.0),
	]
	with patch("optimus.settings.get_config", return_value=_cfg()):
		analyze._run_ai_backfill(_doc(rows), cap=0, regenerate_all=True)
	assert backfill.sent == ["none_mid", "old_small", "current_big"]


def test_excluded_types_are_never_sent_and_gated_ones_are_counted(backfill):
	"""P10: the backfill used to send excluded types, so each Refresh logged an error."""
	rows = [_row("sq", "Slow Query"), _row("n1"), _row("idx", "Missing Index"), _row("mem", "Memory Pressure")]
	with patch("optimus.settings.get_config", return_value=_cfg(ai_excluded_finding_types=("Slow Query",))):
		out = analyze._run_ai_backfill(_doc(rows), cap=0, regenerate_all=True)
	assert backfill.sent == ["n1"]
	assert (out["excluded"], out["gated"], out["failed"], out["total_pending"]) == (1, 1, 0, 1)


@pytest.mark.parametrize("kind,failed,skipped", [("not_eligible", 0, 1), ("config", 0, 1), ("transport", 1, 0)])
def test_skip_kinds_are_not_failures(backfill, monkeypatch, kind, failed, skipped):
	logged = []

	def refuse(payload, **kwargs):
		raise ai_fix.AiFixError("no", kind=kind)

	monkeypatch.setattr(ai_fix, "suggest_fix", refuse)
	monkeypatch.setattr(analyze, "_log_ai_step_failure", lambda title, *a, **k: logged.append(title))
	with patch("optimus.settings.get_config", return_value=_cfg()):
		out = analyze._run_ai_backfill(_doc([_row("n1")]), cap=0)
	assert (out["failed"], out["skipped_ineligible"]) == (failed, skipped)
	assert len(logged) == failed


def test_an_excluded_type_raises_not_eligible():
	with patch("optimus.settings.get_config", return_value=_cfg(ai_excluded_finding_types=("Slow Query",))):
		with pytest.raises(ai_fix.AiFixError) as caught:
			ai_fix.suggest_fix({"finding_type": "Slow Query", "title": "x"})
	assert caught.value.kind == "not_eligible"
	assert "excluded by ai_excluded_finding_types" in str(caught.value)
	assert ai_fix.AI_SKIP_KINDS == frozenset({"config", "not_eligible"})


def test_auto_suggest_notes_gated_and_excluded_counts(monkeypatch):
	def finding(ftype):
		return {
			"finding_type": ftype, "severity": "High", "title": ftype, "customer_description": "d",
			"estimated_impact_ms": 100, "affected_count": 1, "action_ref": "0",
			"technical_detail_json": json.dumps({"normalized_query": "SELECT 1"}),
		}

	ctx = SimpleNamespace(
		session_uuid="u", docname=None, warnings=[], actions=[],
		findings=[finding(t) for t in ("Framework N+1", "Missing Index", "Slow Query", "N+1 Query")],
	)
	monkeypatch.setattr(analyze, "_phase2_index_for", lambda doc: {})
	with patch("optimus.settings.get_config", return_value=_cfg(ai_excluded_finding_types=("Slow Query",))), \
	     patch("optimus.ai_fix.is_available", return_value=True), \
	     patch("optimus.ai_fix.suggest_fix", side_effect=lambda payload, **kw: {"suggestion": "x", "model": "m"}):
		analyze._enrich_findings_with_ai_suggestions(ctx)
	assert any("2 finding(s) get deterministic advice or a note from Optimus" in w for w in ctx.warnings)
	assert any("1 finding(s) skipped because their type is excluded" in w for w in ctx.warnings)


def test_auto_suggest_notes_the_counts_when_nothing_is_eligible():
	ctx = SimpleNamespace(
		session_uuid="u", docname=None, warnings=[], actions=[],
		findings=[
			{"finding_type": t, "severity": "High", "technical_detail_json": "{}"}
			for t in ("Framework N+1", "Missing Index", "Slow Query")
		],
	)
	with patch("optimus.settings.get_config", return_value=_cfg(ai_excluded_finding_types=("Slow Query",))), \
	     patch("optimus.ai_fix.is_available", return_value=True), \
	     patch("optimus.ai_fix.suggest_fix", side_effect=AssertionError("nothing is eligible")):
		analyze._enrich_findings_with_ai_suggestions(ctx)
	assert ctx.warnings == [
		"AI auto-suggest: 2 finding(s) get deterministic advice or a note from Optimus "
		"instead of an AI suggestion (see each finding).",
		"AI auto-suggest: 1 finding(s) skipped because their type is excluded in "
		"Optimus Settings (ai_excluded_finding_types).",
	]
