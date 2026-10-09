# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The evidence the index advisor reads (A2): DocField flags, the DocType's app, real
column types and existing indexes, read once per table per render. A table without a
DocType is skipped before get_meta, and a failed get_meta leaves no "not found"
message behind (P8, Frappe utils/messages.py:61-63, :114-118)."""

from types import SimpleNamespace

import pytest

from optimus import safe_call
from optimus.dbdialect.base import IndexInfo
from optimus.dbdialect.mariadb import MariaDBDialect
from optimus.dbdialect.postgres import PostgresDialect
from optimus.renderer import recipe_enrichment as enrich


class _JobTimeout(Exception):
	"""Stands in for rq's JobTimeoutException."""


class _Dialect:
	name = "mariadb"

	def __init__(self, types, indexes=()):
		self.types = types
		self.indexes = list(indexes)

	def column_types(self, table):
		return dict(self.types.get(table, {}))

	def existing_indexes(self, table):
		return list(self.indexes)

	def is_text_type(self, data_type):
		return MariaDBDialect().is_text_type(data_type)

	def unindexable(self, data_type):
		return MariaDBDialect().unindexable(data_type)


def _df(fieldname, fieldtype, *, length=0, search_index=0, unique=0, is_custom_field=0):
	return SimpleNamespace(
		fieldname=fieldname, fieldtype=fieldtype, length=length,
		search_index=search_index, unique=unique, is_custom_field=is_custom_field,
	)


_SI_META = SimpleNamespace(custom=0, fields=[
	_df("customer", "Link", search_index=1),
	_df("remarks", "Small Text"),
	_df("po_no", "Data", length=60, unique=1),
	_df("cf_ref", "Data", is_custom_field=1),
	_df("payload", "JSON"),
])
_SI_TYPES = {"tabSales Invoice": {
	"name": "varchar", "creation": "datetime", "customer": "varchar", "remarks": "text",
	"po_no": "varchar", "cf_ref": "varchar", "payload": "json",
}}


@pytest.fixture
def site(monkeypatch):
	"""A fake site. frappe.db, frappe.flags and frappe.local are replaced wholesale
	(Werkzeug Local proxies); get_meta appends to message_log like frappe.throw does
	and raises for the "Broken" DocType."""
	import frappe

	state = SimpleNamespace(meta_calls=[], exists_calls=[])
	flags = SimpleNamespace(mute_messages=None)
	local = SimpleNamespace(message_log=[{"message": "earlier"}])

	def exists(doctype, name=None, *args, **kwargs):
		state.exists_calls.append((doctype, name))
		return name if doctype == "DocType" and name in ("Sales Invoice", "Broken") else None

	def get_meta(doctype, *args, **kwargs):
		state.meta_calls.append((doctype, flags.mute_messages))
		if doctype == "Broken":
			local.message_log.append({"message": "DocType Broken not found"})
			raise frappe.DoesNotExistError("Broken")
		return _SI_META

	dialect = _Dialect(_SI_TYPES, [IndexInfo(name="customer_index", columns=["customer"], unique=False, leftmost="customer")])
	monkeypatch.setattr(frappe, "db", SimpleNamespace(exists=exists))
	monkeypatch.setattr(frappe, "flags", flags)
	monkeypatch.setattr(frappe, "local", local)
	monkeypatch.setattr(frappe, "get_meta", get_meta, raising=False)
	monkeypatch.setattr(frappe, "get_doctype_app", lambda doctype: "erpnext", raising=False)
	monkeypatch.setattr(enrich, "get_dialect", lambda: dialect)
	state.flags, state.local, state.dialect = flags, local, dialect
	return state


def test_evidence_carries_flags_app_types_and_indexes(site):
	ev = enrich.make_evidence_lookup()("tabSales Invoice")
	assert (ev.table, ev.doctype, ev.app, ev.is_custom_doctype, ev.dialect) == (
		"tabSales Invoice", "Sales Invoice", "erpnext", False, "mariadb",
	)
	assert ev.fields["customer"] == enrich.FieldEvidence("Link", 0, True, False, False)
	assert ev.fields["po_no"] == enrich.FieldEvidence("Data", 60, False, True, False)
	assert ev.fields["cf_ref"].is_custom_field is True
	assert ev.column_types["remarks"] == "text"
	assert ev.text_columns == frozenset({"remarks"})
	assert ev.unindexable_columns == frozenset({"payload"})
	assert ev.indexes == (enrich.IndexEvidence("customer_index", ("customer",), False),)


def test_a_table_without_a_doctype_is_skipped_before_get_meta(site):
	assert enrich.make_evidence_lookup()("tabSessions") is None
	assert site.exists_calls == [("DocType", "Sessions")]
	assert site.meta_calls == []
	assert site.local.message_log == [{"message": "earlier"}]


def test_a_non_doctype_table_name_is_never_looked_up(site):
	assert enrich.make_evidence_lookup()("information_schema.columns") is None
	assert site.exists_calls == []


