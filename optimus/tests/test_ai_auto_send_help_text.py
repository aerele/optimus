"""Optimus Settings must say that enabling AI also sends findings automatically."""

import json
from pathlib import Path

_SCHEMA = Path(__file__).resolve().parents[1] / "optimus/doctype/optimus_settings/optimus_settings.json"


def _fields():
	return {field["fieldname"]: field for field in json.loads(_SCHEMA.read_text())["fields"]}


def test_section_text_says_findings_are_sent_automatically_and_how_to_stop():
	text = _fields()["ai_description"]["options"]
	assert "Enabling AI also sends findings and Steps to Reproduce automatically" in text
	assert "When a session reaches Ready" in text
	assert "After each analysis" not in text
	assert "automatic-suggestion limit" in text
	assert "Privacy mode" not in text
	assert "Send raw values to the AI provider" in text
	assert "Run AI suggestions automatically after profiling" in text
	assert "untick" in text


def test_enable_description_warns_about_automatic_sending():
	text = _fields()["ai_enabled"]["description"]
	assert "automatically" in text
	assert "untick" in text


def test_auto_suggest_description_says_unticking_turns_it_off_and_default_stays_on():
	field = _fields()["ai_auto_suggest"]
	assert field["default"] == "1"
	assert "Untick" in field["description"]
	assert "automatic" in field["description"]


def _run_queue(monkeypatch, **overrides):
	import frappe

	from optimus import ai_jobs, analyze, settings

	cfg = settings.OptimusConfig(ai_enabled=True, **overrides)
	calls = []
	monkeypatch.setattr(settings, "get_config", lambda: cfg)
	monkeypatch.setattr(
		frappe.db,
		"get_value",
		lambda *a, **kw: {"status": "Ready", "session_uuid": "u", "owner": "o"},
		raising=False,
	)
	monkeypatch.setattr(ai_jobs, "start_refresh", lambda **kw: calls.append(kw) or {"status": "started"})
	analyze.queue_analyze_time_ai("doc-1")
	return calls


def test_auto_suggest_is_on_by_default():
	from optimus import settings

	assert settings._DEFAULTS["ai_auto_suggest"] is True
	assert settings.OptimusConfig().ai_auto_suggest is True


def test_with_defaults_enabling_ai_starts_a_refresh(monkeypatch):
	calls = _run_queue(monkeypatch)
	assert len(calls) == 1
	assert calls[0]["include_fixes"] and calls[0]["include_steps"]


def test_with_auto_suggest_off_no_refresh_starts(monkeypatch):
	assert _run_queue(monkeypatch, ai_auto_suggest=False) == []


def test_modified_is_bumped_so_migrate_reloads_the_descriptions():
	modified = json.loads(_SCHEMA.read_text())["modified"]
	assert modified > "2026-10-31 12:00:00.000000"
	assert modified >= "2026-11-07"
