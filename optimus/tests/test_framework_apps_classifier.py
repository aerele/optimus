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
		# pyinstrument-stripped short forms: the lib IS the top (app-root) segment.
		"werkzeug/routing.py",
		"gunicorn/app.py",
		"rq/worker.py",
		"pyinstrument/frame.py",
		# out-of-bench absolute path (editable / vendored outside site-packages).
		"/opt/pkgs/rq/worker.py",
	])
	def test_matches(self, path):
		assert is_framework_callsite(path) is True

	@pytest.mark.parametrize("path", [
		# A lib name MID-PATH in a relative (recorder-stripped) path is a user
		# submodule, not the library — recorder callsites are ``<app>/<app>/…``.
		"something/werkzeug/routing.py",
		"something/gunicorn/app.py",
		"myapp/myapp/rq/worker.py",
	])
	def test_mid_path_lib_name_is_user_code(self, path):
		assert is_framework_callsite(path) is False


class TestWidenedLibraryCoverage:
	"""The library list was widened beyond main's original 8 fragments so a
	query looping inside a common third-party lib (pandas, redis, requests, …) is
	routed to Observations instead of blamed on the developer as an actionable
	N+1. Kept in sync with call_tree's hot-frames set so both surfaces agree."""

	@pytest.mark.parametrize("path", [
		"pandas/core/frame.py", "numpy/core/fromnumeric.py",
		"redis/client.py", "requests/sessions.py", "urllib3/connectionpool.py",
		"httpx/_client.py", "psycopg2/extensions.py", "boto3/session.py",
		"botocore/client.py", "celery/app/task.py", "jinja2/environment.py",
		"markupsafe/__init__.py", "bleach/sanitizer.py", "openpyxl/reader/excel.py",
		"PIL/Image.py", "sqlparse/engine/grouping.py",
		"cryptography/hazmat/primitives/serialization.py",
		# also matched when pyinstrument leaves the venv prefix intact
		"env/lib/python3.11/site-packages/pandas/core/frame.py",
	])
	def test_widened_libs_are_framework(self, path):
		assert is_framework_callsite(path) is True, (
			f"{path} is un-patchable library code — must be an Observation, not a user N+1"
		)

	def test_sql_and_hotframe_classifiers_agree_on_widened_libs(self):
		"""base (SQL findings) and call_tree (hot frames) recognise the same libs."""
		from optimus.analyzers.call_tree import _is_pure_helper_frame

		for lib in ("pandas", "redis", "requests", "numpy", "cryptography", "sqlparse"):
			path = f"{lib}/mod.py"
			assert is_framework_callsite(path) is True, f"base misses {lib}"
			node = {"function": "run", "filename": path, "kind": "python"}
			assert _is_pure_helper_frame(node) is True, f"call_tree misses {lib}"

	def test_user_app_named_near_a_lib_still_user(self):
		"""A user app whose name merely contains a lib token (not the lib itself)
		is still user code — the fragments carry a trailing slash."""
		assert is_framework_callsite("apps/pandas_tools/pandas_tools/api.py") is False
		assert is_framework_callsite("apps/my_redis_app/handlers.py") is False


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


class TestInclusionModeUnderAppsAncestor:
	"""Issue C: Tracked Apps (inclusion mode) must resolve the real app even when
	the bench is nested under a folder named 'apps' (/opt/apps/…, multi-bench
	servers) — else a tracked app's own findings get hidden as framework."""

	def test_tracked_app_matches_under_apps_ancestor(self):
		tracked = ("erpnext",)
		assert is_framework_callsite(
			"/opt/apps/frappe-bench/apps/erpnext/erpnext/x.py", tracked) is False
		assert is_framework_callsite(
			"/opt/apps/frappe-bench/apps/myapp/myapp/x.py", tracked) is True

	def test_tracked_app_name_ending_in_apps_not_misparsed(self):
		# An app named 'webapps' is its own top segment, not the bench apps dir.
		assert is_framework_callsite("webapps/module.py", ("webapps",)) is False