def test_get_meta_runs_muted_and_the_flag_is_restored(site):
	site.flags.mute_messages = "caller value"
	enrich.make_evidence_lookup()("tabSales Invoice")
	assert site.meta_calls == [("Sales Invoice", True)]
	assert site.flags.mute_messages == "caller value"


def test_a_failed_get_meta_leaves_no_message_behind(site):
	assert enrich.make_evidence_lookup()("tabBroken") is None
	assert site.local.message_log == [{"message": "earlier"}]
	assert site.flags.mute_messages is None


def test_the_lookup_is_memoised_including_misses(site):
	lookup = enrich.make_evidence_lookup()
	first = lookup("tabSales Invoice")
	assert lookup("`tabSales Invoice`") is first
	assert lookup("tabSessions") is None and lookup("tabSessions") is None
	assert site.meta_calls == [("Sales Invoice", True)]
	assert site.exists_calls.count(("DocType", "Sessions")) == 1


def test_no_column_types_means_no_evidence(site):
	site.dialect.types = {}
	assert enrich.make_evidence_lookup()("tabSales Invoice") is None


def test_a_job_timeout_escapes_fresh(site, monkeypatch):
	original = _JobTimeout("deadline")

	def interrupted(table):
		raise original

	monkeypatch.setattr(safe_call, "job_timeout_types", lambda: (_JobTimeout,))
	monkeypatch.setattr(site.dialect, "column_types", interrupted)
	with pytest.raises(_JobTimeout) as caught:
		enrich.make_evidence_lookup()("tabSales Invoice")
	assert caught.value is not original and caught.value.__context__ is None


def test_a_job_timeout_inside_get_meta_restores_the_mute_flag(site, monkeypatch):
	"""Task 1 Minor: the deadline escapes fresh, and the caller's mute flag and message
	log are exactly as they were."""
	import frappe

	original = _JobTimeout("deadline")

	def interrupted(doctype, *args, **kwargs):
		site.meta_calls.append((doctype, site.flags.mute_messages))
		site.local.message_log.append({"message": "half-written"})
		raise original

	site.flags.mute_messages = "caller value"
	monkeypatch.setattr(safe_call, "job_timeout_types", lambda: (_JobTimeout,))
	monkeypatch.setattr(frappe, "get_meta", interrupted, raising=False)
	with pytest.raises(_JobTimeout) as caught:
		enrich.make_evidence_lookup()("tabSales Invoice")
	assert caught.value is not original and caught.value.__context__ is None
	assert site.meta_calls == [("Sales Invoice", True)]
	assert site.flags.mute_messages == "caller value"
	assert site.local.message_log == [{"message": "earlier"}]


@pytest.mark.parametrize("dialect,data_type,text", [
	(MariaDBDialect(), "text", True),
	(MariaDBDialect(), "longtext", True),
	(MariaDBDialect(), "varchar", False),
	(MariaDBDialect(), "json", False),
	(PostgresDialect(), "text", True),
	(PostgresDialect(), "character varying", False),
	(PostgresDialect(), "json", False),
])
def test_text_type_classification(dialect, data_type, text):
	assert dialect.is_text_type(data_type) is text


# --- an index read that came back empty is a failed read -------------------------------
# Every DocType table has a primary key on name, so a table with columns and no index at
# all means the index read failed: the dialect turns an ordinary SQL error into an empty
# list. Advice built on that list would give code for an index that may exist already.


@pytest.fixture
def logged(monkeypatch):
	out = []
	monkeypatch.setattr(enrich, "log_error_line", out.append)
	return out


def test_an_empty_index_list_on_a_table_with_columns_is_a_failed_read(site, logged):
	site.dialect.indexes = []
	lookup = enrich.make_evidence_lookup()
	assert lookup("tabSales Invoice") is None
	assert lookup.read_failed("tabSales Invoice")
	assert logged == ["optimus: evidence read failed for tabSales Invoice: EmptyIndexList"]


def test_a_failed_index_read_never_gives_code(site, logged):
	from optimus.renderer import index_recipes

	site.dialect.indexes = []
	lookup = enrich.make_evidence_lookup()
	advice = index_recipes.advise_finding(
		{"finding_type": "Missing Index", "technical_detail": {"table": "tabSales Invoice", "column": "customer"}},
		evidence_lookup=lookup,
	)
	assert advice.route == index_recipes.ROUTE_NO_CODE and advice.code is None
	assert "could not read the details" in advice.reason


class _ShowIndexFails:
	"""A MariaDB frappe.db whose SHOW INDEX raises ``exc`` (a lock wait, a lost connection,
	an RQ job timeout) and whose information_schema read works."""

	db_type = "mariadb"

	def __init__(self, exc):
		self.exc = exc

	def exists(self, doctype, name=None, *args, **kwargs):
		return name if doctype == "DocType" and name == "Sales Invoice" else None

	def sql(self, query, values=None, as_dict=False, **kwargs):
		if " ".join(str(query).split()).startswith("SHOW INDEX"):
			raise self.exc
		return [{"column_name": "name", "data_type": "varchar"}, {"column_name": "customer", "data_type": "varchar"}]


