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
	assert "settled" in attempt and "usage_complete" in attempt and "completion_counted" not in run
	assert "dispatch_pending" in run and "slice_no" in run and "worker_token" in run


def test_cumulative_session_usage_can_hold_the_sum_of_valid_provider_counts():
	path = ROOT / "optimus" / "doctype" / "optimus_session" / "optimus_session.json"
	fields = {f["fieldname"]: f for f in json.loads(path.read_text())["fields"]}
	assert fields["ai_tokens_spent"]["fieldtype"] == "Long Int"


def test_the_steps_token_field_keeps_its_32_bit_type():
	"""Widening it rebuilds the Session table (MariaDB COPY, Postgres rewrite) for a count one
	reply cannot reach, and makes the column nullable. The engine clamps the value instead."""
	path = ROOT / "optimus" / "doctype" / "optimus_session" / "optimus_session.json"
	fields = {f["fieldname"]: f for f in json.loads(path.read_text())["fields"]}
	assert fields["ai_steps_tokens"]["fieldtype"] == "Int"


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
		patch,
		"frappe",
		SimpleNamespace(db=SimpleNamespace(exists=lambda *a: bool(rows), db_type="mariadb"), get_doc=doc),
	)
	patch.execute()
	patch.execute()
	assert len(rows) == 1 and rows[0]["scope"] == "site"


def test_every_migrate_seeds_the_admission_mutex_again(monkeypatch):
	"""The patch runs once. A rollback that dropped the journal tables, then a redeploy,
	left the mutex row missing with the patch already logged as done, so every user refresh
	failed closed for good."""
	from importlib import import_module
	from types import SimpleNamespace

	from optimus import hooks, install

	patch = import_module("optimus.patches.v0_12_0.initialize_ai_refresh_store")
	assert "optimus.install.after_migrate" in hooks.after_migrate
	seen, tables = [], {"Optimus AI Refresh Control"}
	monkeypatch.setattr(patch, "execute", lambda: seen.append(True))
	monkeypatch.setattr(
		install, "frappe", SimpleNamespace(db=SimpleNamespace(table_exists=lambda doctype: doctype in tables))
	)
	install.after_migrate()
	assert seen == [True]
	tables.clear()  # the journal is not installed: admission stays closed, migrate goes on
	install.after_migrate()
	assert seen == [True]


def test_the_run_journal_records_its_idle_slices():
	path = ROOT / "optimus" / "doctype" / "optimus_ai_refresh_run" / "optimus_ai_refresh_run.json"
	doc = json.loads(path.read_text())
	fields = {f["fieldname"]: f for f in doc["fields"]}
	assert fields["idle_slices"]["fieldtype"] == "Int" and fields["idle_slices"]["read_only"] == 1
	assert "idle_slices" in doc["field_order"]


def test_the_run_journal_records_the_selections_gated_and_excluded_counts():
	"""#71's two Refresh toasts need the counts of the selection; the run keeps them."""
	path = ROOT / "optimus" / "doctype" / "optimus_ai_refresh_run" / "optimus_ai_refresh_run.json"
	doc = json.loads(path.read_text())
	fields = {f["fieldname"]: f for f in doc["fields"]}
	for name in ("gated_count", "excluded_count"):
		assert fields[name]["fieldtype"] == "Int" and fields[name]["read_only"] == 1, name
		assert fields[name]["default"] == "0" and name in doc["field_order"], name
	assert [f["fieldname"] for f in doc["fields"]] == [n for n in doc["field_order"]]


# --- both journal tables keep their indexes on PostgreSQL ---------------------------------


def _index_patch(monkeypatch, *, db_type, indexes, schema="public"):
	"""The patch against a fake ``frappe``: ``indexes`` maps a table to its index definitions
	in the PostgreSQL schema ``schema``."""
	from importlib import import_module
	from types import SimpleNamespace

	patch = import_module("optimus.patches.v0_12_0.initialize_ai_refresh_store")
	created = []

	def fields(doctype):
		folder = doctype.lower().replace(" ", "_")
		doc = json.loads((ROOT / "optimus" / "doctype" / folder / (folder + ".json")).read_text())
		return [SimpleNamespace(**{"unique": 0, "search_index": 0, **f}) for f in doc["fields"]]

	def sql(query, values):
		assert "pg_indexes" in query and values[0] == schema
		return [(definition,) for definition in indexes.get(values[1], [])]

	db = SimpleNamespace(
		db_type=db_type,
		db_schema="public",
		sql=sql,
		add_index=lambda doctype, columns, index_name: created.append((doctype, columns, index_name)),
	)
	monkeypatch.setattr(
		patch, "frappe", SimpleNamespace(db=db, get_meta=lambda d: SimpleNamespace(fields=fields(d)))
	)
	return patch, created