class TestFrameworkNameMatchedOnAppRootNotMidPath:
	"""A framework/lib name counts only as the resolved app ROOT, not a folder
	deeper in the path — so a user app's submodule named like a framework app, the
	standard /home/frappe/ server home, and a vendored lib under a user app all stay
	the user's own code. Regression for the mid-path substring false-positive (which
	main — and this branch before the fix — got wrong)."""

	@pytest.mark.parametrize("path", [
		"mybiz/mybiz/crm/lead_utils.py",       # submodule named like a framework app
		"acme/acme/payments/util.py",
		"/home/frappe/custom_scripts/foo.py",  # the standard Frappe server home dir
		"/home/erpnext/scripts/x.py",
		"apps/myapp/myapp/werkzeug/util.py",   # vendored lib under a user app
		"apps/acme/acme/requests/client.py",
		# recorder-stripped user modules named like a common library (the exact
		# shape frappe's recorder produces — apps/ stripped) must stay actionable.
		"myapp/myapp/requests/api.py",
		"myapp/myapp/redis/cache.py",
		"billing/billing/celery/jobs.py",
		"myapp/myapp/pandas/report.py",
	])
	def test_mid_path_framework_or_lib_name_is_user_code(self, path):
		assert is_framework_callsite(path) is False, (
			f"{path} — the framework/lib name is only mid-path, not the app root"
		)

	@pytest.mark.parametrize("path", [
		"frappe/model/document.py",                # framework at the root
		"apps/erpnext/erpnext/x.py",
		"/home/frappe/bench/apps/hrms/hrms/x.py",  # absolute, real framework root
		"werkzeug/routing.py",                     # lib at the root (top segment)
		"/opt/pkgs/rq/worker.py",                  # out-of-bench absolute lib
	])
	def test_real_framework_or_lib_still_matches(self, path):
		assert is_framework_callsite(path) is True, f"{path} is framework/lib code"


class TestSitePackagesGuardInBothModes:
	"""A venv library is un-patchable framework code in BOTH default and Tracked
	Apps mode — a lib vendored under a tracked app's own .venv must not be reported
	as an actionable user finding just because its top segment is the tracked app."""

	@pytest.mark.parametrize("path", [
		"apps/myapp/.venv/lib/python3.11/site-packages/pandas/core/frame.py",
		"myapp/.venv/lib/python3.11/site-packages/redis/client.py",  # recorder-stripped
	])
	def test_site_packages_lib_is_framework_even_under_tracked_apps(self, path):
		assert is_framework_callsite(path, ("myapp",)) is True
		assert is_framework_callsite(path) is True  # and in default mode

	def test_tracked_app_own_code_still_actionable(self):
		assert is_framework_callsite("myapp/myapp/api.py", ("myapp",)) is False


class TestUserAppWithAppsSubpackage:
	"""A user app that itself contains a subpackage literally named 'apps'
	(``myapp/apps/foo.py``) must resolve to the real app 'myapp', not the segment
	after the mid-path 'apps' — a relative mid-path '/apps/' is a user subpackage,
	not the bench apps dir (the recorder strips the bench prefix)."""

	def test_apps_subpackage_resolves_to_real_app_in_inclusion_mode(self):
		# 'myapp' is tracked → its own code (even under an 'apps' subpackage) is user.
		assert is_framework_callsite("myapp/apps/foo.py", ("myapp",)) is False
		# a NON-tracked app with an apps subpackage is still hidden.
		assert is_framework_callsite("other/apps/foo.py", ("myapp",)) is True

	def test_donut_buckets_apps_subpackage_under_real_app(self):
		from optimus.analyzers.call_tree import _top_level_app
		assert _top_level_app("f", "myapp/apps/foo.py") == "myapp"
		assert _top_level_app("f", "shop/apps/models/order.py") == "shop"


class TestThirdPartyLibSetsUnified:
	"""The SQL-findings classifier (base._THIRD_PARTY_LIB_NAMES) and the hot-frame
	classifier (call_tree._THIRD_PARTY_LIB_SEGMENTS) must be the SAME app-root set,
	so the two surfaces can never disagree on whether a library is user code. They
	drifted apart once (call_tree was missing 6 names); this pins them together."""

	def test_call_tree_reuses_base_lib_set(self):
		from optimus.analyzers import base, call_tree
		assert call_tree._THIRD_PARTY_LIB_SEGMENTS is base._THIRD_PARTY_LIB_NAMES
