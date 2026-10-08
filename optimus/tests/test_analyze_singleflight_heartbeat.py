# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""P5: the single-flight flag is touched before every AI call of the analyze-time AI
step and right before _persist, and every call is capped below the flag's TTL, so the
flag cannot lapse while one analyze still runs (virtual clock, worst case: every call
uses its whole timeout, up to the 600 s maximum configured). A touch never takes the
flag from another session (Task 7 fix round 1)."""

import ast
import inspect
import json
import textwrap
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from optimus import analyze, safe_call


def _finding(i):
	return {
		"finding_type": "N+1 Query", "severity": "High", "title": f"t{i}", "customer_description": "d",
		"estimated_impact_ms": 100 - i, "affected_count": 1, "action_ref": "0",
		"technical_detail_json": json.dumps({"callsite": {"filename": "apps/myapp/myapp/x.py", "lineno": 3, "function": "f"}}),
	}


_PAYLOAD_SECONDS = 30.0  # building one finding's payload (source window, evidence reads)
_AI_CFG = SimpleNamespace(
	ai_enabled=True, ai_suggest_findings=True, ai_auto_suggest=True, ai_auto_suggest_max=0,
	tracked_apps=(), ai_excluded_finding_types=(), ai_humanize_steps=True,
)


@pytest.mark.parametrize("configured", [60, 180, 600])
def test_the_heartbeat_gap_stays_under_the_ttl(monkeypatch, configured):
	clock = SimpleNamespace(now=0.0)
	touches, timeouts = [], []

	def slow_suggest(payload, *, timeout=None):
		timeouts.append(timeout)
		clock.now += timeout  # worst case: the provider uses its whole timeout
		return {"suggestion": "x", "model": "m"}

	def payload(*args, **kwargs):
		clock.now += _PAYLOAD_SECONDS
		return {"finding_type": "N+1 Query"}

	monkeypatch.setattr(analyze, "time", SimpleNamespace(monotonic=lambda: clock.now))
	monkeypatch.setattr(analyze, "_touch_singleflight", lambda uuid: touches.append(clock.now))
	monkeypatch.setattr(analyze, "_ai_payload_for_finding", payload)
	monkeypatch.setattr(analyze, "_phase2_index_for", lambda *a, **k: {})
	monkeypatch.setattr(analyze, "_publish_progress", lambda *a, **k: None)
	ctx = SimpleNamespace(session_uuid="u", docname="PS-1", findings=[_finding(i) for i in range(6)], warnings=[], actions=[])
	with patch("optimus.settings.get_config", return_value=_AI_CFG), \
	     patch("optimus.ai_fix.is_available", return_value=True), \
	     patch("optimus.ai_fix._resolve_timeout_seconds", return_value=configured), \
	     patch("optimus.ai_fix.suggest_fix", side_effect=slow_suggest):
		analyze._enrich_findings_with_ai_suggestions(ctx)
	assert timeouts and timeouts == [min(configured, analyze.AI_CALL_TIMEOUT_CAP_SECONDS)] * len(timeouts)
	assert analyze.AI_CALL_TIMEOUT_CAP_SECONDS + _PAYLOAD_SECONDS < analyze._SINGLEFLIGHT_TTL_SECONDS
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


@pytest.mark.parametrize("configured", [60, 600])
def test_the_humanize_call_in_persist_is_capped_below_the_ttl(monkeypatch, configured):
	seen = {}

	def humanize(actions, *, session_title=None, usage_out=None, timeout=None):
		seen["timeout"] = timeout
		return "1. Open the Sales Invoice list"

	monkeypatch.setattr(analyze, "_actions_for_humanizer", lambda recordings: [{"label": "open"}])
	with patch("optimus.settings.get_config", return_value=SimpleNamespace(ai_enabled=True, ai_humanize_steps=True)), \
	     patch("optimus.ai_fix.is_available", return_value=True), \
	     patch("optimus.ai_fix._resolve_timeout_seconds", return_value=configured), \
	     patch("optimus.ai_fix.humanize_steps", side_effect=humanize):
		analyze._build_humanized_notes_html([])
	assert seen["timeout"] == min(configured, analyze.AI_CALL_TIMEOUT_CAP_SECONDS)


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


# --- Task 7 fix round 1: a touch never takes another session's flag ---------------------


class _JobTimeout(Exception):
	"""Stands in for rq's JobTimeoutException (rq is not importable on the CI stub run)."""


