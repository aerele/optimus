# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the bump_cache_threshold_default patch, which bumps an existing
Optimus Settings.redundant_cache_threshold of 10 to the new default of 50 on
migrate while leaving any deliberately tuned value alone."""

import sys
import types


def _install_frappe_stub(monkeypatch):
	"""Install a minimal ``frappe`` stub via ``monkeypatch.setitem`` so pytest
	restores the real one at teardown (no cross-test pollution)."""
	stub = types.ModuleType("frappe")
	stub._single_values = {}
	stub._doctype_exists = True

	class _DB:
		def exists(self, doctype, name=None):
			if doctype == "DocType" and name is None:
				return stub._doctype_exists
			if doctype == "DocType":
				return stub._doctype_exists
			return True

		def get_single_value(self, doctype, field):
			return stub._single_values.get(field)

		def set_single_value(self, doctype, field, value):
			stub._single_values[field] = value
			return True

		def commit(self):
			pass

	stub.db = _DB()
	stub.cache = types.SimpleNamespace(
		delete_value=lambda k: None,
		get_value=lambda k: None,
		set_value=lambda k, v: None,
	)
	monkeypatch.setitem(sys.modules, "frappe", stub)
	return stub


def _import_patch():
	"""Reload the patch module so its top-level ``import frappe`` rebinds to the
	current ``sys.modules["frappe"]`` stub."""
	import importlib

	import optimus.patches.v0_5_2.bump_cache_threshold_default as patch_mod
	return importlib.reload(patch_mod)


class TestBumpCacheThreshold:
	def test_bumps_exactly_10_to_50(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		stub._single_values["redundant_cache_threshold"] = 10
		patch = _import_patch()
		patch.execute()
		assert stub._single_values["redundant_cache_threshold"] == 50

	def test_does_not_overwrite_deliberate_custom_value(self, monkeypatch):
		"""User set a custom threshold like 20 or 100 don't touch it."""
		for custom in [5, 20, 30, 75, 100, 500]:
			stub = _install_frappe_stub(monkeypatch)
			stub._single_values["redundant_cache_threshold"] = custom
			patch = _import_patch()
			patch.execute()
			assert stub._single_values["redundant_cache_threshold"] == custom, (
				f"Patch must not overwrite user-tuned value {custom}"
			)

	def test_leaves_50_alone(self, monkeypatch):
		"""Idempotent: if the value is already 50, do nothing."""
		stub = _install_frappe_stub(monkeypatch)
		stub._single_values["redundant_cache_threshold"] = 50
		patch = _import_patch()
		patch.execute()
		assert stub._single_values["redundant_cache_threshold"] == 50

	def test_no_op_when_doctype_missing(self, monkeypatch):
		"""On a fresh install where the DocType isn't synced yet, the patch is a
		no-op and doesn't raise."""
		stub = _install_frappe_stub(monkeypatch)
		stub._doctype_exists = False
		patch = _import_patch()
		patch.execute()
		# Nothing was set.
		assert stub._single_values == {}

	def test_no_op_when_value_is_none(self, monkeypatch):
		"""Defensive: a row that exists but has no value for the field must not
		crash migration."""
		stub = _install_frappe_stub(monkeypatch)
		# Explicitly leave it missing from _single_values.
		patch = _import_patch()
		patch.execute()
		# Missing field → get_single_value returns None → patch returns.
		assert "redundant_cache_threshold" not in stub._single_values

	def test_handles_non_integer_stored_value(self, monkeypatch):
		"""A non-integer stored value (legacy string data) must not crash the
		patch."""
		stub = _install_frappe_stub(monkeypatch)
		stub._single_values["redundant_cache_threshold"] = "not-a-number"
		patch = _import_patch()
		# Should NOT raise.
		patch.execute()
		# Left unchanged (wasn't exactly 10).
		assert stub._single_values["redundant_cache_threshold"] == "not-a-number"


class TestPatchRegistered:
	def test_patches_txt_lists_patch(self):
		import os
		patches_txt = os.path.join(
			os.path.dirname(__file__), "..", "patches.txt"
		)
		with open(patches_txt) as f:
			entries = f.read()
		assert (
			"optimus.patches.v0_5_2.bump_cache_threshold_default"
			in entries
		), (
			"patches.txt must register the bump_cache_threshold_default "
			"patch otherwise bench migrate won't run it"
		)
