"""Consent is explicit, restricted to managers and retained in Settings history."""

import json
from pathlib import Path


def test_consent_defaults_and_auditable_settings():
	path = Path(__file__).resolve().parents[1] / "optimus/doctype/optimus_settings/optimus_settings.json"
	schema = json.loads(path.read_text())
	fields = {field["fieldname"]: field for field in schema["fields"]}
	assert schema["track_changes"] == 1
	assert fields["ai_send_raw_values"]["default"] == "0"
	assert fields["ai_enabled"]["default"] == "0"
	assert fields["ai_api_key"]["fieldtype"] == "Password"
	assert {row["role"] for row in schema["permissions"] if row.get("write")} == {"System Manager"}
