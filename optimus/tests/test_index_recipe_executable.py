# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The generated ensure_indexes() module runs against a fake site: every entry is
idempotent, skips quietly when its table or a column is missing (a fixture Custom Field
not synced yet), runs only on the database it was written for, rolls back only its own
writes, and one failing entry writes an Error Log row without stopping the others or the
migrate."""

import sys
from types import ModuleType

import pytest

from optimus.renderer import index_recipes as ir

_SI_NAME = ir.optimus_index_name("Sales Invoice", ("customer", "status"))
_PO_NO = {"doctype": "Sales Invoice", "search_index_field": "po_no", "db": "mariadb"}
_SETTER_WRITE = "Property Setter Sales Invoice.po_no"


class _Site:
	"""frappe.db for the generated module, plus make_property_setter and log_error.

	Writes are transactional as in Frappe: ``commit`` keeps the pending writes,
	``rollback`` discards only them, and ``updatedb`` / ``add_index`` commit before their
	DDL (sql_ddl and add_index, F1, F12). ``log_error`` refuses a title over 140
	characters, as Error Log.method (Data) does on v15."""

	def __init__(
		self, *, tables=("Sales Invoice",), columns=None, indexes=(), property_setters=(), fail_on=(),
		db_type="mariadb", setter_error=False, invalid_other_fields=False, log_error_fails=False,
	):
		self.tables = set(tables)
		self.columns = {
			doctype: set(cols)
			for doctype, cols in (columns or {"Sales Invoice": ("customer", "status", "po_no", "remarks")}).items()
		}
		self.indexes = set(indexes)
		self.property_setters = set(property_setters)  # (doc_type, field_name, property, value)
		self.fail_on = set(fail_on)
		self.db_type = db_type
		self.setter_error = setter_error
		self.invalid_other_fields = invalid_other_fields
		self.log_error_fails = log_error_fails
		self.calls = []
		self.errors = []
		self.pending = []
		self.committed = []

	def _flush(self):
		self.committed += self.pending
		self.pending = []

	def table_exists(self, doctype, cached=True):
		self.calls.append(("table_exists", doctype, cached))
		return doctype in self.tables

	def has_column(self, doctype, column):
		return column in self.columns.get(doctype, set())

	def has_index(self, table_name, index_name):
		return (table_name, index_name) in self.indexes

	def add_index(self, doctype, fields, index_name=None):
		self._flush()
		if index_name in self.fail_on:
			raise RuntimeError("(1071, 'Specified key was too long; max key length is 3072 bytes')")
		self.calls.append(("add_index", doctype, list(fields), index_name))
		self.indexes.add((f"tab{doctype}", index_name))

	def exists(self, doctype, filters=None, *args, **kwargs):
		if doctype == "Error Log":
			return filters.get("method") in self.errors
		keys = ("doc_type", "field_name", "property", "value")
		return any(
			all(filters.get(key, value) == value for key, value in zip(keys, setter, strict=True))
			for setter in self.property_setters
		)

	def updatedb(self, doctype):
		self.calls.append(("updatedb", doctype))
		self._flush()
		for setter_doctype, field, prop, _value in self.property_setters:
			if setter_doctype == doctype and prop == "search_index":
				self.indexes.add((f"tab{doctype}", f"{field}_index"))

	def commit(self):
		self.calls.append(("commit",))
		self._flush()

	def rollback(self):
		self.calls.append(("rollback",))
		self.pending = []

	def make_property_setter(self, doctype, fieldname, property, value, property_type, **kwargs):
		self.calls.append(("make_property_setter", doctype, fieldname, property, value, property_type))
		if self.invalid_other_fields and kwargs.get("validate_fields_for_doctype", True):
			raise RuntimeError("Options required for Link field 'x' in row 3")
		self.pending.append(f"Property Setter {doctype}.{fieldname}")
		if self.setter_error:
			raise RuntimeError("Property Setter on_update failed")
		self.property_setters.add((doctype, fieldname, property, str(value)))

	def log_error(self, title=None, **kwargs):
		if self.log_error_fails:
			raise RuntimeError("Error Log insert failed")
		if len(title or "") > 140:
			raise RuntimeError(f"CharacterLengthExceededError: Error Log method is {len(title)} > 140")
		self.errors.append(title)
		self.pending.append(f"Error Log {title}")


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


def _setters(site):
	return [c for c in site.calls if c[0] == "make_property_setter"]


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


def test_a_field_not_synced_yet_gets_no_property_setter(monkeypatch):
	"""The Search Index route has the same has_column guard as the composite route."""
	site = _Site()
	_run([{"doctype": "Sales Invoice", "search_index_field": "cf_ref", "db": "mariadb"}], site, monkeypatch)
	assert not _setters(site) and not [c for c in site.calls if c[0] == "updatedb"]
	assert site.errors == []


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
	_run([_PO_NO], site, monkeypatch, times=2)
	assert _setters(site) == [("make_property_setter", "Sales Invoice", "po_no", "search_index", 1, "Check")]
	assert [c for c in site.calls if c[0] == "updatedb"] == [("updatedb", "Sales Invoice")]
	assert ("tabSales Invoice", "po_no_index") in site.indexes


def test_a_setter_left_by_a_failed_sync_is_not_created_twice(monkeypatch):
	"""P2: the Property Setter row was committed before a failed ALTER; the next run
	only syncs the table."""
	site = _Site(property_setters={("Sales Invoice", "po_no", "search_index", "1")})
	_run([_PO_NO], site, monkeypatch)
	assert not _setters(site)
	assert ("updatedb", "Sales Invoice") in site.calls


def test_another_property_setter_on_the_field_does_not_count(monkeypatch):
	"""Only a search_index = 1 setter means the field is already declared indexed."""
	site = _Site(property_setters={("Sales Invoice", "po_no", "in_list_view", "1")})
	_run([_PO_NO], site, monkeypatch)
	assert _setters(site) == [("make_property_setter", "Sales Invoice", "po_no", "search_index", 1, "Check")]
	assert ("tabSales Invoice", "po_no_index") in site.indexes


def test_the_property_setter_skips_validating_the_doctypes_other_fields(monkeypatch):
	"""Fix round 1 I1: like Frappe's own Property Setter sync (modules/utils.py:196-203),
	another field's validation problem never blocks this setter."""
	site = _Site(invalid_other_fields=True)
	_run([_PO_NO], site, monkeypatch)
	assert site.errors == []
	assert ("tabSales Invoice", "po_no_index") in site.indexes


