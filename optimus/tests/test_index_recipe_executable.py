# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The generated ensure_indexes() module runs against a fake site: every entry is
idempotent, skips quietly when its table or a column is missing (a fixture Custom Field
not synced yet), runs only on the database it was written for, rolls back only its own
writes, and one failing entry writes an Error Log row without stopping the others or the
migrate. On Postgres a failed statement aborts the transaction, so nothing the module
suppresses may leave it aborted for the next hook. Index builds wait at most 300 seconds
for a lock, and the connection's own setting comes back afterwards."""

import contextlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from optimus.renderer import index_recipes as ir

_SI_NAME = ir.optimus_index_name("Sales Invoice", ("customer", "status"))
_PO_NO = {"doctype": "Sales Invoice", "search_index_field": "po_no", "db": "mariadb"}
_SETTER_WRITE = "Property Setter Sales Invoice.po_no"
_SI = "tabSales Invoice"
_LOCK_READ = "select @@session.lock_wait_timeout"
_LOCK_SET = "set session lock_wait_timeout = %s"
_PG_LOCK_READ = "select current_setting('lock_timeout')"
_PG_LOCK_SET = "select set_config('lock_timeout', %s, false)"


class InFailedSqlTransaction(Exception):
	"""psycopg2's error for any statement after a failed one in the same transaction."""