class _Cache:
	"""frappe.cache with the single-flight key in ``store``; ``writes`` lists every value
	written to that key."""

	def __init__(self, holder=None):
		self.store = {analyze._SINGLEFLIGHT_KEY: holder} if holder else {}
		self.writes = []

	def get_value(self, key, *args, **kwargs):
		return self.store.get(key)

	def set_value(self, key, value, *args, expires_in_sec=None, **kwargs):
		if key == analyze._SINGLEFLIGHT_KEY:
			self.writes.append((value, expires_in_sec))
		self.store[key] = value

	def delete_value(self, key, *args, **kwargs):
		self.store.pop(key, None)


@pytest.mark.parametrize("holder,writes", [
	("A", [("A", analyze._SINGLEFLIGHT_TTL_SECONDS)]),  # ours: the TTL is refreshed
	(None, [("A", analyze._SINGLEFLIGHT_TTL_SECONDS)]),  # lapsed and free: taken back
	("B", []),  # another session's: left alone
])
def test_a_touch_refreshes_only_a_flag_this_session_may_hold(monkeypatch, holder, writes):
	import frappe

	cache = _Cache(holder)
	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	analyze._touch_singleflight("A")
	assert cache.writes == writes
	assert cache.store.get(analyze._SINGLEFLIGHT_KEY) == (holder or "A")


def test_after_a_lapse_the_ai_loop_never_steals_another_sessions_flag(monkeypatch):
	"""A's flag lapsed during a long step and B took it: A's per-call heartbeats must
	leave B's flag alone."""
	import frappe

	cache = _Cache("B")
	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	monkeypatch.setattr(analyze, "_ai_payload_for_finding", lambda *a, **k: {"finding_type": "N+1 Query"})
	monkeypatch.setattr(analyze, "_phase2_index_for", lambda *a, **k: {})
	monkeypatch.setattr(analyze, "_publish_progress", lambda *a, **k: None)
	sent = []
	ctx = SimpleNamespace(session_uuid="A", docname="PS-A", findings=[_finding(i) for i in range(3)], warnings=[], actions=[])
	with patch("optimus.settings.get_config", return_value=_AI_CFG), \
	     patch("optimus.ai_fix.is_available", return_value=True), \
	     patch("optimus.ai_fix.suggest_fix", side_effect=lambda p, **k: sent.append(p) or {"suggestion": "x"}):
		analyze._enrich_findings_with_ai_suggestions(ctx)
	assert len(sent) == 3  # the AI step itself still ran
	assert cache.store[analyze._SINGLEFLIGHT_KEY] == "B" and cache.writes == []