def test_a_real_mariadb_index_read_that_raises_is_a_failed_read(site, logged, monkeypatch):
	import frappe

	monkeypatch.setattr(frappe, "db", _ShowIndexFails(RuntimeError("(1205, 'Lock wait timeout exceeded')")))
	monkeypatch.setattr(enrich, "get_dialect", lambda: MariaDBDialect())
	lookup = enrich.make_evidence_lookup()
	assert lookup("tabSales Invoice") is None and lookup.read_failed("tabSales Invoice")


def test_a_job_timeout_in_the_real_index_read_escapes_fresh(site, monkeypatch):
	import frappe

	original = _JobTimeout("deadline")
	monkeypatch.setattr(safe_call, "job_timeout_types", lambda: (_JobTimeout,))
	monkeypatch.setattr(frappe, "db", _ShowIndexFails(original))
	monkeypatch.setattr(enrich, "get_dialect", lambda: MariaDBDialect())
	with pytest.raises(_JobTimeout) as caught:
		enrich.make_evidence_lookup()("tabSales Invoice")
	assert caught.value is not original and caught.value.__context__ is None


class _PostgresDb:
	"""A Postgres frappe.db that records savepoint calls; ``fail`` names the call that raises."""

	db_type = "postgres"

	def __init__(self, fail=None, rollback_error=None):
		self.fail = fail
		self.rollback_error = rollback_error
		self.calls = []

	def savepoint(self, name):
		self.calls.append(("savepoint", name))

	def release_savepoint(self, name):
		self.calls.append(("release", name))

	def rollback(self, save_point=None, **kwargs):
		self.calls.append(("rollback", save_point))
		if self.rollback_error is not None:
			raise self.rollback_error

	def exists(self, doctype, name=None, *args, **kwargs):
		if self.fail == "exists":
			raise RuntimeError("current transaction is aborted")
		return name if doctype == "DocType" and name == "Sales Invoice" else None


@pytest.mark.parametrize("fail", ["exists", "get_meta"])
def test_on_postgres_a_failed_read_rolls_back_to_its_savepoint(site, logged, monkeypatch, fail):
	"""On Postgres one failed statement aborts the whole transaction, so the whole read runs
	under a savepoint and a failure rolls back to it before the read counts as failed."""
	import frappe

	db = _PostgresDb(fail=fail)
	monkeypatch.setattr(frappe, "db", db)
	site.dialect.name = "postgres"
	if fail == "get_meta":
		monkeypatch.setattr(frappe, "get_meta", _boom_meta, raising=False)
	lookup = enrich.make_evidence_lookup()
	assert lookup("tabSales Invoice") is None and lookup.read_failed("tabSales Invoice")
	(opened,) = [name for call, name in db.calls if call == "savepoint"]
	assert db.calls[-1] == ("rollback", opened) and ("release", opened) not in db.calls


def _boom_meta(*args, **kwargs):
	raise RuntimeError("get_meta failed")


def test_on_postgres_a_good_read_releases_its_savepoint(site, monkeypatch):
	import frappe

	db = _PostgresDb()
	monkeypatch.setattr(frappe, "db", db)
	site.dialect.name = "postgres"
	evidence = enrich.make_evidence_lookup()("tabSales Invoice")
	assert evidence is not None and evidence.dialect == "postgres"
	(opened,) = [name for call, name in db.calls if call == "savepoint"]
	assert db.calls == [("savepoint", opened), ("release", opened)]


def test_on_mariadb_the_read_takes_no_savepoint(site):
	import frappe

	assert not hasattr(frappe.db, "savepoint")  # the fake would raise if the read asked for one
	assert enrich.make_evidence_lookup()("tabSales Invoice") is not None


def test_on_postgres_a_failed_rollback_keeps_the_read_failure(site, logged, monkeypatch):
	"""A rollback that raises an ordinary error must not hide why the read failed."""
	import frappe

	monkeypatch.setattr(frappe, "db", _PostgresDb(fail="exists", rollback_error=ConnectionError("connection lost")))
	site.dialect.name = "postgres"
	lookup = enrich.make_evidence_lookup()
	assert lookup("tabSales Invoice") is None and lookup.read_failed("tabSales Invoice")
	assert logged == ["optimus: evidence read failed for tabSales Invoice: RuntimeError"]


def test_on_postgres_a_job_timeout_in_the_rollback_escapes_fresh(site, monkeypatch):
	"""A job timeout raised while rolling back to the savepoint must still stop the job."""
	import frappe

	original = _JobTimeout("deadline")
	monkeypatch.setattr(safe_call, "job_timeout_types", lambda: (_JobTimeout,))
	monkeypatch.setattr(frappe, "db", _PostgresDb(fail="exists", rollback_error=original))
	site.dialect.name = "postgres"
	with pytest.raises(_JobTimeout) as caught:
		enrich.make_evidence_lookup()("tabSales Invoice")
	assert caught.value is not original and caught.value.__context__ is None