class _Site:
	"""frappe.db for the generated module, plus make_property_setter and log_error.

	Writes are transactional as in Frappe: ``commit`` keeps the pending writes,
	``rollback`` discards only them, and ``add_index`` commits before its DDL.
	On Postgres a failed statement aborts the transaction: every later statement raises
	until a rollback, and a commit then only rolls back. ``log_error`` refuses a title
	over 140 characters, as Error Log.method (Data) does on v15. ``get_column_index``
	finds only an index whose one column is the field (Frappe's MariaDB method skips an
	index with a second column). The lock wait is a MariaDB session variable; on
	Postgres a ``set_config`` is undone by a rollback until it is committed."""

	def __init__(
		self, *, tables=("Sales Invoice",), columns=None, indexes=None, property_setters=(), fail_on=(),
		db_type="mariadb", setter_error=False, invalid_other_fields=False, log_error_fails=False,
		rollback_fails=0, sql_fails=None, lock_wait=86400, in_migrate=True, build_error=RuntimeError,
	):
		self.tables = set(tables)
		self.columns = {
			doctype: set(cols)
			for doctype, cols in (columns or {"Sales Invoice": ("customer", "status", "po_no", "remarks")}).items()
		}
		# (table, index name) -> (columns, unique)
		self.indexes = {key: (tuple(cols), False) for key, cols in (indexes or {}).items()}
		self.property_setters = set(property_setters)  # (doc_type, field_name, property, value)
		self.fail_on = set(fail_on)
		self.build_error = build_error
		self.db_type = db_type
		self.setter_error = setter_error
		self.invalid_other_fields = invalid_other_fields
		self.log_error_fails = log_error_fails
		self.rollback_fails = rollback_fails
		self.sql_fails = dict(sql_fails or {})  # query -> the 1-based calls of it that fail
		self.sql_seen = {}
		self.flags = SimpleNamespace(in_install=False, in_migrate=in_migrate)
		self.lock_wait = lock_wait  # MariaDB @@session.lock_wait_timeout
		self.lock_timeout = "0"  # Postgres lock_timeout, committed
		self.lock_timeout_pending = None  # a set_config not committed yet
		self.ddl_lock = []  # (index name, the lock wait in force for its build)
		self.sync_ddl = []  # what Frappe's own schema sync changed
		self.aborted = False
		self.calls = []
		self.errors = []
		self.error_rows = {}
		self.error_log_filters = []
		self.pending = []
		self.committed = []

	# --- transactions -------------------------------------------------------------

	def _stmt(self, what):
		if self.aborted:
			raise InFailedSqlTransaction(f"current transaction is aborted, commands ignored ({what})")

	def _fail(self, error):
		if self.db_type == "postgres":
			self.aborted = True
		raise error

	def _end(self, *, commit):
		if commit and not self.aborted:
			self.committed += self.pending
			if self.lock_timeout_pending is not None:
				self.lock_timeout = self.lock_timeout_pending
		self.pending = []
		self.lock_timeout_pending = None
		self.aborted = False

	def commit(self):
		self.calls.append(("commit",))
		self._end(commit=True)

	def rollback(self):
		self.calls.append(("rollback",))
		if self.rollback_fails:
			self.rollback_fails -= 1
			raise RuntimeError("(2013, 'Lost connection to server during query')")
		self._end(commit=False)

	def write(self, what):
		"""Another hook's write in the same transaction."""
		self._stmt(what)
		self.pending.append(what)

	def _lock_in_force(self):
		if self.db_type == "postgres":
			return self.lock_timeout if self.lock_timeout_pending is None else self.lock_timeout_pending
		return self.lock_wait

	# --- frappe.db ----------------------------------------------------------------

	def sql(self, query, values=()):
		self._stmt(query)
		self.calls.append(("sql", query, tuple(values)))
		seen = self.sql_seen[query] = self.sql_seen.get(query, 0) + 1
		if seen in self.sql_fails.get(query, ()):
			self._fail(RuntimeError(f"(1227, 'Access denied') for {query}"))
		mariadb = self.db_type == "mariadb"
		if mariadb and query == _LOCK_READ:
			return ((self.lock_wait,),)
		if mariadb and query == _LOCK_SET:
			self.lock_wait = values[0]
			return ()
		if not mariadb and query == _PG_LOCK_READ:
			return ((self._lock_in_force(),),)
		if not mariadb and query == _PG_LOCK_SET:
			self.lock_timeout_pending = values[0]
			return ((values[0],),)
		self._fail(RuntimeError(f"syntax error in {query!r} on {self.db_type}"))

	def table_exists(self, doctype, cached=True):
		self._stmt("table_exists")
		self.calls.append(("table_exists", doctype, cached))
		return doctype in self.tables

	def has_column(self, doctype, column):
		self._stmt("has_column")
		return column in self.columns.get(doctype, set())

	def has_index(self, table_name, index_name):
		self._stmt("has_index")
		return (table_name, index_name) in self.indexes

	def get_column_index(self, table_name, fieldname, unique=False):
		self._stmt("get_column_index")
		self.calls.append(("get_column_index", table_name, fieldname, unique))
		for (table, name), (cols, is_unique) in self.indexes.items():
			if table == table_name and cols == (fieldname,) and is_unique == unique:
				return {"Key_name": name}
		return None

	def add_index(self, doctype, fields, index_name=None):
		table = f"tab{doctype}"
		if self.db_type == "mariadb" and self.has_index(table, index_name):
			return
		self._end(commit=True)  # Frappe commits before the DDL
		self._stmt("add_index")
		if index_name in self.fail_on:
			self._fail(self.build_error("(1071, 'Specified key was too long; max key length is 3072 bytes')"))
		self.calls.append(("add_index", doctype, list(fields), index_name))
		self.ddl_lock.append((index_name, self._lock_in_force()))
		self.indexes.setdefault((table, index_name), (tuple(f.split("(", 1)[0] for f in fields), False))
		# mariadb/database.py:428: a one-column add_index outside install and migrate adds its own setter
		if self.db_type == "mariadb" and len(fields) == 1 and not (self.flags.in_install or self.flags.in_migrate):
			self.make_property_setter(doctype, fields[0], "search_index", "1", "Check")

	def updatedb(self, doctype, meta=None):
		self.calls.append(("updatedb", doctype))

	def exists(self, doctype, filters=None, *args, **kwargs):
		self._stmt("exists")
		if doctype == "Error Log":
			self.error_log_filters.append(dict(filters))
			live = self.committed + self.pending
			return any(
				f"Error Log {title}" in live and all(row.get(key) == value for key, value in filters.items())
				for title, row in self.error_rows.items()
			)
		keys = ("doc_type", "field_name", "property", "value")
		return any(
			all(filters.get(key, value) == value for key, value in zip(keys, setter, strict=True))
			for setter in self.property_setters
		)

	# --- frappe -------------------------------------------------------------------

	def make_property_setter(self, doctype, fieldname, property, value, property_type, **kwargs):
		self._stmt("make_property_setter")
		self.calls.append(("make_property_setter", doctype, fieldname, property, value, property_type))
		if self.invalid_other_fields and kwargs.get("validate_fields_for_doctype", True):
			self._fail(RuntimeError("Options required for Link field 'x' in row 3"))
		self.pending.append(f"Property Setter {doctype}.{fieldname}")
		if self.setter_error:
			self._fail(RuntimeError("Property Setter on_update failed"))
		self.property_setters.add((doctype, fieldname, property, str(value)))

	def log_error(self, title=None, message=None, reference_doctype=None, reference_name=None, **kwargs):
		self._stmt("Error Log insert")
		if self.log_error_fails:
			self._fail(RuntimeError("Error Log insert failed"))
		if len(title or "") > 140:
			raise RuntimeError(f"CharacterLengthExceededError: Error Log method is {len(title)} > 140")
		self.errors.append(title)
		self.error_rows[title] = {
			"method": title, "reference_doctype": reference_doctype, "reference_name": reference_name,
		}
		self.pending.append(f"Error Log {title}")


