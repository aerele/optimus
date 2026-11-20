# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Line profiling integration tests exercise capture.start_line_profile_pass /
stop_line_profile_pass / is_active end-to-end with a fake frappe.cache so
we don't need a live Redis to confirm the Redis-backed lifecycle works.

These tests complement the pure-function tests in
test_line_profile_capture.py (aggregate_samples + serialize_stats) by
covering the impure orchestration layer.
"""

import sys
import types

import pytest

from optimus.tests.phase2_cache_fake import Cache as _FakeCache

pytestmark = pytest.mark.usefixtures("suite_functions_are_user_code")


def _install_fake_frappe(monkeypatch):
	"""Inject a minimal frappe stub into sys.modules via monkeypatch.setitem
	so the real frappe is restored at teardown. Returns the fake cache
	object the tests can introspect."""
	fake_frappe = types.ModuleType("frappe")
	fake_local = types.SimpleNamespace(cache={})
	fake_cache = _FakeCache()
	fake_frappe.local = fake_local
	fake_frappe.cache = fake_cache
	fake_frappe.flags = types.SimpleNamespace(in_test=True)
	fake_frappe.get_installed_apps = lambda: ["optimus"]
	fake_frappe.log_error = lambda *a, **k: None  # swallow
	monkeypatch.setitem(sys.modules, "frappe", fake_frappe)
	return fake_cache


# Pre-populated fake function we can resolve. Lives in this test module so
# the dotted path is stable.
def sample_target():
	x = 1
	y = x + 1
	return y


class TestPhase2Lifecycle:
	@pytest.fixture(autouse=True)
	def _fresh_state(self, monkeypatch):
		# Fresh fake frappe per test so state doesn't leak. Routed through
		# monkeypatch.setitem so the real frappe + line_profile.* modules
		# are restored at teardown (no pollution to subsequent test files).
		_install_fake_frappe(monkeypatch)
		from optimus.renderer import source
		monkeypatch.setattr(source, "_installed_apps", lambda: {"optimus"})
		# Force-reload capture so its module-level state (the
		# _resolved_fns_by_run dict) and frappe references are clean.
		# The re-import also rebinds the parent package's attribute
		# (optimus.line_profile), so that comes back at teardown too:
		# otherwise a later ``import optimus.line_profile.capture`` would
		# reach the fresh module while the code under test still uses the
		# original one from sys.modules.
		for name in list(sys.modules):
			if name.startswith("optimus.line_profile"):
				parent, _, leaf = name.rpartition(".")
				if parent in sys.modules:
					monkeypatch.setattr(sys.modules[parent], leaf, sys.modules[name], raising=False)
				monkeypatch.delitem(sys.modules, name, raising=False)

	def test_start_rejects_when_no_picks_eligible(self):
		from optimus.line_profile import capture

		if not capture._LP_AVAILABLE:
			import pytest
			pytest.skip("line_profiler not installed; integration covered under bench")

		# `len` is a C-extension builtin picker rejects it.
		picks = [{"dotted_path": "builtins.len", "source": "freeform"}]

		import pytest
		with pytest.raises(capture.CaptureError):
			capture.start_line_profile_pass(
				session_uuid="s", run_uuid="r1", user="a@b.c", picks=picks,
			)

	def test_cleanup_run_drops_redis_keys_and_worker_cache(self):
		from optimus.line_profile import capture

		if not capture._LP_AVAILABLE:
			import pytest
			pytest.skip("line_profiler not installed; integration covered under bench")

		picks = [{"dotted_path": __name__ + ".sample_target", "source": "freeform"}]
		capture.start_line_profile_pass(
			session_uuid="s", run_uuid="r1", user="a@b.c", picks=picks,
		)
		capture._get_or_resolve_picks("r1")  # populate worker cache

		assert "r1" in capture._resolved_fns_by_run

		capture.cleanup_run("r1")

		import frappe
		assert frappe.cache.get_value("profiler:lp:r1:picks") is None
		assert frappe.cache.get_value("profiler:lp:r1:source") is None
		assert "r1" not in capture._resolved_fns_by_run
