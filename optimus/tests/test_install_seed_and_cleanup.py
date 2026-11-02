# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Install seeds, legacy refusal, and uninstall cleanup."""

import types

import pytest

from optimus import install


class FakeSettings:
	def __init__(self, tracked=(), ignored=()):
		self.tracked_apps = list(tracked)
		self.ignored_apps = list(ignored)
		self.saved = 0

	def append(self, table, row):
		getattr(self, table).append(row["app_name"])

	def save(self, ignore_permissions=False):
		assert ignore_permissions is True
		self.saved += 1


@pytest.fixture
def site(monkeypatch):
	frappe = install.frappe

	state = types.SimpleNamespace(
		exists={("DocType", "Optimus Settings")},
		installed=["frappe", "erpnext", "hrms", "my_app"],
		settings=FakeSettings(),
		errors=[],
		inserted=[],
	)

	class _Doc:
		def __init__(self, fields):
			self.fields = fields

		def insert(self, ignore_permissions=False):
			state.inserted.append(self.fields)
			return self

	monkeypatch.setattr(
		frappe,
		"db",
		types.SimpleNamespace(db_type="mariadb", exists=lambda dt, name=None: (dt, name) in state.exists),
		raising=False,
	)
	monkeypatch.setattr(frappe, "get_single", lambda dt: state.settings, raising=False)
	monkeypatch.setattr(frappe, "get_installed_apps", lambda: state.installed, raising=False)
	monkeypatch.setattr(frappe, "get_doc", lambda fields: _Doc(fields), raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda **kw: state.errors.append(kw["title"]), raising=False)
	monkeypatch.setattr(install, "safe_commit", lambda: None)

	def _throw(msg, exc=None, **kw):
		raise (exc or frappe.ValidationError)(msg)

	# Real frappe.throw and _ need a bound site.
	monkeypatch.setattr(frappe, "throw", _throw, raising=False)
	monkeypatch.setattr(install, "_", lambda msg: msg)
	return state


class TestRefuseLegacyInstall:
	def test_clean_site_passes(self, site):
		install._refuse_legacy_install()

	@pytest.mark.parametrize("legacy", [("Module Def", "Frappe Profiler"), ("DocType", "Profiler Session")])
	def test_legacy_module_or_doctype_blocks_install(self, site, legacy):
		frappe = install.frappe

		site.exists.add(legacy)

		with pytest.raises(frappe.ValidationError, match="uninstall-app frappe_profiler"):
			install._refuse_legacy_install()

	def test_db_probe_failure_is_treated_as_no_legacy(self, site, monkeypatch):
		frappe = install.frappe

		def _boom(*a, **kw):
			raise RuntimeError("db gone")

		monkeypatch.setattr(frappe, "db", types.SimpleNamespace(exists=_boom), raising=False)

		install._refuse_legacy_install()


class TestIgnoredAppsSeed:
	def test_seeds_only_installed_defaults_in_alphabetical_order(self, site):
		install._seed_ignored_apps_with_framework_apps()

		assert site.settings.ignored_apps == ["erpnext", "frappe", "hrms"]
		assert site.settings.saved == 1

	def test_never_overwrites_operator_config(self, site):
		site.settings = FakeSettings(ignored=["custom"])

		install._seed_ignored_apps_with_framework_apps()

		assert site.settings.ignored_apps == ["custom"]
		assert site.settings.saved == 0

	def test_skips_when_settings_doctype_is_not_migrated_yet(self, site):
		site.exists.clear()

		install._seed_ignored_apps_with_framework_apps()

		assert site.settings.saved == 0

	def test_no_installed_default_means_no_save(self, site):
		site.installed = ["my_app"]

		install._seed_ignored_apps_with_framework_apps()

		assert site.settings.saved == 0


class TestTrackedAppsSeed:
	def test_seeds_custom_apps_only(self, site):
		install._seed_tracked_apps_from_installed_apps()

		assert site.settings.tracked_apps == ["my_app"]
		assert site.settings.saved == 1

	def test_existing_tracked_apps_are_preserved(self, site):
		site.settings = FakeSettings(tracked=["mine"])

		install._seed_tracked_apps_from_installed_apps()

		assert site.settings.tracked_apps == ["mine"]
		assert site.settings.saved == 0

	def test_only_framework_apps_installed_means_nothing_to_seed(self, site):
		site.installed = ["frappe", "erpnext"]

		install._seed_tracked_apps_from_installed_apps()

		assert site.settings.saved == 0

	def test_skips_when_settings_doctype_is_not_migrated_yet(self, site):
		site.exists.clear()

		install._seed_tracked_apps_from_installed_apps()

		assert site.settings.saved == 0