def _load(entries, site, monkeypatch):
	frappe = ModuleType("frappe")
	frappe.db = site
	frappe.flags = site.flags
	frappe.log_error = site.log_error
	setter_module = ModuleType("frappe.custom.doctype.property_setter.property_setter")
	setter_module.make_property_setter = site.make_property_setter
	for name in ("frappe.custom", "frappe.custom.doctype", "frappe.custom.doctype.property_setter"):
		monkeypatch.setitem(sys.modules, name, ModuleType(name))
	monkeypatch.setitem(sys.modules, "frappe", frappe)
	monkeypatch.setitem(sys.modules, "frappe.custom.doctype.property_setter.property_setter", setter_module)
	namespace = {}
	exec(compile(ir.ensure_indexes_code(entries, app_name="myapp"), "optimus_indexes.py", "exec"), namespace)
	return namespace["ensure_indexes"]


def _run(entries, site, monkeypatch, *, times=1):
	ensure_indexes = _load(entries, site, monkeypatch)
	for _ in range(times):
		ensure_indexes()
	return _builds(site)


def _migrate(entries, site, monkeypatch):
	"""frappe.migrate's atomic post-schema step: an earlier hook's write, ensure_indexes,
	then another app's after_migrate hook; a raise rolls everything back and fails the
	migrate. Returns the error that escaped, or None."""
	ensure_indexes = _load(entries, site, monkeypatch)
	try:
		site.write("EARLIER hook")
		ensure_indexes()
		site.write("LATER hook")
		site.commit()
	except Exception as error:
		with contextlib.suppress(Exception):
			site.rollback()
		return f"{type(error).__name__}: {error}"
	return None


def _frappe_sync(site, doctype):
	"""Frappe's MariaDB schema sync of one DocType's index flags (schema.py:310-315,
	mariadb/schema.py:88-104, mariadb/database.py:337-360): a field declared indexed (here
	by a search_index Property Setter) gets ``<field>_index`` when no non-unique index
	starts with it; an undeclared field loses its single-column non-unique index."""
	table = f"tab{doctype}"
	for field in sorted(site.columns[doctype]):
		declared = (doctype, field, "search_index", "1") in site.property_setters
		leads = any(t == table and cols[0] == field and not unique for (t, _), (cols, unique) in site.indexes.items())
		single = next(
			(name for (t, name), (cols, unique) in site.indexes.items() if t == table and cols == (field,) and not unique),
			None,
		)
		if leads and not declared and single:
			del site.indexes[(table, single)]
			site.sync_ddl.append(f"DROP INDEX {single}")
		elif declared and not leads and not single:
			site.indexes[(table, f"{field}_index")] = ((field,), False)
			site.sync_ddl.append(f"ADD INDEX {field}_index")


def _builds(site):
	return [call for call in site.calls if call[0] == "add_index"]


def _setters(site):
	return [c for c in site.calls if c[0] == "make_property_setter"]


def _syncs(site):
	return [c for c in site.calls if c[0] == "updatedb"]


def _title(key, what, doctype="Sales Invoice"):
	return f"ensure_indexes: {key} {what} on {doctype}"


def test_a_composite_is_created_once_under_its_short_name(monkeypatch):
	site = _Site()
	entry = {"doctype": "Sales Invoice", "columns": ["customer", "status"], "index_name": _SI_NAME}
	assert _run([entry], site, monkeypatch, times=2) == [("add_index", "Sales Invoice", ["customer", "status"], _SI_NAME)]
	assert ("table_exists", "Sales Invoice", False) in site.calls
	assert site.errors == []


def test_fixture_custom_field_not_synced_yet_skips_quietly(monkeypatch):
	"""A composite on a fixture-shipped Custom Field, on a
	site where migrate has not added the column yet."""
	site = _Site()
	entry = {"doctype": "Sales Invoice", "columns": ["cf_ref", "status"], "index_name": "idx_sales_invoice_0000000a"}
	assert _run([entry], site, monkeypatch) == []
	assert site.errors == [] and ("rollback",) not in site.calls