def _definition(column, table="tabOptimus AI Refresh Run"):
	return f'CREATE INDEX {column} ON public."{table}" USING btree ({column})'


def test_the_attempt_table_gets_the_session_index_frappe_skipped(monkeypatch):
	run = "tabOptimus AI Refresh Run"
	patch, created = _index_patch(
		monkeypatch,
		db_type="postgres",
		indexes={
			run: [_definition(c) for c in ("session_name", "requested_by", "state", "resume_from")],
			"tabOptimus AI Refresh Attempt": [_definition("run_id", "tabOptimus AI Refresh Attempt")],
		},
	)
	patch.ensure_indexes()
	assert created == [
		(
			"Optimus AI Refresh Attempt",
			["session_name"],
			"tabOptimus AI Refresh Attempt_session_name_index",
		)
	]


def test_the_run_table_gets_it_when_frappe_gave_the_name_to_the_attempt_table(monkeypatch):
	attempt = "tabOptimus AI Refresh Attempt"
	patch, created = _index_patch(
		monkeypatch,
		db_type="postgres",
		indexes={
			"tabOptimus AI Refresh Run": [_definition(c) for c in ("requested_by", "state", "resume_from")],
			attempt: [_definition("run_id", attempt), _definition("session_name", attempt)],
		},
	)
	patch.ensure_indexes()
	assert [(c[0], c[1]) for c in created] == [("Optimus AI Refresh Run", ["session_name"])]


def test_the_indexes_are_created_once_and_a_rerun_does_nothing(monkeypatch):
	patch, created = _index_patch(monkeypatch, db_type="postgres", indexes={})
	patch.ensure_indexes()
	wanted = {(c[0], c[1][0]) for c in created}
	assert wanted == {
		("Optimus AI Refresh Run", "session_name"),
		("Optimus AI Refresh Run", "requested_by"),
		("Optimus AI Refresh Run", "state"),
		("Optimus AI Refresh Run", "resume_from"),
		("Optimus AI Refresh Attempt", "run_id"),
		("Optimus AI Refresh Attempt", "session_name"),
	}
	# every name is the table's own, so two tables can never claim one name
	assert len({c[2] for c in created}) == len(created) and all(c[2].startswith("tab") for c in created)


@pytest.mark.parametrize("conf,schema", [({}, "public"), ({"db_schema": "fake_tenant"}, "fake_tenant")])
def test_on_frappe_v15_the_schema_comes_from_the_site_config(monkeypatch, conf, schema):
	"""Frappe v15's PostgreSQL database has no ``db_schema`` (v16's reads it from the site
	config): the patch raised AttributeError there, which failed ``install-app`` and
	``bench migrate``. It reads the site config itself then, as Frappe v15 does, with
	"public" when it names none."""
	patch, created = _index_patch(monkeypatch, db_type="postgres", indexes={}, schema=schema)
	del patch.frappe.db.db_schema
	patch.frappe.conf = conf
	patch.ensure_indexes()
	assert len(created) == 6 and all(c[2].startswith("tab") for c in created)


def test_mariadb_is_left_alone(monkeypatch):
	patch, created = _index_patch(monkeypatch, db_type="mariadb", indexes={})
	patch.ensure_indexes()
	assert created == []


def test_the_seed_runs_after_the_model_sync():
	"""The mutex row needs its table, so the patch must be after the schema is synced."""
	lines = (ROOT / "patches.txt").read_text().splitlines()
	name = "optimus.patches.v0_12_0.initialize_ai_refresh_store"
	assert lines.count(name) == 1
	assert "[post_model_sync]" in lines and lines.index("[post_model_sync]") < lines.index(name)
	assert "[pre_model_sync]" not in lines[lines.index("[post_model_sync]") :]


def test_the_seed_patch_creates_the_indexes_too(monkeypatch):
	"""Install, upgrade and every migrate run ``execute``: the indexes must come with the seed."""
	patch, created = _index_patch(monkeypatch, db_type="postgres", indexes={})
	patch.frappe.db.exists = lambda *a: True  # the mutex row is already seeded
	patch.execute()
	assert ("Optimus AI Refresh Attempt", ["session_name"]) in [(c[0], c[1]) for c in created]
