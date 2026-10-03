"""Refresh journal schema must be private, durable and safe for upgrades."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
	"folder", ["optimus_ai_refresh_control", "optimus_ai_refresh_run", "optimus_ai_refresh_attempt"]
)
def test_journal_doctypes_are_internal_and_not_child_rows(folder):
	path = ROOT / "optimus" / "doctype" / folder / (folder + ".json")
	doc = json.loads(path.read_text())
	assert doc.get("permissions") == []
	assert not doc.get("istable") and not doc.get("issingle")
	assert doc.get("allow_rename") == 0 and doc.get("track_changes") == 0
	assert all(field.get("read_only") for field in doc["fields"])


def test_active_session_and_attempt_identities_are_unique():
	def fields(folder):
		path = ROOT / "optimus" / "doctype" / folder / (folder + ".json")
		return {f["fieldname"]: f for f in json.loads(path.read_text())["fields"]}

	run = fields("optimus_ai_refresh_run")
	attempt = fields("optimus_ai_refresh_attempt")
	assert run["active_session"]["unique"] == 1 and not run["active_session"].get("reqd")
	assert run["run_id"]["unique"] == attempt["attempt_id"]["unique"] == 1
	assert run["tokens_reported"]["fieldtype"] == attempt["tokens_reported"]["fieldtype"] == "Long Int"
	assert "settled" in attempt and "usage_complete" in attempt and "completion_counted" in run
	assert "dispatch_pending" in run and "slice_no" in run and "worker_token" in run


def test_cumulative_session_usage_can_hold_the_sum_of_valid_provider_counts():
	path = ROOT / "optimus" / "doctype" / "optimus_session" / "optimus_session.json"
	fields = {f["fieldname"]: f for f in json.loads(path.read_text())["fields"]}
	assert fields["ai_tokens_spent"]["fieldtype"] == "Long Int"
	assert fields["ai_steps_tokens"]["fieldtype"] == "Long Int"


def test_fresh_install_calls_the_same_seed_as_upgrade(monkeypatch):
	from importlib import import_module
	from types import SimpleNamespace

	from optimus import install

	patch = import_module("optimus.patches.v0_12_0.initialize_ai_refresh_store")
	seen = []
	monkeypatch.setattr(patch, "execute", lambda: seen.append(True))
	monkeypatch.setattr(install, "frappe", SimpleNamespace(db=SimpleNamespace(exists=lambda *a: True)))
	for name in (
		"_refuse_legacy_install",
		"_assign_profiler_user_to_system_managers",
		"_seed_tracked_apps_from_installed_apps",
		"_seed_ignored_apps_with_framework_apps",
	):
		monkeypatch.setattr(install, name, lambda: None)
	install.after_install()
	assert seen == [True]
	assert (
		"optimus.patches.v0_12_0.initialize_ai_refresh_store"
		in (ROOT / "patches.txt").read_text().splitlines()
	)


def test_admission_seed_is_idempotent(monkeypatch):
	from importlib import import_module
	from types import SimpleNamespace

	patch = import_module("optimus.patches.v0_12_0.initialize_ai_refresh_store")
	rows = []

	def doc(values):
		return SimpleNamespace(insert=lambda **kw: rows.append(values))

	monkeypatch.setattr(
		patch, "frappe", SimpleNamespace(db=SimpleNamespace(exists=lambda *a: bool(rows)), get_doc=doc)
	)
	patch.execute()
	patch.execute()
	assert len(rows) == 1 and rows[0]["scope"] == "site"
