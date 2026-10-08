# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The generated ensure_indexes() module runs against a fake site: every entry is
idempotent, skips quietly when its table or a column is missing (a fixture Custom Field
not synced yet), and one failing entry writes an Error Log row without stopping the
others or the migrate."""

import sys
from types import ModuleType

from optimus.renderer import index_recipes as ir

_SI_NAME = ir.optimus_index_name("Sales Invoice", ("customer", "status"))


class _Site:
	"""frappe.db for the generated module, plus make_property_setter and log_error."""

	def __init__(self, *, tables=("Sales Invoice",), columns=None, indexes=(), property_setters=(), fail_on=()):
		self.tables = set(tables)
		self.columns = {
			doctype: set(cols)
			for doctype, cols in (columns or {"Sales Invoice": ("customer", "status", "po_no", "remarks")}).items()
		}
		self.indexes = set(indexes)
		self.property_setters = set(property_setters)
		self.fail_on = set(fail_on)
		self.calls = []
		self.errors = []

	def table_exists(self, doctype, cached=True):
		self.calls.append(("table_exists", doctype, cached))
		return doctype in self.tables

	def has_column(self, doctype, column):
		return column in self.columns.get(doctype, set())

	def has_index(self, table_name, index_name):
		return (table_name, index_name) in self.indexes

	def add_index(self, doctype, fields, index_name=None):
		if index_name in self.fail_on:
			raise RuntimeError("(1071, 'Specified key was too long; max key length is 3072 bytes')")
		self.calls.append(("add_index", doctype, list(fields), index_name))
		self.indexes.add((f"tab{doctype}", index_name))

	def exists(self, doctype, filters=None, *args, **kwargs):
		return (filters["doc_type"], filters["field_name"]) in self.property_setters

	def updatedb(self, doctype):
		self.calls.append(("updatedb", doctype))
		for setter_doctype, field in self.property_setters:
			if setter_doctype == doctype:
				self.indexes.add((f"tab{doctype}", f"{field}_index"))

	def commit(self):
		self.calls.append(("commit",))

	def rollback(self):
		self.calls.append(("rollback",))

	def make_property_setter(self, doctype, fieldname, property, value, property_type, **kwargs):
		self.calls.append(("make_property_setter", doctype, fieldname, property, value, property_type))
		self.property_setters.add((doctype, fieldname))

	def log_error(self, title=None, **kwargs):
		self.errors.append(title)


def _run(entries, site, monkeypatch, *, times=1):
	frappe = ModuleType("frappe")
	frappe.db = site
	frappe.log_error = site.log_error
	setter_module = ModuleType("frappe.custom.doctype.property_setter.property_setter")
	setter_module.make_property_setter = site.make_property_setter
	for name in ("frappe.custom", "frappe.custom.doctype", "frappe.custom.doctype.property_setter"):
		monkeypatch.setitem(sys.modules, name, ModuleType(name))
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	monkeypatch.setitem(sys.modules, "frappe.custom.doctype.property_setter.property_setter", setter_module)
	namespace = {}
	exec(compile(ir.ensure_indexes_code(entries, app_name="myapp"), "optimus_indexes.py", "exec"), namespace)
	for _ in range(times):
		namespace["ensure_indexes"]()
	return [call for call in site.calls if call[0] == "add_index"]


def test_a_composite_is_created_once_under_its_short_name(monkeypatch):
	site = _Site()
	entry = {"doctype": "Sales Invoice", "columns": ["customer", "status"], "index_name": _SI_NAME}
	assert _run([entry], site, monkeypatch, times=2) == [("add_index", "Sales Invoice", ["customer", "status"], _SI_NAME)]
	assert ("table_exists", "Sales Invoice", False) in site.calls
	assert site.errors == []


def test_fixture_custom_field_not_synced_yet_skips_quietly(monkeypatch):
	"""Review Focus 1 (R-I2, D-I1): a composite on a fixture-shipped Custom Field, on a
	site where migrate has not added the column yet."""
	site = _Site()
	entry = {"doctype": "Sales Invoice", "columns": ["cf_ref", "status"], "index_name": "idx_sales_invoice_0000000a"}
	assert _run([entry], site, monkeypatch) == []
	assert site.errors == [] and ("rollback",) not in site.calls


def test_a_missing_table_is_skipped(monkeypatch):
	site = _Site(tables=())
	entry = {"doctype": "Sales Invoice", "columns": ["customer", "status"], "index_name": _SI_NAME}
	assert _run([entry], site, monkeypatch) == []
	assert site.errors == []


def test_a_failing_entry_is_logged_and_the_next_one_still_runs(monkeypatch):
	site = _Site(fail_on={"idx_sales_invoice_0000000b"})
	entries = [
		{"doctype": "Sales Invoice", "columns": ["remarks(255)", "customer"], "index_name": "idx_sales_invoice_0000000b"},
		{"doctype": "Sales Invoice", "columns": ["customer", "status"], "index_name": _SI_NAME},
	]
	assert _run(entries, site, monkeypatch) == [("add_index", "Sales Invoice", ["customer", "status"], _SI_NAME)]
	assert site.errors == ["Index for Sales Invoice was not created: idx_sales_invoice_0000000b"]
	assert ("rollback",) in site.calls


def test_another_apps_field_gets_one_property_setter_then_frappe_owns_the_index(monkeypatch):
	site = _Site()
	_run([{"doctype": "Sales Invoice", "search_index_field": "po_no"}], site, monkeypatch, times=2)
	setters = [c for c in site.calls if c[0] == "make_property_setter"]
	assert setters == [("make_property_setter", "Sales Invoice", "po_no", "search_index", 1, "Check")]
	assert [c for c in site.calls if c[0] == "updatedb"] == [("updatedb", "Sales Invoice")]
	assert ("tabSales Invoice", "po_no_index") in site.indexes


def test_a_setter_left_by_a_failed_sync_is_not_created_twice(monkeypatch):
	"""P2: the Property Setter row was committed before a failed ALTER; the next run
	only syncs the table."""
	site = _Site(property_setters={("Sales Invoice", "po_no")})
	_run([{"doctype": "Sales Invoice", "search_index_field": "po_no"}], site, monkeypatch)
	assert not [c for c in site.calls if c[0] == "make_property_setter"]
	assert ("updatedb", "Sales Invoice") in site.calls
