# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""P5: the single-flight flag is touched before every AI call of the analyze-time AI
step and right before _persist, and every call is capped below the flag's TTL, so the
flag cannot lapse while one analyze still runs (virtual clock, worst case: every call
uses its whole timeout, with the 600 s maximum configured)."""

import ast
import inspect
import json
import textwrap
from types import SimpleNamespace
from unittest.mock import patch

from optimus import analyze


def _finding(i):
	return {
		"finding_type": "N+1 Query", "severity": "High", "title": f"t{i}", "customer_description": "d",
		"estimated_impact_ms": 100 - i, "affected_count": 1, "action_ref": "0",
		"technical_detail_json": json.dumps({"callsite": {"filename": "apps/myapp/myapp/x.py", "lineno": 3, "function": "f"}}),
	}


def test_the_heartbeat_gap_stays_under_the_ttl_with_a_600_second_timeout(monkeypatch):
	clock = SimpleNamespace(now=0.0)
	touches, timeouts = [], []
	cfg = SimpleNamespace(
		ai_enabled=True, ai_suggest_findings=True, ai_auto_suggest=True, ai_auto_suggest_max=0,
		tracked_apps=(), ai_excluded_finding_types=(),
	)

	def slow_suggest(payload, *, timeout=None):
		timeouts.append(timeout)
		clock.now += timeout  # worst case: the provider uses its whole timeout
		return {"suggestion": "x", "model": "m"}

	monkeypatch.setattr(analyze, "time", SimpleNamespace(monotonic=lambda: clock.now))
	monkeypatch.setattr(analyze, "_touch_singleflight", lambda uuid: touches.append(clock.now))
	monkeypatch.setattr(analyze, "_ai_payload_for_finding", lambda *a, **k: {"finding_type": "N+1 Query"})
	monkeypatch.setattr(analyze, "_phase2_index_for", lambda *a, **k: {})
	monkeypatch.setattr(analyze, "_publish_progress", lambda *a, **k: None)
	ctx = SimpleNamespace(session_uuid="u", docname="PS-1", findings=[_finding(i) for i in range(6)], warnings=[], actions=[])
	with patch("optimus.settings.get_config", return_value=cfg), \
	     patch("optimus.ai_fix.is_available", return_value=True), \
	     patch("optimus.ai_fix._resolve_timeout_seconds", return_value=600), \
	     patch("optimus.ai_fix.suggest_fix", side_effect=slow_suggest):
		analyze._enrich_findings_with_ai_suggestions(ctx)
	assert timeouts and max(timeouts) <= analyze.AI_CALL_TIMEOUT_CAP_SECONDS < analyze._SINGLEFLIGHT_TTL_SECONDS
	assert len(touches) == len(timeouts)  # a heartbeat before every call
	beats = [*touches, clock.now]  # run() touches again right before _persist, when the step returns
	assert max(b - a for a, b in zip(beats, beats[1:], strict=False)) < analyze._SINGLEFLIGHT_TTL_SECONDS


def test_run_touches_the_flag_right_before_persist():
	tree = ast.parse(textwrap.dedent(inspect.getsource(analyze.run)))
	for node in ast.walk(tree):
		for field in ("body", "orelse", "finalbody"):
			block = getattr(node, field, None)
			if not isinstance(block, list):
				continue
			for i, stmt in enumerate(block):
				call = stmt.value if isinstance(stmt, ast.Expr) else None
				if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "_persist":
					previous = block[i - 1].value if i and isinstance(block[i - 1], ast.Expr) else None
					assert isinstance(previous, ast.Call) and getattr(previous.func, "id", None) == "_touch_singleflight"
					return
	raise AssertionError("no _persist call found in analyze.run")


def test_the_humanize_call_in_persist_is_capped_below_the_ttl(monkeypatch):
	seen = {}

	def humanize(actions, *, session_title=None, usage_out=None, timeout=None):
		seen["timeout"] = timeout
		return "1. Open the Sales Invoice list"

	monkeypatch.setattr(analyze, "_actions_for_humanizer", lambda recordings: [{"label": "open"}])
	with patch("optimus.settings.get_config", return_value=SimpleNamespace(ai_enabled=True, ai_humanize_steps=True)), \
	     patch("optimus.ai_fix.is_available", return_value=True), \
	     patch("optimus.ai_fix._resolve_timeout_seconds", return_value=600), \
	     patch("optimus.ai_fix.humanize_steps", side_effect=humanize):
		analyze._build_humanized_notes_html([])
	assert seen["timeout"] == analyze.AI_CALL_TIMEOUT_CAP_SECONDS


def test_humanize_steps_sends_its_timeout_to_the_provider(monkeypatch):
	import requests

	from optimus import ai_fix
	from optimus.tests.test_ai_fix import _FakeResp, _post_returning, _provider

	post = _post_returning(_FakeResp(200, {"choices": [{"message": {"content": "1. Open the list."}}]}))
	monkeypatch.setattr(requests, "post", post)
	provider = {
		"name": "OpenAI", "protocol": "openai", "base_url": "https://api.openai.com/v1", "model": "m",
		"needs_key": True, "has_key": True,
	}
	with _provider(provider):
		ai_fix.humanize_steps([{"label": "open", "cmd": "x", "duration_ms": 5}], timeout=analyze.AI_CALL_TIMEOUT_CAP_SECONDS)
	assert post.last.timeout == analyze.AI_CALL_TIMEOUT_CAP_SECONDS
