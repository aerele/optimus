# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the ``set_hide_framework_tables_default`` patch.

The JSON ships ``"default": "1"`` for the ``hide_framework_tables`` Check
field, but Single rows persisted before the default was added hold 0, so the
patch flips 0/None to 1. Truthy values are left alone (idempotent).
"""

import sys
import types


def _install_frappe_stub(monkeypatch):
	"""Install a minimal ``frappe`` stub via ``monkeypatch.setitem`` so
	the real ``frappe`` is restored at test teardown (preventing
	pollution of subsequent tests in the same pytest session)."""
	stub = types.ModuleType("frappe")
	stub._single_values = {}
	stub._doctype_exists = True
	stub._committed = False
	stub._cache_invalidations = []

	class _DB:
		def exists(self, doctype, name=None):
			if doctype == "DocType":
				return stub._doctype_exists
			return True

		def get_single_value(self, doctype, field):
			return stub._single_values.get(field)

		def set_single_value(self, doctype, field, value):
			stub._single_values[field] = value
			return True

		def commit(self):
			stub._committed = True

	stub.db = _DB()
	stub.cache = types.SimpleNamespace(
		delete_value=lambda k: stub._cache_invalidations.append(k),
		get_value=lambda k: None,
		set_value=lambda k, v: None,
	)
	monkeypatch.setitem(sys.modules, "frappe", stub)
	return stub


def _import_patch(monkeypatch):
	"""Return the patch module, re-resolved against the current
	``sys.modules["frappe"]`` stub via ``importlib.reload`` (a plain ``from
	import`` won't re-execute a cached module; ``delitem`` tangles with pytest
	teardown ordering). ``monkeypatch`` is unused (kept for symmetry).
	"""
	import importlib

	import optimus.patches.v0_6_0.set_hide_framework_tables_default as patch_mod
	return importlib.reload(patch_mod)


class TestSetHideFrameworkTablesDefault:
	def test_flips_zero_to_one(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		stub._single_values["hide_framework_tables"] = 0
		patch = _import_patch(monkeypatch)
		patch.execute()
		assert stub._single_values["hide_framework_tables"] == 1
		assert stub._committed is True
		# Cache invalidated so the next request reads the new value
		# without a bench restart.
		assert "optimus_settings_cached" in stub._cache_invalidations

	def test_flips_none_to_one(self, monkeypatch):
		"""A Single row that existed before the field was added has no
		stored value for the field ``get_single_value`` returns None."""
		stub = _install_frappe_stub(monkeypatch)
		patch = _import_patch(monkeypatch)
		patch.execute()
		assert stub._single_values.get("hide_framework_tables") == 1

	def test_flips_empty_string_to_one(self, monkeypatch):
		"""Some legacy Frappe rows store Checks as ""/"" (empty) falsy."""
		stub = _install_frappe_stub(monkeypatch)
		stub._single_values["hide_framework_tables"] = ""
		patch = _import_patch(monkeypatch)
		patch.execute()
		assert stub._single_values["hide_framework_tables"] == 1

	def test_leaves_truthy_one_alone(self, monkeypatch):
		"""Idempotent: if the value is already 1, do nothing."""
		stub = _install_frappe_stub(monkeypatch)
		stub._single_values["hide_framework_tables"] = 1
		patch = _import_patch(monkeypatch)
		patch.execute()
		assert stub._single_values["hide_framework_tables"] == 1
		# No cache invalidation needed when nothing changed.
		assert stub._cache_invalidations == []
		assert stub._committed is False

	def test_leaves_string_truthy_one_alone(self, monkeypatch):
		stub = _install_frappe_stub(monkeypatch)
		stub._single_values["hide_framework_tables"] = "1"
		patch = _import_patch(monkeypatch)
		patch.execute()
		assert stub._single_values["hide_framework_tables"] == "1"

	def test_no_op_when_doctype_missing(self, monkeypatch):
		"""Fresh install where the DocType isn't yet synced: bail out."""
		stub = _install_frappe_stub(monkeypatch)
		stub._doctype_exists = False
		patch = _import_patch(monkeypatch)
		patch.execute()
		assert stub._single_values == {}
		assert stub._committed is False


class TestPatchRegistered:
	def test_patches_txt_lists_patch(self):
		import os
		patches_txt = os.path.join(
			os.path.dirname(__file__), "..", "patches.txt"
		)
		with open(patches_txt) as f:
			entries = f.read()
		assert (
			"optimus.patches.v0_6_0.set_hide_framework_tables_default"
			in entries
		), (
			"patches.txt must register the set_hide_framework_tables_default "
			"patch otherwise bench migrate won't run it"
		)


class TestJSONDefault:
	def test_json_has_explicit_default_one(self):
		"""The JSON field MUST carry ``"default": "1"``: fresh installs
		need the new default to land in the Single row on first create."""
		import json
		import os
		json_path = os.path.join(
			os.path.dirname(__file__), "..", "optimus", "doctype",
			"optimus_settings", "optimus_settings.json",
		)
		with open(json_path) as f:
			schema = json.load(f)
		field = next(
			(f for f in schema.get("fields", [])
			 if f.get("fieldname") == "hide_framework_tables"),
			None,
		)
		assert field is not None, "hide_framework_tables field missing from schema"
		assert field.get("default") == "1", (
			"hide_framework_tables must default to '1' (on) "
			"so fresh Single rows ship with the filter active"
		)