# --- transactions (fix round 1 I1 and the per-entry commit) -----------------------


def test_a_failing_entry_rolls_back_only_its_own_writes(monkeypatch):
	"""Fix round 1 I1: an earlier after_migrate hook (or migrate itself) left writes
	pending; the failing entry's rollback must not discard them."""
	site = _Site(setter_error=True)
	site.pending += ["Installed Applications updated", "Website Theme saved by an earlier hook"]
	_run([_PO_NO], site, monkeypatch)
	assert "Installed Applications updated" in site.committed
	assert "Website Theme saved by an earlier hook" in site.committed
	assert _SETTER_WRITE not in site.committed + site.pending
	assert site.errors == ["Index for Sales Invoice was not created: po_no"]


def test_each_entry_commits_its_own_writes(monkeypatch):
	"""The setter of an entry whose index already exists (no table sync) is committed
	by the entry itself, so a later hook's failure cannot undo it."""
	site = _Site(indexes={("tabSales Invoice", "po_no_index")})
	_run([_PO_NO], site, monkeypatch)
	assert _SETTER_WRITE in site.committed and site.pending == []


# --- one database per entry (fix round 1 I2) --------------------------------------


@pytest.mark.parametrize(
	("entry", "site_db"),
	[
		(_PO_NO, "postgres"),
		({"doctype": "Sales Invoice", "columns": ["remarks(255)"], "index_name": "idx_sales_invoice_0000000d", "db": "mariadb"}, "postgres"),
		({"doctype": "Sales Invoice", "columns": ["po_no"], "index_name": "idx_sales_invoice_0000000c", "db": "postgres"}, "mariadb"),
	],
)
def test_an_entry_for_another_database_is_skipped_and_logged_once(monkeypatch, entry, site_db):
	site = _Site(db_type=site_db)
	assert _run([entry], site, monkeypatch, times=2) == []
	assert not _setters(site) and not [c for c in site.calls if c[0] == "updatedb"]
	key = entry.get("index_name") or entry.get("search_index_field")
	assert site.errors == [f"Index for Sales Invoice skipped on {site_db}, the entry is for {entry['db']}: {key}"]


def test_an_entry_for_this_database_runs(monkeypatch):
	site = _Site(db_type="postgres")
	entry = {"doctype": "Sales Invoice", "columns": ["po_no"], "index_name": "idx_sales_invoice_0000000c", "db": "postgres"}
	assert _run([entry], site, monkeypatch) == [("add_index", "Sales Invoice", ["po_no"], "idx_sales_invoice_0000000c")]
	assert site.errors == []
	site = _Site(db_type="mariadb")
	_run([_PO_NO], site, monkeypatch)
	assert ("tabSales Invoice", "po_no_index") in site.indexes and site.errors == []


# --- the Error Log row (fix round 1 item 5) ---------------------------------------


def test_a_long_doctype_name_still_gets_its_error_log_row(monkeypatch):
	"""Error Log.method is Data(140) on v15: the title is cut to 140 characters."""
	doctype = "Purchase Taxes and Charges Template Detail Override For Regio"
	assert len(doctype) == 61
	name = ir.optimus_index_name(doctype, ("customer", "status"))
	site = _Site(tables=(doctype,), columns={doctype: ("customer", "status")}, fail_on={name})
	_run([{"doctype": doctype, "columns": ["customer", "status"], "index_name": name}], site, monkeypatch)
	title = f"Index for {doctype} was not created: {name}"
	assert len(title) > 140
	assert site.errors == [title[:140]]


def test_a_failing_error_log_never_stops_the_next_entry(monkeypatch):
	site = _Site(fail_on={"idx_sales_invoice_0000000b"}, log_error_fails=True)
	entries = [
		{"doctype": "Sales Invoice", "columns": ["customer", "remarks(255)"], "index_name": "idx_sales_invoice_0000000b"},
		{"doctype": "Sales Invoice", "columns": ["customer", "status"], "index_name": _SI_NAME},
	]
	assert _run(entries, site, monkeypatch) == [("add_index", "Sales Invoice", ["customer", "status"], _SI_NAME)]
