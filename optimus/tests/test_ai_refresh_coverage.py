# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Refresh AI suggestions and the analyze-time AI step: what is selected, in what order,
and what is counted."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from optimus import ai_fix, ai_prompts, analyze

# The real suggest_fix, captured before any fixture replaces it.
_REAL_SUGGEST = ai_fix.suggest_fix


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
	"""Nine eligible findings, three fit one refresh."""
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
	"""The backfill used to send excluded types, so each Refresh logged an error."""
	rows = [_row("sq", "Slow Query"), _row("n1"), _row("idx", "Missing Index"), _row("mem", "Memory Pressure")]
	with patch("optimus.settings.get_config", return_value=_cfg(ai_excluded_finding_types=("Slow Query",))):
		out = analyze._run_ai_backfill(_doc(rows), cap=0, regenerate_all=True)
	assert backfill.sent == ["n1"]
	assert (out["excluded"], out["gated"], out["failed"], out["total_pending"]) == (1, 0, 0, 1)


@pytest.mark.parametrize("kind,failed,skipped", [("not_eligible", 0, 1), ("config", 1, 0), ("transport", 1, 0)])
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
	assert "listed under Excluded finding types" in str(caught.value)
	assert ai_fix.AI_SKIP_KINDS == frozenset({"not_eligible"})


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
	assert any("1 finding(s) were not sent to the AI: the report shows" in w for w in ctx.warnings)
	assert any("1 finding(s) skipped because their type is listed under Excluded finding types" in w for w in ctx.warnings)


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
		"AI auto-suggest: 1 finding(s) were not sent to the AI: the report shows "
		"Optimus's own advice or a note on each.",
		"AI auto-suggest: 1 finding(s) skipped because their type is listed under "
		"Excluded finding types in Optimus Settings.",
	]


def test_an_excluded_type_the_ai_never_sees_is_counted_by_the_gate(backfill):
	"""Only an AI-eligible type counts as excluded; an excluded index type is still the
	gate's (it gets the deterministic advice either way), but index findings are not counted
	as gated: every session has them, so the note would fire on every session."""
	rows = [_row("idx", "Missing Index"), _row("n1")]
	with patch("optimus.settings.get_config", return_value=_cfg(ai_excluded_finding_types=("Missing Index",))):
		out = analyze._run_ai_backfill(_doc(rows), cap=0, regenerate_all=True)
	assert (out["excluded"], out["gated"], out["total_pending"]) == (0, 0, 1)
	assert backfill.sent == ["n1"]


def test_the_exclusion_wins_over_the_gate(backfill):
	"""An excluded, unstamped Redundant Call is counted once, as excluded."""
	rows = [_row("rc", "Redundant Call", detail=json.dumps({"fn_name": "get_doc"}))]
	with patch("optimus.settings.get_config", return_value=_cfg(ai_excluded_finding_types=("Redundant Call",))):
		out = analyze._run_ai_backfill(_doc(rows), cap=0, regenerate_all=True)
	assert (out["excluded"], out["gated"]) == (1, 0)
	assert backfill.sent == []


def test_a_context_window_too_small_is_a_logged_failure(backfill, monkeypatch):
	"""Kind "config" (here the pre-HTTP window check) is a failure,
	never a silent skip."""
	logged = []
	monkeypatch.setattr(ai_fix, "suggest_fix", _REAL_SUGGEST)
	monkeypatch.setattr(analyze, "_log_ai_step_failure", lambda title, *a, **k: logged.append(title))
	monkeypatch.setattr(ai_fix, "_provider_config", lambda: {
		"name": "OpenAI-compatible", "model": "m", "base_url": "http://x/v1", "protocol": "openai", "needs_key": False,
	})
	monkeypatch.setattr(ai_fix, "_context_tokens", lambda provider: 512)
	monkeypatch.setattr(ai_fix, "_resolve_display_threshold_ms", lambda: 1000.0)
	with patch("optimus.settings.get_config", return_value=_cfg()):
		out = analyze._run_ai_backfill(_doc([_row("n1")]), cap=0)
	assert (out["failed"], out["skipped_ineligible"], out["added"]) == (1, 0, 0)
	assert logged == ["optimus ai backfill"]


def _one_n_plus_one_ctx():
	return SimpleNamespace(
		session_uuid="u", docname=None, warnings=[], actions=[],
		findings=[{
			"finding_type": "N+1 Query", "severity": "High", "title": "t", "customer_description": "d",
			"estimated_impact_ms": 100, "affected_count": 1, "action_ref": "0", "technical_detail_json": "{}",
		}],
	)


