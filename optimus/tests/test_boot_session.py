# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the boot_session hook.

Attaches ``optimus_enabled`` to ``frappe.boot`` so the floating widget can
hide itself when the master kill-switch is off.
"""

import sys
import types

import pytest


def _fresh_bootinfo():
	return types.SimpleNamespace()


def _cfg(enabled, threshold_ms=1000.0):
	"""A stand-in for settings.get_config() carrying just the two fields
	boot_session reads."""
	return types.SimpleNamespace(enabled=enabled, large_duration_threshold_ms=threshold_ms)


@pytest.fixture(autouse=True)
def _frappe_stub(monkeypatch):
	"""Install a minimal frappe stub via monkeypatch.setitem so it is restored
	at teardown."""
	if "frappe" not in sys.modules:
		monkeypatch.setitem(sys.modules, "frappe", types.ModuleType("frappe"))


class TestBootSession:
	def test_enabled_flag_attached_when_settings_enabled(self, monkeypatch):
		from optimus import boot, settings
		monkeypatch.setattr(settings, "get_config", lambda: _cfg(True))

		bootinfo = _fresh_bootinfo()
		boot.boot_session(bootinfo)
		assert bootinfo.optimus_enabled is True

	def test_enabled_flag_false_when_settings_disabled(self, monkeypatch):
		from optimus import boot, settings
		monkeypatch.setattr(settings, "get_config", lambda: _cfg(False))

		bootinfo = _fresh_bootinfo()
		boot.boot_session(bootinfo)
		assert bootinfo.optimus_enabled is False

	def test_fails_open_on_settings_read_error(self, monkeypatch):
		"""If settings.get_config raises, default to True (fail open) rather
		than hide the widget on a settings-read error."""
		from optimus import boot, settings

		def boom():
			raise RuntimeError("cache down")

		monkeypatch.setattr(settings, "get_config", boom)

		bootinfo = _fresh_bootinfo()
		boot.boot_session(bootinfo)
		assert bootinfo.optimus_enabled is True, (
			"boot_session must fail-open a settings-read error must "
			"NOT hide the widget. Returning False here would silently "
			"break the primary UI."
		)

	def test_returns_bool_not_truthy_value(self, monkeypatch):
		"""The JS guard does a strict `=== false` comparison, so this must
		always be a Python bool, not a truthy/falsy value."""
		from optimus import boot, settings

		# config.enabled is 1 (truthy int, but not bool).
		monkeypatch.setattr(settings, "get_config", lambda: _cfg(1))
		bootinfo = _fresh_bootinfo()
		boot.boot_session(bootinfo)
		assert bootinfo.optimus_enabled is True
		assert isinstance(bootinfo.optimus_enabled, bool)

	def test_threshold_attached_from_config(self, monkeypatch):
		from optimus import boot, settings
		monkeypatch.setattr(settings, "get_config", lambda: _cfg(True, threshold_ms=500))
		bootinfo = _fresh_bootinfo()
		boot.boot_session(bootinfo)
		assert bootinfo.optimus_large_duration_threshold_ms == 500.0

	def test_threshold_failure_does_not_flip_enabled(self, monkeypatch):
		# The enabled flag and the threshold are read independently, so a failure
		# resolving the threshold must NOT flip a deliberately-disabled Optimus
		# back on (they used to share one try/except).
		from optimus import boot, settings
		monkeypatch.setattr(settings, "get_config", lambda: _cfg(False))

		def boom():
			raise RuntimeError("threshold read failed")

		monkeypatch.setattr(settings, "display_threshold_ms", boom)
		bootinfo = _fresh_bootinfo()
		boot.boot_session(bootinfo)
		assert bootinfo.optimus_enabled is False  # not flipped to True
		assert bootinfo.optimus_large_duration_threshold_ms == 1000.0  # fell back

	def test_threshold_zero_is_preserved_not_defaulted(self, monkeypatch):
		# An explicit 0 disables the seconds rollover; it must reach the client
		# as 0, not be silently bumped to 1000, so the desk picker matches a
		# report configured to stay in ms.
		from optimus import boot, settings
		monkeypatch.setattr(settings, "get_config", lambda: _cfg(True, threshold_ms=0))
		bootinfo = _fresh_bootinfo()
		boot.boot_session(bootinfo)
		assert bootinfo.optimus_large_duration_threshold_ms == 0.0


class TestHookWired:
	"""Verify hooks.py actually registers the boot_session handler."""

	def test_boot_session_entry_in_hooks(self):
		import os
		hooks_path = os.path.join(
			os.path.dirname(__file__), "..", "hooks.py"
		)
		with open(hooks_path) as f:
			content = f.read()
		assert 'boot_session = "optimus.boot.boot_session"' in content, (
			"hooks.py must register the boot_session hook without "
			"it the bootinfo.optimus_enabled flag never reaches the "
			"client and the widget can't hide itself"
		)


class TestWidgetGuard:
	"""Verify the JS guard correctly references the boot flag."""

	def test_widget_checks_profiler_enabled_before_mount(self):
		import os
		js_path = os.path.join(
			os.path.dirname(__file__),
			"..", "public", "js", "floating_widget.js",
		)
		with open(js_path) as f:
			js = f.read()
		# The guard must reference the boot flag.
		assert "frappe.boot.optimus_enabled" in js, (
			"floating_widget.js must check frappe.boot.optimus_enabled "
			"before mounting otherwise a disabled profiler still "
			"shows the widget"
		)
		# The guard must return/skip mount when the flag is False.
		assert "=== false" in js, (
			"Must use strict === false so a missing/undefined boot "
			"flag (e.g. older boot payload without this field) doesn't "
			"hide the widget fail-open shape"
		)


class TestSessionJsFormatterParity:
	"""The Desk-side duration formatter (optimus_session.js optimus_fmt_ms) must
	match the server's humanize rule: the seconds branch divides the WHOLE-
	millisecond value (Math.round(v)/1000), the same rounding the server uses, so
	the hot-path picker and the report can't show the same duration as two
	different seconds strings at a rounding boundary."""

	def _session_js(self):
		import os
		path = os.path.join(
			os.path.dirname(__file__), "..", "optimus", "doctype",
			"optimus_session", "optimus_session.js",
		)
		with open(path) as f:
			return f.read()

	def test_seconds_branch_divides_whole_ms(self):
		js = self._session_js()
		assert "(Math.round(v) / 1000).toFixed(2)" in js, (
			"optimus_fmt_ms seconds branch must divide the whole-ms value "
			"(Math.round(v)/1000) to match server humanize_duration_ms"
		)
		# The old raw-value form was the cross-surface mismatch; it must be gone.
		assert "(v / 1000).toFixed(2)" not in js
