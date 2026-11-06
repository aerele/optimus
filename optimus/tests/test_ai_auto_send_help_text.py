"""Optimus Settings must say that enabling AI also sends findings automatically."""

import json
from pathlib import Path

_SCHEMA = Path(__file__).resolve().parents[1] / "optimus/doctype/optimus_settings/optimus_settings.json"


def _fields():
	return {field["fieldname"]: field for field in json.loads(_SCHEMA.read_text())["fields"]}


def test_section_text_says_findings_are_sent_automatically_and_how_to_stop():
	text = _fields()["ai_description"]["options"]
	assert "Enabling AI also sends findings automatically" in text
	assert "After each analysis" in text
	assert "automatic-suggestion limit" in text
	assert "Privacy mode" in text
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


def test_modified_is_bumped_so_migrate_reloads_the_descriptions():
	modified = json.loads(_SCHEMA.read_text())["modified"]
	assert modified > "2026-10-31 12:00:00.000000"
	assert modified >= "2026-11-07"