def test_auto_suggest_counts_a_config_error_as_a_failure(monkeypatch):
	ctx = _one_n_plus_one_ctx()
	monkeypatch.setattr(analyze, "_phase2_index_for", lambda doc: {})
	monkeypatch.setattr(analyze, "_log_ai_step_failure", lambda *a, **k: None)
	with patch("optimus.settings.get_config", return_value=_cfg()), \
	     patch("optimus.ai_fix.is_available", return_value=True), \
	     patch("optimus.ai_fix.suggest_fix", side_effect=ai_fix.AiFixError("window too small", kind="config")):
		analyze._enrich_findings_with_ai_suggestions(ctx)
	assert "llm_fix_json" not in ctx.findings[0]
	assert any("1 finding(s) couldn't get a suggestion" in w for w in ctx.warnings)


def test_auto_suggest_treats_not_eligible_as_a_skip(monkeypatch):
	logged = []
	ctx = _one_n_plus_one_ctx()
	monkeypatch.setattr(analyze, "_phase2_index_for", lambda doc: {})
	monkeypatch.setattr(analyze, "_log_ai_step_failure", lambda title, *a, **k: logged.append(title))
	with patch("optimus.settings.get_config", return_value=_cfg()), \
	     patch("optimus.ai_fix.is_available", return_value=True), \
	     patch("optimus.ai_fix.suggest_fix", side_effect=ai_fix.AiFixError("gated", kind="not_eligible")):
		analyze._enrich_findings_with_ai_suggestions(ctx)
	assert "llm_fix_json" not in ctx.findings[0]
	assert logged == [] and not any("couldn't get a suggestion" in w for w in ctx.warnings)


def test_a_missing_only_refresh_and_eligible_findings_agree_on_what_is_missing(backfill):
	"""One definition: only a blank answer is missing. Corrupt text is outdated, so a missing-only
	run leaves it, and so does ``eligible_findings(include_outdated=False)``."""
	rows = [_row("blank"), _row("corrupt"), _row("old", version=1), _row("fresh", version=ai_prompts.PROMPT_VERSION)]
	rows[0].llm_fix_json = "  "
	rows[1].llm_fix_json = "{not json"
	with patch("optimus.settings.get_config", return_value=_cfg()):
		expected = [r.name for r in analyze.eligible_findings(rows, _cfg(), include_outdated=False)]
		analyze._run_ai_backfill(_doc(rows), cap=0)
	assert expected == ["blank"] and backfill.sent == ["blank"]


def test_a_newer_answer_is_not_asked_again_and_a_float_version_is(backfill):
	rows = [
		_row("newer", version=ai_prompts.PROMPT_VERSION + 1),
		_row("float", version=float(ai_prompts.PROMPT_VERSION)),
		_row("blank"),
	]
	with patch("optimus.settings.get_config", return_value=_cfg()):
		analyze._run_ai_backfill(_doc(rows), cap=0, regenerate_all=True)
	assert backfill.sent == ["blank", "float", "newer"]


def test_the_backfill_orders_by_severity_impact_then_age_like_eligible_findings(backfill):
	def aged(name, generated, severity="High", impact=10.0):
		r = _row(name, impact=impact, version=1)
		r.severity = severity
		r.llm_fix_json = json.dumps({"suggestion": "s", "prompt_version": 1, "generated_at": generated})
		return r

	rows = [
		aged("young", "2026-01-03T00:00:00+00:00"),
		aged("old", "2026-01-01T00:00:00+00:00"),
		aged("low", "2026-01-01T00:00:00+00:00", severity="Low"),
		aged("big", "2026-01-05T00:00:00+00:00", impact=99.0),
	]
	with patch("optimus.settings.get_config", return_value=_cfg()):
		by_function = [r.name for r in analyze.eligible_findings(rows, _cfg(), regenerate_all=True)]
		analyze._run_ai_backfill(_doc(rows), cap=0, regenerate_all=True, time_budget=10_000)
	assert backfill.sent == by_function == ["big", "old", "young", "low"]


def test_the_backfill_counts_come_from_the_selection(backfill):
	rows = [_row("ok"), _row("fw", "Framework N+1"), _row("sq", "Slow Query")]
	with patch("optimus.settings.get_config", return_value=_cfg(ai_excluded_finding_types=("Slow Query",))):
		out = analyze._run_ai_backfill(_doc(rows), cap=0)
	assert (out["gated"], out["excluded"], out["total_pending"]) == (1, 1, 1)