def test_a_field_not_synced_yet_gets_no_index_and_no_property_setter(monkeypatch):
	"""The Search Index route has the same has_column guard as the composite route."""
	site = _Site()
	_run([{"doctype": "Sales Invoice", "search_index_field": "cf_ref", "db": "mariadb"}], site, monkeypatch)
	assert not _setters(site) and not _builds(site) and not _syncs(site)
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
	assert site.errors == [_title("idx_sales_invoice_0000000b", "was not created (RuntimeError)")]
	assert ("rollback",) in site.calls


# --- another app's single column: the index first, then the Property Setter ---


def test_another_apps_field_gets_its_index_then_one_property_setter(monkeypatch):
	"""The entry builds ``<field>_index`` itself, under the entry's guard, and only
	then declares Search Index on the field, so Frappe's schema sync keeps the index. It
	never calls updatedb (whose ALTER commits the setter first and runs unguarded on
	every later sync of the DocType once the build failed)."""
	site = _Site()
	assert _run([_PO_NO], site, monkeypatch, times=2) == [("add_index", "Sales Invoice", ["po_no"], "po_no_index")]
	assert _setters(site) == [("make_property_setter", "Sales Invoice", "po_no", "search_index", 1, "Check")]
	builds_at = site.calls.index(_builds(site)[0])
	assert builds_at < site.calls.index(_setters(site)[0])
	assert not _syncs(site)
	assert site.indexes[(_SI, "po_no_index")] == (("po_no",), False)
	assert _SETTER_WRITE in site.committed and site.errors == []


def test_a_failed_build_leaves_no_property_setter_behind(monkeypatch):
	"""A lock wait timeout on the build. No setter is written, so no later
	DocType sync, Custom Field insert or Customize Form save retries the build outside
	this guard; the next migrate tries again here."""
	site = _Site(fail_on={"po_no_index"})
	_run([_PO_NO], site, monkeypatch)
	assert not _setters(site) and _SETTER_WRITE not in site.committed + site.pending
	assert not site.property_setters and not _syncs(site)
	assert site.errors == [_title("po_no", "was not created (RuntimeError)")]
	site.fail_on.clear()
	_run([_PO_NO], site, monkeypatch)
	assert (_SI, "po_no_index") in site.indexes and _SETTER_WRITE in site.committed


@pytest.mark.parametrize("existing", ["po_no", "po_no_index", "si_po"])
def test_a_column_that_has_its_own_index_under_any_name_is_not_built_again(monkeypatch, existing):
	"""A table created with Search Index names the index ``po_no``,
	not ``po_no_index``. Any single-column index on the column counts, so nothing is
	built or synced on any migrate; the setter is written once."""
	site = _Site(indexes={(_SI, existing): ("po_no",)})
	assert _run([_PO_NO], site, monkeypatch, times=3) == []
	assert ("get_column_index", _SI, "po_no", False) in site.calls
	assert len(_setters(site)) == 1 and not _syncs(site) and site.errors == []


def test_a_setter_left_by_an_older_failed_run_gets_its_index(monkeypatch):
	"""The Property Setter row was committed by an older module before its failed
	ALTER; this one builds the index and writes no second setter."""
	site = _Site(property_setters={("Sales Invoice", "po_no", "search_index", "1")})
	assert _run([_PO_NO], site, monkeypatch) == [("add_index", "Sales Invoice", ["po_no"], "po_no_index")]
	assert not _setters(site) and not _syncs(site)


def test_another_property_setter_on_the_field_does_not_count(monkeypatch):
	"""Only a search_index = 1 setter means the field is already declared indexed."""
	site = _Site(property_setters={("Sales Invoice", "po_no", "in_list_view", "1")})
	_run([_PO_NO], site, monkeypatch)
	assert _setters(site) == [("make_property_setter", "Sales Invoice", "po_no", "search_index", 1, "Check")]
	assert (_SI, "po_no_index") in site.indexes


def test_the_property_setter_skips_validating_the_doctypes_other_fields(monkeypatch):
	"""Like Frappe's own Property Setter sync (modules/utils.py:196-203),
	another field's validation problem never blocks this setter."""
	site = _Site(invalid_other_fields=True)
	_run([_PO_NO], site, monkeypatch)
	assert site.errors == []
	assert (_SI, "po_no_index") in site.indexes and _SETTER_WRITE in site.committed


