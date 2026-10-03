"""Upgrade defaults preserve deliberate unlimited refresh caps."""

import json
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest

from optimus import settings

pytestmark = pytest.mark.rq


@pytest.mark.parametrize("stored,expected", [(None, 20), (0, 0), (7, 7)])
def test_manual_refresh_cap_is_independent_of_the_sensitivity_profile(monkeypatch, stored, expected):
	monkeypatch.setattr(settings, "_read_doctype_row", lambda: {
		"config_profile": "Strict", "ai_refresh_max_findings": stored,
	})
	monkeypatch.setattr(settings, "_site_conf_fallback", lambda k: None)
	assert settings._resolve().ai_refresh_max_findings == expected


def test_settings_form_exposes_refresh_cap_and_existing_context_window():
	root = Path(__file__).resolve().parents[1]
	doc = json.loads((root / "optimus/doctype/optimus_settings/optimus_settings.json").read_text())
	fields = {f["fieldname"]: f for f in doc["fields"]}
	assert fields["ai_refresh_max_findings"]["default"] == "20"
	assert fields["ai_context_tokens"]["default"] == "0"
	assert all(k in doc["field_order"] for k in ("ai_refresh_max_findings", "ai_context_tokens"))


@pytest.mark.parametrize("stored,expected", [({}, []), ({"ai_enabled": "1"}, [20]),
	({"ai_refresh_max_findings": "0"}, []), ({"ai_refresh_max_findings": "7"}, [])])
def test_upgrade_seeds_only_a_saved_single_without_the_field(monkeypatch, stored, expected):
	patch = import_module("optimus.patches.v0_12_0.seed_ai_refresh_max_findings")
	writes, cleared = [], []
	def write(doctype, field, value):
		assert doctype == "Optimus Settings" and field == "ai_refresh_max_findings"
		writes.append(value)
		stored[field] = str(value)
	fake = SimpleNamespace(db=SimpleNamespace(get_singles_dict=lambda *a: dict(stored), set_single_value=write),
		cache=SimpleNamespace(delete_value=cleared.append), clear_document_cache=lambda *a: None)
	monkeypatch.setattr(patch, "frappe", fake)
	patch.execute()
	patch.execute()
	assert writes == expected
	assert bool(cleared) == bool(expected)


def test_upgrade_patch_runs_after_schema_sync():
	text = (Path(__file__).resolve().parents[1] / "patches.txt").read_text()
	assert text.index("[post_model_sync]") < text.index("optimus.patches.v0_12_0.seed_ai_refresh_max_findings")


@pytest.mark.rq
@pytest.mark.parametrize("stage", ["read", "write", "cache", "document_cache"])
def test_seed_patch_propagates_fresh_interrupt_at_every_boundary(monkeypatch, stage):
	JobTimeoutException = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	patch = import_module("optimus.patches.v0_12_0.seed_ai_refresh_max_findings")
	original = JobTimeoutException("fake patch timeout")
	def fail(*a):
		raise original
	fake = SimpleNamespace(db=SimpleNamespace(
		get_singles_dict=fail if stage == "read" else lambda *a: {"ai_enabled": "1"},
		set_single_value=fail if stage == "write" else lambda *a: None,
	), cache=SimpleNamespace(delete_value=fail if stage == "cache" else lambda *a: None),
		clear_document_cache=fail if stage == "document_cache" else lambda *a: None)
	monkeypatch.setattr(patch, "frappe", fake)
	with pytest.raises(JobTimeoutException) as caught:
		patch.execute()
	assert caught.value is not original and caught.value.__context__ is None



def test_seed_cache_failure_reports_fixed_recovery_without_losing_saved_cap(monkeypatch, capsys):
	patch = import_module("optimus.patches.v0_12_0.seed_ai_refresh_max_findings")
	written = []
	def fail(*a):
		raise ConnectionError("fake cache detail must not be printed")
	fake = SimpleNamespace(db=SimpleNamespace(get_singles_dict=lambda *a: {"ai_enabled": "1"},
		set_single_value=lambda *a: written.append(a)), cache=SimpleNamespace(delete_value=fail))
	monkeypatch.setattr(patch, "frappe", fake)
	patch.execute()
	assert written == [("Optimus Settings", "ai_refresh_max_findings", 20)]
	output = capsys.readouterr().out
	assert "clear Settings cache" in output and "fake cache detail" not in output