def _roles_inserted(site):
	return [row for row in site.inserted if row["doctype"] == "Role"]


class TestAfterInstall:
	def test_creates_the_role_once_and_runs_every_seed(self, site, monkeypatch):
		monkeypatch.setattr(install, "_assign_profiler_user_to_system_managers", lambda: None)

		install.after_install()

		assert _roles_inserted(site) == [
			{"doctype": "Role", "role_name": "Optimus User", "desk_access": 1, "is_custom": 0}
		]
		assert site.settings.tracked_apps == ["my_app"]
		assert site.settings.ignored_apps == ["erpnext", "frappe", "hrms"]

	def test_existing_role_is_not_recreated(self, site, monkeypatch):
		site.exists.add(("Role", "Optimus User"))
		monkeypatch.setattr(install, "_assign_profiler_user_to_system_managers", lambda: None)

		install.after_install()

		assert _roles_inserted(site) == []

	def test_a_failing_step_is_logged_and_does_not_abort_the_install(self, site, monkeypatch):
		def _boom():
			raise RuntimeError("step failed")

		monkeypatch.setattr(install, "_assign_profiler_user_to_system_managers", _boom)
		monkeypatch.setattr(install, "_seed_tracked_apps_from_installed_apps", _boom)

		install.after_install()

		assert site.errors == [
			"optimus after_install auto-role",
			"optimus after_install tracked-apps seed",
		]
		assert site.settings.ignored_apps == ["erpnext", "frappe", "hrms"]  # later step still ran

	def test_legacy_install_aborts_before_creating_anything(self, site):
		frappe = install.frappe

		site.exists.add(("DocType", "Profiler Session"))

		with pytest.raises(frappe.ValidationError):
			install.after_install()

		assert site.inserted == []


class FakeRedis:
	"""Returns two SCAN pages."""

	def __init__(self):
		self.deleted = []
		self.fail_pattern = None

	def make_key(self, key):
		return b"site1|"

	def scan(self, cursor, match=None, count=None):
		if match == self.fail_pattern:
			raise RuntimeError("scan failed")
		if cursor == 0:
			return 7, [f"{match}-a", f"{match}-b"]
		return 0, [f"{match}-c"]

	def delete(self, *keys):
		self.deleted.extend(keys)


class TestUninstallCleanup:
	def test_clears_every_app_namespace_across_scan_pages(self, site, monkeypatch):
		frappe = install.frappe

		redis = FakeRedis()
		monkeypatch.setattr(frappe, "cache", redis, raising=False)
		monkeypatch.setattr(
			frappe, "logger", lambda: types.SimpleNamespace(info=lambda *a, **kw: None), raising=False
		)

		install._clear_redis_state()

		patterns = {k.rsplit("-", 1)[0] for k in redis.deleted}
		assert patterns == {
			"site1|profiler:*",
			"site1|optimus:*",
			"site1|optimus_settings_cached",
		}
		assert len(redis.deleted) == 9  # 3 patterns x 3 keys over two pages

	def test_one_failing_pattern_does_not_stop_the_others(self, site, monkeypatch):
		frappe = install.frappe

		redis = FakeRedis()
		redis.fail_pattern = "site1|profiler:*"
		monkeypatch.setattr(frappe, "cache", redis, raising=False)
		monkeypatch.setattr(
			frappe, "logger", lambda: types.SimpleNamespace(info=lambda *a, **kw: None), raising=False
		)

		install._clear_redis_state()

		assert len(redis.deleted) == 6

	def test_before_uninstall_survives_capture_and_redis_failures(self, site, monkeypatch):
		from optimus import capture

		def _boom(*a, **kw):
			raise RuntimeError("nope")

		monkeypatch.setattr(capture, "uninstall_wraps", _boom)
		monkeypatch.setattr(install, "_clear_redis_state", _boom)

		install.before_uninstall()

		assert site.errors == ["optimus before_uninstall capture", "optimus uninstall cleanup"]

	def test_before_uninstall_restores_the_wrapped_frappe_functions(self, site, monkeypatch):
		from optimus import capture

		calls = []
		monkeypatch.setattr(capture, "uninstall_wraps", lambda: calls.append("unwrap"))
		monkeypatch.setattr(install, "_clear_redis_state", lambda: calls.append("redis"))

		install.before_uninstall()

		assert calls == ["unwrap", "redis"]