def test_run_by_hand_outside_migrate_it_still_converges(monkeypatch):
	"""bench execute: Frappe's add_index then writes its own validated setter, which a
	broken other field can fail after the index is built. The next run finds the index
	and writes the setter without that validation."""
	site = _Site(in_migrate=False, invalid_other_fields=True)
	_run([_PO_NO], site, monkeypatch)
	assert (_SI, "po_no_index") in site.indexes and not site.property_setters
	assert site.errors == [_title("po_no", "was not created (RuntimeError)")]
	_run([_PO_NO], site, monkeypatch)
	assert len(_builds(site)) == 1 and ("Sales Invoice", "po_no", "search_index", "1") in site.property_setters


@pytest.mark.parametrize("order", ["composite first", "single first"])
def test_a_composite_and_a_single_entry_on_one_column_converge_in_either_order(monkeypatch, order):
	"""Two pieces of advice from one
	report. The first migrate builds both indexes in either order (get_column_index, like
	Frappe's own check before it adds ``<field>_index``, counts only a single-column
	index); every later migrate runs no DDL, no updatedb, and Frappe's own sync between
	them changes nothing."""
	composite = {"doctype": "Sales Invoice", "columns": ["po_no", "customer"], "index_name": "idx_sales_invoice_0000000e"}
	entries = [composite, _PO_NO] if order == "composite first" else [_PO_NO, composite]
	site = _Site()
	ensure_indexes = _load(entries, site, monkeypatch)
	for migrate in range(3):
		_frappe_sync(site, "Sales Invoice")
		builds = len(_builds(site))
		ensure_indexes()
		if migrate:
			assert len(_builds(site)) == builds and not site.sync_ddl
	assert sorted(name for (_, name), _ in site.indexes.items()) == ["idx_sales_invoice_0000000e", "po_no_index"]
	assert not _syncs(site) and site.errors == [] and len(_setters(site)) == 1


def test_a_column_leading_another_apps_composite_gets_its_own_index_once(monkeypatch):
	"""On a deployed site po_no already leads a composite of another app.
	The entry builds po_no_index once and then converges."""
	site = _Site(indexes={(_SI, "po_no_customer_index"): ("po_no", "customer")})
	ensure_indexes = _load([_PO_NO], site, monkeypatch)
	for _ in range(3):
		_frappe_sync(site, "Sales Invoice")
		ensure_indexes()
	assert _builds(site) == [("add_index", "Sales Invoice", ["po_no"], "po_no_index")]
	assert not site.sync_ddl and not _syncs(site)


# --- transactions (the per-entry commit, a failed Error Log write) ---------------------


def test_a_failing_entry_rolls_back_only_its_own_writes(monkeypatch):
	"""An earlier after_migrate hook (or migrate itself) left writes
	pending; the failing entry's rollback must not discard them."""
	site = _Site(setter_error=True)
	site.pending += ["Installed Applications updated", "Website Theme saved by an earlier hook"]
	_run([_PO_NO], site, monkeypatch)
	assert "Installed Applications updated" in site.committed
	assert "Website Theme saved by an earlier hook" in site.committed
	assert _SETTER_WRITE not in site.committed + site.pending
	title = _title("po_no", "was not created (RuntimeError)")
	assert site.errors == [title]
	# the Error Log row is written after the rollback and committed, so no rollback discards it
	assert f"Error Log {title}" in site.committed


def test_each_entry_commits_its_own_writes(monkeypatch):
	"""The setter of an entry whose index already exists (no build) is committed by the
	entry itself, so a later hook's failure cannot undo it."""
	site = _Site(indexes={(_SI, "po_no_index"): ("po_no",)})
	_run([_PO_NO], site, monkeypatch)
	assert _SETTER_WRITE in site.committed and site.pending == []


@pytest.mark.parametrize("db_type", ["mariadb", "postgres"])
def test_a_rollback_that_raises_never_costs_the_error_log_row(monkeypatch, db_type):
	"""The rollback and the Error Log write are separate suppress blocks, so a
	rollback that raises (a lost connection that comes back) still leaves the row."""
	site = _Site(db_type=db_type, fail_on={_SI_NAME}, rollback_fails=1)
	entry = {"doctype": "Sales Invoice", "columns": ["customer", "status"], "index_name": _SI_NAME}
	assert _migrate([entry], site, monkeypatch) is None
	assert "LATER hook" in site.committed
	if db_type == "mariadb":
		assert f"Error Log {_title(_SI_NAME, 'was not created (RuntimeError)')}" in site.committed