def _drive_run(monkeypatch, *, cache, scheduler_disabled, deadline=None, configured=600, n=3, build=0.0, touch=None):
	"""Run the real ``analyze.run`` on a virtual clock, from the single-flight gate through
	the AI loop, _persist's humanize call and the render step (stubbed); every provider
	call uses its whole timeout and each finding's payload costs ``build`` seconds."""
	import frappe

	clock = SimpleNamespace(now=0.0)

	def slow_suggest(payload, *, timeout=None):
		clock.now += timeout
		return {"suggestion": "x", "model": "m"}

	def humanize(actions, *, session_title=None, usage_out=None, timeout=None):
		clock.now += timeout
		return "1. Open"

	def payload(*args, **kwargs):
		clock.now += build
		return {"finding_type": "N+1 Query"}

	db = SimpleNamespace(get_value=lambda *a, **k: "PS-A", set_value=lambda *a, **k: None, rollback=lambda *a, **k: None)
	monkeypatch.setattr(analyze, "time", SimpleNamespace(
		monotonic=lambda: clock.now, time=lambda: clock.now, sleep=lambda seconds: None,
	))
	monkeypatch.setattr(frappe, "db", db, raising=False)
	monkeypatch.setattr(frappe, "local", SimpleNamespace(), raising=False)
	monkeypatch.setattr(frappe, "conf", {"optimus_analyze_gc_collect": False}, raising=False)
	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda *a, **k: None, raising=False)
	monkeypatch.setattr(analyze, "is_scheduler_disabled", lambda: scheduler_disabled)
	monkeypatch.setattr(analyze, "_apply_nice", lambda: None)  # never renice the test process
	monkeypatch.setattr(analyze, "safe_commit", lambda: None)
	monkeypatch.setattr(analyze, "session", SimpleNamespace(
		get_recordings=lambda *a, **k: ["rec-1"], get_session_meta=lambda *a, **k: {},
		delete_session_state=lambda *a, **k: None,
	))
	monkeypatch.setattr(analyze, "_bg_wait_for_pending_jobs", lambda *a, **k: 0)
	monkeypatch.setattr(analyze, "_fetch_recordings", lambda *a, **k: iter([{"uuid": "rec-1"}]))
	monkeypatch.setattr(analyze, "_enrich_recordings", lambda *a, **k: [])
	monkeypatch.setattr(analyze, "_get_analyzers", lambda: [])
	monkeypatch.setattr("optimus.api._read_frontend_data", lambda *a, **k: {"xhr": [], "vitals": []})
	monkeypatch.setattr(analyze, "_enrich_findings_with_source_snippets", lambda fs: fs.extend(_finding(i) for i in range(n)))
	monkeypatch.setattr(analyze, "_ai_payload_for_finding", payload)
	monkeypatch.setattr(analyze, "_phase2_index_for", lambda *a, **k: {})
	monkeypatch.setattr(analyze, "_actions_for_humanizer", lambda recordings: [{"label": "open"}])
	monkeypatch.setattr(analyze, "_persist", lambda *a, **k: analyze._build_humanized_notes_html([]))
	for name in ("_publish_session_event", "_publish_progress", "_mark_ai_spend_session", "_render_and_attach_reports",
	             "_persist_recordings_file", "_cleanup_redis", "_auto_arm_phase2"):
		monkeypatch.setattr(analyze, name, lambda *a, **k: None)
	if touch is not None:
		monkeypatch.setattr(analyze, "_touch_singleflight", lambda uuid: touch(clock.now))
	with patch("optimus.settings.get_config", return_value=_AI_CFG), \
	     patch("optimus.ai_fix.is_available", return_value=True), \
	     patch("optimus.ai_fix._resolve_timeout_seconds", return_value=configured), \
	     patch("optimus.ai_fix.suggest_fix", side_effect=slow_suggest), \
	     patch("optimus.ai_fix.humanize_steps", side_effect=humanize):
		analyze.run("A", _singleflight_deadline=deadline)
	return clock


def test_a_degraded_run_never_takes_the_flag_from_its_holder(monkeypatch):
	"""Past its wait deadline, A runs anyway while OTHER holds the flag: none of A's
	heartbeats (before the EXPLAIN burst, the AI loop, before _persist and the render)
	may overwrite OTHER's flag, and A's release leaves it too."""
	cache = _Cache("OTHER")
	_drive_run(monkeypatch, cache=cache, scheduler_disabled=False, deadline=-1.0)
	assert cache.store[analyze._SINGLEFLIGHT_KEY] == "OTHER"
	assert cache.writes == []


@pytest.mark.parametrize("configured", [60, 180, 600])
def test_the_whole_run_keeps_every_heartbeat_gap_under_the_ttl(monkeypatch, configured):
	beats = []
	clock = _drive_run(
		monkeypatch, cache=_Cache(), scheduler_disabled=True, configured=configured, n=6,
		build=_PAYLOAD_SECONDS, touch=beats.append,
	)
	beats.append(clock.now)  # the run ended (its finally releases the flag)
	assert max(b - a for a, b in zip(beats, beats[1:], strict=False)) < analyze._SINGLEFLIGHT_TTL_SECONDS


@pytest.mark.parametrize("where", ["get_value", "set_value"])
def test_a_job_timeout_in_the_redis_call_escapes_fresh(monkeypatch, where):
	import frappe

	original = _JobTimeout("deadline")
	cache = _Cache()

	def interrupted(*args, **kwargs):
		raise original

	setattr(cache, where, interrupted)
	monkeypatch.setattr(safe_call, "job_timeout_types", lambda: (_JobTimeout,))
	monkeypatch.setattr(frappe, "cache", cache, raising=False)
	with pytest.raises(_JobTimeout) as caught:
		analyze._touch_singleflight("A")
	assert caught.value is not original
	assert caught.value.__context__ is None and caught.value.__cause__ is None


def test_a_cache_hiccup_never_fails_the_touch(monkeypatch):
	import frappe

	def broken(*args, **kwargs):
		raise ConnectionError("redis down")

	monkeypatch.setattr(frappe, "cache", SimpleNamespace(get_value=broken, set_value=broken), raising=False)
	analyze._touch_singleflight("A")  # no exception
