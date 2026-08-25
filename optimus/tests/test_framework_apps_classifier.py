# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Unit tests for the shared is_framework_callsite() classifier.

v0.5.2: extended the framework-app filter to cover every official
Frappe-maintained app (erpnext, hrms, payments, lms, helpdesk,
insights, crm, builder, wiki, drive) so findings rooted inside those
apps route into the collapsed Observations subsection instead of the
actionable Findings list. Triggered by a production Sales Invoice
Save+Submit session that surfaced 10 'Redundant cache lookup'
findings landing in apps/erpnext/.../sales_invoice.py:300 — the
application developer can't patch ERPNext from their bench.

These tests pin the classifier's behavior so regressions (e.g. an
unrelated refactor removing an app from FRAMEWORK_APPS) surface
immediately.
"""

import pytest

from optimus.analyzers.base import (
	FRAMEWORK_APPS,
	is_framework_callsite,
)


class TestFrameworkAppsMembership:
	def test_frappe_core_in_set(self):
		assert "frappe" in FRAMEWORK_APPS
		assert "optimus" in FRAMEWORK_APPS

	def test_all_official_apps_present(self):
		"""Pins the full list so accidentally dropping one (e.g.
		hrms) re-introduces noise for users of that app."""
		expected = {
			"frappe", "optimus",
			"erpnext", "payments", "hrms", "lms",
			"helpdesk", "insights", "crm", "builder",
			"wiki", "drive",
		}
		assert expected <= FRAMEWORK_APPS, (
			f"FRAMEWORK_APPS missing entries: {expected - FRAMEWORK_APPS}"
		)


class TestFrappeCoreDetection:
	@pytest.mark.parametrize("path", [
		"frappe/handler.py",
		"frappe/model/document.py",
		"frappe/query_builder/utils.py",
		"apps/frappe/frappe/handler.py",
		"/Users/x/bench/apps/frappe/frappe/app.py",
		"optimus/capture.py",
		"apps/optimus/optimus/analyze.py",
	])
	def test_matches(self, path):
		assert is_framework_callsite(path) is True


class TestOfficialAppDetection:
	@pytest.mark.parametrize("path", [
		"apps/erpnext/erpnext/accounts/doctype/sales_invoice/sales_invoice.py",
		"apps/erpnext/erpnext/stock/doctype/item/item.py",
		"apps/hrms/hrms/payroll/utils.py",
		"apps/payments/payments/utils.py",
		"apps/lms/lms/lms/api.py",
		"apps/helpdesk/helpdesk/api.py",
		"apps/insights/insights/api.py",
		"apps/crm/crm/fcrm/doctype/crm_lead/crm_lead.py",
		"apps/builder/builder/api.py",
		"apps/wiki/wiki/api.py",
		"apps/drive/drive/api.py",
		# Absolute paths also match (bench installs on servers)
		"/home/frappe/bench/apps/erpnext/erpnext/controllers/accounts_controller.py",
	])
	def test_matches(self, path):
		assert is_framework_callsite(path) is True, (
			f"{path} should be classified as framework"
		)


class TestThirdPartyLibraryDetection:
	@pytest.mark.parametrize("path", [
		"env/lib/python3.14/site-packages/werkzeug/serving.py",
		"env/lib/python3.14/site-packages/gunicorn/workers/base.py",
		"/usr/lib/python3/dist-packages/requests/sessions.py",
		"something/werkzeug/routing.py",
		"something/gunicorn/app.py",
		"something/rq/worker.py",
		"pyinstrument/frame.py",
		# v0.12.x: pyinstrument-stripped paths (no site-packages/ prefix) for
		# libs that were leaking into findings. Only the unambiguous LONGER names
		# are in base's substring list — short tokens like "click" are handled by
		# call_tree's exact first-segment match instead (see test_call_tree_findings).
		"pydantic/type_adapter.py",
		"pydantic_core/_pydantic_core.py",
		"sqlparse/engine/grouping.py",
		"cryptography/hazmat/primitives/serialization.py",
	])
	def test_matches(self, path):
		assert is_framework_callsite(path) is True


class TestShortTokenNotSubstringMatched:
	"""Regression: short lib tokens must NOT substring-match in base — "click/" collided inside …/onclick/… and misrouted real findings."""

	@pytest.mark.parametrize("path", [
		"apps/myapp/onclick/handler.py",
		"apps/myapp/babel/messages.py",
		"click/core.py",  # base leaves this to call_tree's exact-match module
	])
	def test_short_token_paths_not_framework(self, path):
		assert is_framework_callsite(path) is False, (
			f"{path} must NOT be classified framework by base's substring list"
		)


class TestThirdPartyClassifiersAgree:
	"""base and call_tree's third-party lists must agree on the unambiguous libs in both, so a one-sided edit can't silently drift (short tokens like click are call_tree-only)."""

	@pytest.mark.parametrize("lib", [
		"pydantic", "pydantic_core", "sqlparse", "croniter",
		"cryptography", "num2words", "lxml", "html5lib",
		"premailer", "oauthlib",
	])
	def test_both_classifiers_treat_as_framework(self, lib):
		from optimus.analyzers.call_tree import _is_pure_helper_frame

		path = f"{lib}/core.py"
		assert is_framework_callsite(path) is True, f"base misses {lib}"
		node = {"function": "run", "filename": path, "kind": "python"}
		assert _is_pure_helper_frame(node) is True, f"call_tree misses {lib}"


class TestUserCodeNotMatched:
	@pytest.mark.parametrize("path", [
		"apps/myapp/controllers/bulk_import.py",
		"apps/my_custom_app/handlers.py",
		"apps/jewellery_erpnext/jewellery_erpnext/doctype/foo.py",
		"apps/my_erpnext_fork/custom.py",
		"apps/acme/acme/api.py",
	])
	def test_user_code_passes_through(self, path):
		assert is_framework_callsite(path) is False, (
			f"{path} should NOT be classified as framework"
		)


class TestVendoredLibSubpackageStaysUserCode:
	"""A dir under a user app named after a third-party lib (a vendored
	``apps/myapp/lxml/…``) is the user's own code — the lib substring scan must
	not misfire on it. But a stripped lib WITHOUT an ``apps/`` prefix
	(``something/werkzeug/…``) must still classify as framework (that mid-path
	match is deliberate and separately tested)."""

	@pytest.mark.parametrize("path", [
		"apps/myapp/lxml/util.py",
		"apps/myapp/pydantic/schema.py",
		"apps/acme/cryptography/signer.py",
		"/home/x/frappe-bench/apps/myapp/sqlparse/patched.py",
	])
	def test_vendored_subpackage_is_user_code(self, path):
		assert is_framework_callsite(path) is False, (
			f"{path} is a vendored subpackage under a user app, not framework"
		)

	@pytest.mark.parametrize("path", [
		"something/werkzeug/routing.py",
		"something/rq/worker.py",
		"lxml/etree.py",
	])
	def test_stripped_lib_without_apps_prefix_still_framework(self, path):
		assert is_framework_callsite(path) is True, (
			f"{path} has no user-app prefix — the lib scan must still catch it"
		)


class TestBenchUnderAppsAncestorDir:
	"""A bench installed under a path that itself contains an 'apps' ancestor
	(``/opt/apps/…``, ``/srv/apps/…``) must still resolve the REAL app after the
	last ``apps/`` — else core framework code is misread as user code."""

	@pytest.mark.parametrize("path", [
		"/opt/apps/frappe-bench/apps/erpnext/erpnext/accounts/doctype/sales_invoice/sales_invoice.py",
		"/srv/apps/bench/apps/frappe/frappe/model/document.py",
		"/data/apps/prod/apps/hrms/hrms/x.py",
	])
	def test_core_framework_under_apps_ancestor_still_framework(self, path):
		assert is_framework_callsite(path) is True, (
			f"{path} is core framework code — the last 'apps/' segment decides the app"
		)

	def test_user_app_under_apps_ancestor_still_user(self):
		# A real user app under an 'apps'-ancestor bench must stay user code.
		assert is_framework_callsite(
			"/opt/apps/frappe-bench/apps/myapp/controllers/x.py") is False


class TestAppLocalVenvIsThirdParty:
	"""A third-party lib in an app-local venv sits under ``apps/<app>/`` but is
	NOT the user's code — the site-packages check must win over the user-app
	guard."""

	def test_app_local_site_packages_is_framework(self):
		assert is_framework_callsite(
			"/home/frappe/frappe-bench/apps/myapp/.venv/lib/python3.11/"
			"site-packages/werkzeug/wrappers/response.py") is True


class TestInclusionModeBoundaryAnchored:
	"""Inclusion mode (Tracked Apps) must recognise a tracked app whose name ends
	in 'apps' — the old substring parse hid its real findings as framework."""

	def test_tracked_app_ending_in_apps_is_user_code(self):
		assert is_framework_callsite("webapps/module.py", ("webapps",)) is False
		assert is_framework_callsite(
			"apps/webapps/webapps/doctype/foo.py", ("webapps",)) is False

	def test_untracked_app_still_framework(self):
		assert is_framework_callsite("apps/other/foo.py", ("webapps",)) is True


class TestBoundaryCases:
	"""Boundary-sensitive matching: ``crm/`` must not false-positive
	on ``my_crm/``. These regressions are subtle so they get their
	own class."""

	def test_lookalike_crm(self):
		# Both should be user code.
		assert is_framework_callsite("apps/my_crm/custom.py") is False
		assert is_framework_callsite("apps/custom_crm/foo.py") is False

	def test_lookalike_erpnext(self):
		# Fork of erpnext with renamed top-level should still be user code.
		assert is_framework_callsite("apps/myerpnext_fork/foo.py") is False
		# But the jewellery_erpnext case — which is a DIFFERENT app —
		# must also be user code (it's not in the official list even
		# though its name ends with 'erpnext').
		assert is_framework_callsite("apps/jewellery_erpnext/foo.py") is False

	def test_lookalike_hrms(self):
		assert is_framework_callsite("apps/custom_hrms/foo.py") is False


class TestNilInputs:
	def test_empty_string(self):
		assert is_framework_callsite("") is False

	def test_none(self):
		assert is_framework_callsite(None) is False


class TestInclusionMode:
	"""When tracked_apps is a non-empty tuple (Optimus Settings ▸
	Tracked Apps populated), the classifier flips: framework = NOT
	in the allowlist. This lets a site admin say 'I only care about
	findings in myapp' without enumerating every framework app."""

	def test_in_allowlist_returns_false(self):
		tracked = ("myapp",)
		assert is_framework_callsite(
			"apps/myapp/myapp/controllers/foo.py",
			tracked_apps=tracked,
		) is False

	def test_not_in_allowlist_returns_true(self):
		"""Even erpnext, which is in FRAMEWORK_APPS, still returns
		True in inclusion mode — the inclusion check is the ONLY
		check when tracked_apps is set."""
		tracked = ("myapp",)
		assert is_framework_callsite(
			"apps/erpnext/erpnext/foo.py", tracked_apps=tracked,
		) is True
		# Even myapp2 (not in the allowlist) returns True.
		assert is_framework_callsite(
			"apps/myapp2/foo.py", tracked_apps=tracked,
		) is True

	def test_short_form_filename_matches(self):
		"""Pyinstrument's short-form filenames (no apps/ prefix) must
		still match the allowlist on first segment."""
		tracked = ("myapp",)
		assert is_framework_callsite(
			"myapp/controllers/foo.py", tracked_apps=tracked,
		) is False

	def test_absolute_path_matches(self):
		tracked = ("myapp",)
		assert is_framework_callsite(
			"/home/frappe/bench/apps/myapp/myapp/foo.py",
			tracked_apps=tracked,
		) is False

	def test_empty_tracked_apps_falls_back_to_exclusion(self):
		"""Empty tuple means 'no allowlist configured' — fall back to
		the built-in FRAMEWORK_APPS exclusion list."""
		assert is_framework_callsite(
			"apps/erpnext/erpnext/foo.py", tracked_apps=(),
		) is True
		assert is_framework_callsite(
			"apps/myapp/foo.py", tracked_apps=(),
		) is False

	def test_none_tracked_apps_falls_back_to_exclusion(self):
		assert is_framework_callsite(
			"apps/erpnext/erpnext/foo.py", tracked_apps=None,
		) is True

	def test_multiple_allowlisted_apps(self):
		tracked = ("myapp", "custom_invoicing", "reporting")
		for app in tracked:
			assert is_framework_callsite(
				f"apps/{app}/{app}/foo.py", tracked_apps=tracked,
			) is False
		# Un-listed still framework.
		assert is_framework_callsite(
			"apps/something_else/foo.py", tracked_apps=tracked,
		) is True

	def test_boundary_check_still_sound_in_inclusion_mode(self):
		"""tracked_apps=('crm',) must NOT match apps/my_crm/..."""
		tracked = ("crm",)
		# Real crm app: matches
		assert is_framework_callsite(
			"apps/crm/crm/foo.py", tracked_apps=tracked,
		) is False
		# Look-alike: should be treated as framework (NOT in the allowlist)
		assert is_framework_callsite(
			"apps/my_crm/foo.py", tracked_apps=tracked,
		) is True