@pytest.mark.parametrize("failing_last", [True, False])
@pytest.mark.parametrize("db_type", ["postgres", "mariadb"])
def test_a_failed_error_log_write_never_fails_the_migrate(monkeypatch, db_type, failing_last):
	"""The entry fails, then its Error Log insert fails with a
	database error. On Postgres that aborts the transaction; the final rollback clears
	it, so the next after_migrate hook still runs, whichever entry fails."""
	a = {"doctype": "Sales Invoice", "columns": ["customer", "status"], "index_name": _SI_NAME}
	b = {"doctype": "Sales Invoice", "columns": ["po_no", "customer"], "index_name": "idx_sales_invoice_0000000f"}
	site = _Site(db_type=db_type, fail_on={_SI_NAME}, log_error_fails=True)
	assert _migrate([b, a] if failing_last else [a, b], site, monkeypatch) is None
	assert "EARLIER hook" in site.committed and "LATER hook" in site.committed
	assert (_SI, "idx_sales_invoice_0000000f") in site.indexes


@pytest.mark.parametrize("db_type", ["postgres", "mariadb"])
def test_a_failed_skip_note_never_fails_the_migrate(monkeypatch, db_type):
	"""The db-mismatch branch: the "skipped on" Error Log write fails on the last
	entry; the next hook still runs."""
	other = "postgres" if db_type == "mariadb" else "mariadb"
	entry = {"doctype": "Sales Invoice", "columns": ["po_no"], "index_name": "idx_sales_invoice_0000000c", "db": other}
	site = _Site(db_type=db_type, log_error_fails=True)
	assert _migrate([_PO_NO if db_type == "postgres" else entry], site, monkeypatch) is None
	assert "LATER hook" in site.committed


# --- one database per entry and its one Error Log row -------------


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
	assert not _setters(site) and not _syncs(site)
	key = entry.get("index_name") or entry.get("search_index_field")
	title = _title(key, f"skipped on {site_db} (the entry is for {entry['db']})")
	assert site.errors == [title]
	assert site.error_rows[title]["reference_doctype"] == "DocType"
	assert site.error_rows[title]["reference_name"] == "Sales Invoice"


def test_the_skip_note_is_looked_up_by_indexed_columns(monkeypatch):
	"""Error Log.method has no index, so the once-only lookup also filters on
	reference_doctype and reference_name; v15 indexes the first, v16 the second."""
	site = _Site(db_type="postgres")
	_run([_PO_NO], site, monkeypatch, times=2)
	title = _title("po_no", "skipped on postgres (the entry is for mariadb)")
	expected = {"reference_doctype": "DocType", "reference_name": "Sales Invoice", "method": title}
	assert site.error_log_filters == [expected, expected]


def test_frappe_indexes_a_reference_column_of_error_log():
	"""On MariaDB the lookup's reference filter reads an index on this Frappe (v16
	indexes reference_name, v15 reference_doctype). On Postgres the Search Index is named
	after the bare field, schema-wide, so Error Log may not get it (docs say so)."""
	frappe = pytest.importorskip("frappe")
	path = Path(getattr(frappe, "__file__", "") or ".").parent / "core" / "doctype" / "error_log" / "error_log.json"
	if not path.is_file():
		pytest.skip("the real Frappe is not installed")
	fields = {f["fieldname"]: f for f in json.loads(path.read_text(encoding="utf-8"))["fields"]}
	assert any(fields[name].get("search_index") for name in ("reference_doctype", "reference_name"))
	assert not fields["method"].get("search_index")


def test_an_entry_for_this_database_runs(monkeypatch):
	site = _Site(db_type="postgres")
	entry = {"doctype": "Sales Invoice", "columns": ["po_no"], "index_name": "idx_sales_invoice_0000000c", "db": "postgres"}
	assert _run([entry], site, monkeypatch) == [("add_index", "Sales Invoice", ["po_no"], "idx_sales_invoice_0000000c")]
	assert site.errors == []
	site = _Site(db_type="mariadb")
	_run([_PO_NO], site, monkeypatch)
	assert (_SI, "po_no_index") in site.indexes and site.errors == []


# --- the Error Log row ------------------------------------


def test_the_error_log_title_starts_with_the_index_and_names_the_error(monkeypatch):
	site = _Site(fail_on={_SI_NAME})
	_run([{"doctype": "Sales Invoice", "columns": ["customer", "status"], "index_name": _SI_NAME}], site, monkeypatch)
	title = f"ensure_indexes: {_SI_NAME} was not created (RuntimeError) on Sales Invoice"
	assert site.errors == [title]
	assert site.error_rows[title]["reference_doctype"] == "DocType"
	assert site.error_rows[title]["reference_name"] == "Sales Invoice"


def test_a_long_doctype_name_keeps_the_index_name_and_the_error_type_in_its_title(monkeypatch):
	"""Error Log.method is Data(140) on v15: the title is cut to 140 characters from the
	end. The index name grows with the DocType's slug, so the key and the error type come
	first and only the DocType (also in reference_name) can be cut."""
	doctype = "Purchase Taxes and Charges Template Detail Override For Regio"
	assert len(doctype) == 61
	name = ir.optimus_index_name(doctype, ("customer", "status"))
	site = _Site(tables=(doctype,), columns={doctype: ("customer", "status")}, fail_on={name})
	_run([{"doctype": doctype, "columns": ["customer", "status"], "index_name": name}], site, monkeypatch)
	title = _title(name, "was not created (RuntimeError)", doctype)
	assert len(title) > 140
	assert site.errors == [title[:140]]
	assert site.errors[0].startswith(f"ensure_indexes: {name} was not created (RuntimeError) on ")


class CharacterLengthExceededError(Exception):
	"""A long Frappe error class name, for the worst-case title."""


def test_the_longest_key_and_a_long_error_type_both_survive_the_cut(monkeypatch):
	"""The worst case: a 64-character field (MariaDB's column limit) on a 61-character
	DocType, failing with a long error class name, and the same entry skipped on Postgres."""
	doctype = "Purchase Taxes and Charges Template Detail Override For Regio"
	field = "f" * 64
	entry = {"doctype": doctype, "search_index_field": field, "db": "mariadb"}
	site = _Site(
		tables=(doctype,), columns={doctype: (field,)}, fail_on={f"{field}_index"},
		build_error=CharacterLengthExceededError,
	)
	_run([entry], site, monkeypatch)
	[title] = site.errors
	assert len(title) <= 140
	assert title.startswith(f"ensure_indexes: {field} was not created (CharacterLengthExceededError)")
	site = _Site(db_type="postgres", tables=(doctype,), columns={doctype: (field,)})
	_run([entry], site, monkeypatch)
	[title] = site.errors
	assert title.startswith(f"ensure_indexes: {field} skipped on postgres (the entry is for mariadb)")


def test_a_failing_error_log_never_stops_the_next_entry(monkeypatch):
	site = _Site(fail_on={"idx_sales_invoice_0000000b"}, log_error_fails=True)
	entries = [
		{"doctype": "Sales Invoice", "columns": ["customer", "remarks(255)"], "index_name": "idx_sales_invoice_0000000b"},
		{"doctype": "Sales Invoice", "columns": ["customer", "status"], "index_name": _SI_NAME},
	]
	assert _run(entries, site, monkeypatch) == [("add_index", "Sales Invoice", ["customer", "status"], _SI_NAME)]


# --- the lock wait ------------------------------------------------------------------


@pytest.mark.parametrize("lock_wait", [86400, 0])
def test_mariadb_builds_wait_at_most_300_seconds_and_the_old_value_comes_back(monkeypatch, lock_wait):
	"""Install has no lock_wait_timeout cap (only v16 migrate sets 300 s), so an
	ADD INDEX on a busy table would queue every query on it for up to a day. A server
	set to 0 (never wait) gets its 0 back."""
	site = _Site(fail_on={"idx_sales_invoice_0000000f"}, lock_wait=lock_wait)
	entries = [
		{"doctype": "Sales Invoice", "columns": ["po_no", "customer"], "index_name": "idx_sales_invoice_0000000f"},
		{"doctype": "Sales Invoice", "columns": ["customer", "status"], "index_name": _SI_NAME},
		_PO_NO,
	]
	_run(entries, site, monkeypatch)
	assert site.ddl_lock == [(_SI_NAME, 300), ("po_no_index", 300)]
	assert site.lock_wait == lock_wait
	assert [c for c in site.calls if c[0] == "sql"] == [
		("sql", _LOCK_READ, ()), ("sql", _LOCK_SET, (300,)), ("sql", _LOCK_SET, (lock_wait,)),
	]


def test_postgres_builds_wait_at_most_300_seconds_even_after_a_failed_entry(monkeypatch):
	"""On Postgres: lock_timeout for the session (a SET LOCAL would end at the commit
	before each build), committed before the first entry, so a failing entry's rollback
	does not undo it; the old value comes back with the host's commit."""
	a = {"doctype": "Sales Invoice", "columns": ["customer", "status"], "index_name": _SI_NAME}
	b = {"doctype": "Sales Invoice", "columns": ["po_no", "customer"], "index_name": "idx_sales_invoice_0000000f"}
	site = _Site(db_type="postgres", fail_on={_SI_NAME})
	assert _migrate([a, b], site, monkeypatch) is None
	assert site.ddl_lock == [("idx_sales_invoice_0000000f", "300s")]
	assert site.lock_timeout == "0"


@pytest.mark.parametrize(
	("db_type", "fails"),
	[
		("mariadb", {_LOCK_READ: {1}}),
		("mariadb", {_LOCK_SET: {1}}),
		("mariadb", {_LOCK_SET: {2}}),
		("postgres", {_PG_LOCK_READ: {1}}),
		("postgres", {_PG_LOCK_SET: {1}}),
		("postgres", {_PG_LOCK_SET: {2}}),
	],
)
def test_the_lock_wait_setting_can_never_fail_the_run(monkeypatch, db_type, fails):
	"""Reading, setting or restoring the setting can fail (a missing privilege, a
	proxy); the entries still run, the earlier hook's work is kept, and on Postgres the
	failed statement leaves no aborted transaction for the next hook."""
	entry = {"doctype": "Sales Invoice", "columns": ["customer", "status"], "index_name": _SI_NAME}
	site = _Site(db_type=db_type, sql_fails=fails)
	assert _migrate([entry], site, monkeypatch) is None
	assert (_SI, _SI_NAME) in site.indexes and site.errors == []
	assert "EARLIER hook" in site.committed and "LATER hook" in site.committed


def test_an_empty_index_list_leaves_no_failed_transaction(monkeypatch):
	"""Every entry removed (docs: "Removing an index or an entry"): with no entry left to
	commit, a failed lock setting on Postgres must still be rolled back here."""
	site = _Site(db_type="postgres", sql_fails={_PG_LOCK_SET: {1}})
	assert _migrate([], site, monkeypatch) is None
	assert "EARLIER hook" in site.committed and "LATER hook" in site.committed


def test_no_lock_setting_is_touched_on_another_database(monkeypatch):
	site = _Site(db_type="sqlite")
	entry = {"doctype": "Sales Invoice", "columns": ["customer", "status"], "index_name": _SI_NAME}
	assert _run([entry], site, monkeypatch) == [("add_index", "Sales Invoice", ["customer", "status"], _SI_NAME)]
	assert not [c for c in site.calls if c[0] == "sql"] and site.errors == []


@pytest.mark.parametrize("entries", [1, 0])
def test_postgres_lock_timeout_restore_survives_a_later_hooks_rollback(monkeypatch, entries):
	"""set_config is transactional on Postgres, so an
	uncommitted restore is undone by a later after_migrate hook that rolls back its own
	failed work, and the 300 s cap would stay for the rest of the session. The module
	commits right after the restore."""
	entry = {"doctype": "Sales Invoice", "columns": ["customer", "status"], "index_name": _SI_NAME}
	site = _Site(db_type="postgres")
	ensure_indexes = _load([entry][:entries], site, monkeypatch)
	site.write("EARLIER hook")
	ensure_indexes()
	site.write("LATER hook attempt")
	site.rollback()  # the later hook failed and rolled back its own work
	site.commit()
	assert site.lock_timeout == "0"


def test_a_failed_error_log_write_leaves_one_line_on_the_console(monkeypatch, capsys):
	"""The index fails and its Error Log row cannot be written either, so
	migrate's output is the only place left to name the index and the error type."""
	entry = {"doctype": "Sales Invoice", "columns": ["customer", "status"], "index_name": _SI_NAME}
	site = _Site(fail_on={_SI_NAME}, log_error_fails=True)
	assert _migrate([entry], site, monkeypatch) is None
	out = capsys.readouterr().err
	assert _SI_NAME in out and "RuntimeError" in out and "Error Log" in out
	assert len(out.strip().splitlines()) == 1


def test_a_written_error_log_row_prints_nothing(monkeypatch, capsys):
	entry = {"doctype": "Sales Invoice", "columns": ["customer", "status"], "index_name": _SI_NAME}
	site = _Site(fail_on={_SI_NAME})
	assert _migrate([entry], site, monkeypatch) is None
	assert capsys.readouterr().err == "" and len(site.errors) == 1


def test_the_ps_route_entry_is_stamped_mariadb():
	"""PG has no get_column_index, so the Property Setter entry must never run there."""
	code = ir.ensure_indexes_code([_PO_NO], app_name="myapp")
	assert '{"doctype": "Sales Invoice", "search_index_field": "po_no", "db": "mariadb"}' in code
