"""Real SQL checks of the refresh journal on disposable databases, shaped like a Frappe site.

The facade gives the journal what a Frappe site gives it, on v15 and v16:

- Frappe's DDL: each table is built from its DocType JSON with Frappe's column rules
  (``NOT NULL DEFAULT`` for Check/Int/Float, inline ``UNIQUE``), an index per
  ``search_index`` field and, on MariaDB, the ``creation`` index. On Postgres the index
  names are schema-global, as Frappe creates them.
- Frappe's queries: ``get_value`` adds ``ORDER BY creation DESC LIMIT 1`` and
  ``FOR UPDATE [NOWAIT]``; ``Document.insert`` fills the standard columns.
- Frappe's errors: ``Database.sql`` raises a MariaDB deadlock (1213) or snapshot conflict
  (1020) as ``QueryDeadlockError(e)``, a lock wait timeout (1205) as
  ``QueryTimeoutError(e)``, a Postgres deadlock (40P01) as ``QueryDeadlockError(e)`` and a
  lock-not-available (55P03) or statement timeout (57014) as ``QueryTimeoutError(e)``, and
  leaves everything else raw. ``Document.insert`` raises a duplicate primary key as
  ``DuplicateEntryError(doctype, name, e)`` and a duplicate unique column as
  ``UniqueValidationError(doctype, name, e)``.
- #72's session counters run unchanged: ``analyze._add_ai_spend`` and
  ``_bump_ai_refresh_count`` (savepoint, log-and-continue). Only Frappe's query builder is
  replaced by the same ``UPDATE`` statement.

``OPTIMUS_TEST_SQL`` selects mariadb or postgres; a configured backend never skips.
``OPTIMUS_TEST_SQL_SNAPSHOT=1`` states that the MariaDB server runs with
``innodb_snapshot_isolation=ON`` (the default since MariaDB 11.8), and a test checks it.
``OPTIMUS_TEST_SQL_PORT`` overrides the port of the fixed, fake CI credentials. On Postgres
that role must be a superuser, as CI's ``POSTGRES_USER`` is: one test sets
``deadlock_timeout``. Every table is uniquely named and owned by the test.
"""

import json
import os
import re
import secrets
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from optimus import ai_fix, ai_jobs, analyze
from optimus import ai_refresh_store as store
from optimus.line_profile import jobs as phase2

BACKEND = os.environ.get("OPTIMUS_TEST_SQL")
SNAPSHOT = os.environ.get("OPTIMUS_TEST_SQL_SNAPSHOT") == "1"
_DOCTYPES = Path(__file__).resolve().parents[1] / "optimus" / "doctype"


def _frappe_errors():
	try:
		from frappe import exceptions
	except ImportError:
		exceptions = None
	names = ("QueryDeadlockError", "QueryTimeoutError", "DuplicateEntryError", "UniqueValidationError")
	if exceptions is not None and all(hasattr(exceptions, name) for name in names):
		return {name: getattr(exceptions, name) for name in names}
	# CI installs no Frappe: the same class names, so the type checks see what a site raises.
	return {
		"QueryDeadlockError": type("QueryDeadlockError", (Exception,), {}),
		"QueryTimeoutError": type("QueryTimeoutError", (Exception,), {}),
		"DuplicateEntryError": type("DuplicateEntryError", (NameError,), {}),
		"UniqueValidationError": type("UniqueValidationError", (Exception,), {}),
	}


ERRORS = _frappe_errors()

# Frappe's type maps (frappe/database/{mariadb,postgres}/database.py) for the types used here.
_TYPES = {
	"mariadb": {
		"Data": "varchar(140)",
		"Select": "varchar(140)",
		"Link": "varchar(140)",
		"Int": "int(11)",
		"Long Int": "bigint(20)",
		"Float": "decimal(21,9)",
		"Check": "tinyint(4)",
		"Datetime": "datetime(6)",
		"Long Text": "longtext",
		"Text Editor": "longtext",
		"Attach": "text",
	},
	"postgres": {
		"Data": "varchar(140)",
		"Select": "varchar(140)",
		"Link": "varchar(140)",
		"Int": "int",
		"Long Int": "bigint",
		"Float": "decimal(21,9)",
		"Check": "smallint",
		"Datetime": "timestamp",
		"Long Text": "text",
		"Text Editor": "text",
		"Attach": "text",
	},
}
_LAYOUT = {"Section Break", "Column Break", "Tab Break", "HTML", "Button", "Table"}
_NOT_NULL = {"Check", "Int", "Currency", "Float", "Percent"}  # frappe.database.schema.NOT_NULL_TYPES


def _column(field, backend):
	"""``DbColumn.get_definition`` for one DocField."""
	fieldtype = field["fieldtype"]
	definition = _TYPES[backend][fieldtype]
	default = field.get("default")
	if fieldtype in ("Check", "Int"):
		default = str(int(default or 0))
	elif fieldtype == "Float":
		default = repr(float(default or 0))
	elif default:
		default = "'" + str(default).replace("'", "''") + "'"
	else:
		default = None
	if fieldtype in _NOT_NULL:
		definition += " NOT NULL"
	if default is not None:
		definition += " DEFAULT " + default
	if field.get("unique") and definition.split()[0] not in ("text", "longtext"):
		definition += " UNIQUE"
	return definition


def _ddl(doctype, table, backend):
	"""``MariaDBTable.create`` / ``PostgresTable.create`` for a parent DocType."""
	folder = doctype.lower().replace(" ", "_")
	meta = json.loads((_DOCTYPES / folder / (folder + ".json")).read_text())
	fields = meta["fields"]
	if meta.get("istable"):
		fields += [{"fieldname": key, "fieldtype": "Data", "search_index": key == "parent"} for key in ("parent", "parenttype", "parentfield")]
	fields = [f for f in fields if f["fieldtype"] not in _LAYOUT]
	q = "`" if backend == "mariadb" else '"'
	columns = [f"{q}{f['fieldname']}{q} {_column(f, backend)}" for f in fields]
	indexed = [f["fieldname"] for f in fields if f.get("search_index") and not f.get("unique")]
	if backend == "mariadb":
		indexes = [f"index `{name}`(`{name}`)" for name in indexed] + ["index creation(creation)"]
		return [
			f"create table `{table}` (name varchar(140) primary key, creation datetime(6), "
			"modified datetime(6), modified_by varchar(140), owner varchar(140), "
			"docstatus tinyint not null default '0', idx int not null default '0', "
			+ ", ".join(columns + indexes)
			+ ") ENGINE=InnoDB ROW_FORMAT=DYNAMIC CHARACTER SET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
		]
	return [
		f'create table "{table}" (name varchar(140) primary key, creation timestamp(6), '
		"modified timestamp(6), modified_by varchar(140), owner varchar(140), "
		"docstatus smallint not null default '0', idx bigint not null default '0', "
		+ ", ".join(columns)
		+ ")",
		*(f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}"("{name}")' for name in indexed),
	]


def _code(exc):
	"""The driver's error code: psycopg2's SQLSTATE or PyMySQL's errno."""
	return getattr(exc, "pgcode", None) or (exc.args[0] if exc.args else None)


class SQLDatabase:
	"""The ``frappe.db`` subset the journal and #72's counters use, on one connection."""

	def __init__(self, connection, tables, backend):
		self.connection, self.tables, self.backend = connection, tables, backend
		self.db_type = backend  # ``frappe.db.db_type``
		self.conflicts = []  # the driver code of every failed statement

	def quoted(self, value):
		assert re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", value)
		quote = "`" if self.backend == "mariadb" else '"'
		return quote + value + quote

	def table(self, doctype):
		return self.quoted(self.tables[doctype])

	def sql(self, query, values=(), *, as_dict=True):
		"""``Database.sql``: run one statement and raise its error the way Frappe does. Rows are
		always dicts, as ``as_dict=True`` gives them."""
		try:
			with self.connection.cursor() as cursor:
				cursor.execute(query, values)
				if not cursor.description:
					return []
				names = [col[0] for col in cursor.description]
				return [
					dict(zip(names, (float(v) if isinstance(v, Decimal) else v for v in row), strict=True))
					for row in cursor.fetchall()
				]
		except Exception as exc:
			self.conflicts.append(_code(exc))
			wrapper = self._wrapper(exc)
			if wrapper is None:
				raise
			raise wrapper(exc) from exc

	def _wrapper(self, exc):
		if self.backend == "mariadb":
			# MariaDBDatabase.is_deadlocked / is_timedout
			code = exc.args[0] if exc.args else None
			if code in (1213, 1020):
				return ERRORS["QueryDeadlockError"]
			if code == 1205:
				return ERRORS["QueryTimeoutError"]
			return None
		import psycopg2.errors
		import psycopg2.extensions

		# PostgresDatabase.is_deadlocked / is_timedout
		if getattr(exc, "pgcode", None) == "40P01":
			return ERRORS["QueryDeadlockError"]
		if isinstance(exc, psycopg2.extensions.QueryCanceledError | psycopg2.errors.LockNotAvailable):
			return ERRORS["QueryTimeoutError"]
		return None

	def get_values(
		self,
		doctype,
		filters,
		fieldname="name",
		*,
		as_dict=False,
		for_update=False,
		wait=True,
		limit=None,
		order_by="creation",
		**kw,
	):
		filters = {"name": filters} if isinstance(filters, str) else filters
		where, params = [], []
		for key, value in filters.items():
			if isinstance(value, (list, tuple)) and value[0] in ("<=", ">"):
				where.append(self.quoted(key) + " " + value[0] + " %s")
				params.append(value[1])
			elif isinstance(value, (list, tuple)):
				assert value[0] == "in" and value[1]
				where.append(self.quoted(key) + " IN (" + ",".join(["%s"] * len(value[1])) + ")")
				params.extend(value[1])
			else:
				where.append(self.quoted(key) + " = %s")
				params.append(value)
		fields = [fieldname] if isinstance(fieldname, str) else list(fieldname)
		columns = "*" if fields == ["*"] else ",".join(self.quoted(k) for k in fields)
		query = "SELECT " + columns + " FROM " + self.table(doctype) + " WHERE " + " AND ".join(where)
		if order_by:
			# Frappe's default is the newest first ("creation desc"); a caller may name it
			column, _, direction = order_by.partition(" ")
			assert direction.lower() in ("", "desc", "asc")
			query += " ORDER BY " + self.quoted(column) + " " + (direction.upper() or "DESC")
		if limit:
			query += " LIMIT " + str(int(limit))
		if for_update:
			query += " FOR UPDATE" + ("" if wait else " NOWAIT")
		rows = self.sql(query, params)
		return rows if as_dict or fields == ["*"] else [tuple(r.values()) for r in rows]

	def get_value(self, doctype, filters, fieldname="name", *, as_dict=False, **kw):
		rows = self.get_values(doctype, filters, fieldname, as_dict=True, limit=1, **kw)
		if not rows:
			return None
		return rows[0] if as_dict or fieldname == "*" else next(iter(rows[0].values()))

	def set_value(self, doctype, name, values, update_modified=True, **kw):
		values = dict(values)
		if update_modified:
			values["modified"] = datetime.now()
		self.sql(
			"UPDATE "
			+ self.table(doctype)
			+ " SET "
			+ ",".join(self.quoted(key) + "=%s" for key in values)
			+ " WHERE name=%s",
			[*values.values(), name],
		)

	def multisql(self, queries, values, *, as_dict=False):
		query = queries[self.backend]
		quote = "`" if self.backend == "mariadb" else '"'
		for table, name in self.tables.items():
			query = query.replace(quote + "tab" + table + quote, self.quoted(name))
		return self.sql(query, values)

	def table_exists(self, table):
		return table in self.tables

	def delete(self, table, filters):
		where = " AND ".join(self.quoted(key) + "=%s" for key in filters)
		self.sql("DELETE FROM " + self.quoted(self.tables[table]) + " WHERE " + where, list(filters.values()))

	def insert(self, values):
		"""``Document.insert`` (``BaseDocument.db_insert``) for a journal row."""
		values = dict(values)
		doctype = values.pop("doctype")
		now = datetime.now()
		values.update(creation=now, modified=now, owner="Administrator", modified_by="Administrator")
		try:
			self.sql(
				"INSERT INTO "
				+ self.table(doctype)
				+ " ("
				+ ",".join(self.quoted(key) for key in values)
				+ ") VALUES ("
				+ ",".join(["%s"] * len(values))
				+ ")",
				list(values.values()),
			)
		except Exception as exc:
			duplicate = self._duplicate(exc)
			if duplicate is None:
				raise
			raise ERRORS[duplicate](doctype, values["name"], exc) from None

	def _duplicate(self, exc):
		if self.backend == "mariadb":
			import pymysql

			if not (isinstance(exc, pymysql.IntegrityError) and exc.args[0] == 1062):
				return None
			return "DuplicateEntryError" if "PRIMARY" in str(exc.args[1]) else "UniqueValidationError"
		if getattr(exc, "pgcode", None) != "23505":
			return None
		return "DuplicateEntryError" if "_pkey" in str(exc.args[0]) else "UniqueValidationError"

	def savepoint(self, name):
		self.sql(f"savepoint {name}")

	def release_savepoint(self, name):
		self.sql(f"release savepoint {name}")

	def commit(self):
		self.connection.commit()

	def rollback(self, *, save_point=None):
		if save_point:
			self.sql(f"rollback to savepoint {save_point}")
		else:
			self.connection.rollback()


class _Field:
	def __init__(self, name):
		self.name = name

	def __eq__(self, value):
		return (self.name, value)

	__hash__ = object.__hash__


class _Coalesce:
	def __init__(self, field, default):
		self.field, self.default = field, default

	def __add__(self, amount):
		return (self, amount)


class _Update:
	def __init__(self, facade, table):
		self.facade, self.table, self.assignment, self.condition = facade, table, None, None

	def set(self, field, expression):
		self.assignment = (field, expression)
		return self

	def where(self, condition):
		self.condition = condition
		return self

	def run(self):
		"""The statement Frappe builds for ``analyze._session_increment_query``."""
		db = self.facade.db
		field, (coalesce, amount) = self.assignment
		assert coalesce.field is field and coalesce.default == 0
		column, (key, value) = db.quoted(field.name), self.condition
		db.sql(
			f"UPDATE {db.table(self.table)} SET {column} = COALESCE({column}, 0) + %s WHERE {db.quoted(key)} = %s",
			[amount, value],
		)


class _QueryBuilder:
	def __init__(self, facade):
		self.facade = facade

	def DocType(self, name):
		return _Table(name)

	def update(self, table):
		return _Update(self.facade, table.name)


class _Table:
	def __init__(self, name):
		self.name = name

	def __getitem__(self, field):
		return _Field(field)


class Facade:
	def __init__(self, primary):
		self.local_ = threading.local()
		self.primary = primary
		self.qb = _QueryBuilder(self)
		self.conf, self.local, self.flags = {}, SimpleNamespace(), SimpleNamespace()
		self.enqueued = []
		for name, cls in ERRORS.items():
			setattr(self, name, cls)

	@property
	def db(self):
		return getattr(self.local_, "db", self.primary)

	@db.setter
	def db(self, value):
		self.local_.db = value

	def get_doc(self, values):
		return SimpleNamespace(insert=lambda **kw: self.db.insert(values))

	def get_all(self, doctype, *, filters, fields, limit_page_length=0, **kw):
		"""``frappe.get_all``: a plain read (the recovery sweep's candidates)."""
		return self.db.get_values(
			doctype, filters, fields, as_dict=True, order_by=None, limit=limit_page_length or None
		)

	def enqueue(self, method, **kw):
		self.enqueued.append((method, kw))


@pytest.fixture
def sql(monkeypatch):
	from datetime import datetime

	monkeypatch.setattr("frappe.utils.now_datetime", lambda: datetime(2026, 10, 14, 12))
	if not BACKEND:
		pytest.skip("Requires a disposable SQL validation service")
	assert BACKEND in {"mariadb", "postgres"}
	prefix = "opr_" + secrets.token_hex(6)
	tables = {
		doctype: prefix + "_" + suffix
		for doctype, suffix in (
			("Optimus Session", "session"),
			("Optimus Phase Two Run", "phase2"),
			(store.RUN, "run"),
			(store.ATTEMPT, "attempt"),
			(store.CONTROL, "control"),
		)
	}
	tables["answer"] = prefix + "_answer"
	connections = []

	def connect():
		# Deliberately fixed fake CI credentials/database, never a bench config or arbitrary
		# DSN that could accidentally select production data. Only the port may change.
		if BACKEND == "mariadb":
			import pymysql

			connection = pymysql.connect(
				host="127.0.0.1",
				port=int(os.environ.get("OPTIMUS_TEST_SQL_PORT") or 3306),
				user="root",
				password="optimus_test",
				database="optimus_test",
				autocommit=False,
				connect_timeout=5,
			)
		else:
			import psycopg2

			connection = psycopg2.connect(
				host="127.0.0.1",
				port=int(os.environ.get("OPTIMUS_TEST_SQL_PORT") or 5432),
				user="optimus_test",
				password="optimus_test",
				dbname="optimus_test",
				connect_timeout=5,
			)
		db = SQLDatabase(connection, tables, BACKEND)
		if BACKEND == "mariadb":
			db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
			db.sql("SET SESSION innodb_lock_wait_timeout=10")
		else:
			# PostgresDatabase.connect: ISOLATION_LEVEL_REPEATABLE_READ
			connection.set_session(isolation_level="REPEATABLE READ")
		connection.commit()
		connections.append(db)
		return db

	primary = connect()
	facade = Facade(primary)
	counter_rows = []
	monkeypatch.setattr(store, "frappe", facade)
	monkeypatch.setattr(ai_jobs, "frappe", facade)
	monkeypatch.setattr(analyze, "frappe", facade)
	monkeypatch.setattr(ai_jobs, "safe_commit", lambda: facade.db.commit())
	monkeypatch.setattr(phase2, "frappe", facade)
	monkeypatch.setattr(phase2, "_authorized", lambda *a: True)
	monkeypatch.setattr(
		ai_fix, "log_ai_failure", lambda title, exc=None, **kw: counter_rows.append((title, exc, kw))
	)
	functions = ModuleType("frappe.query_builder.functions")
	functions.Coalesce = _Coalesce
	monkeypatch.setitem(sys.modules, "frappe.query_builder", ModuleType("frappe.query_builder"))
	monkeypatch.setitem(sys.modules, "frappe.query_builder.functions", functions)
	created = []
	try:
		for doctype in ("Optimus Session", phase2.TABLE, store.RUN, store.ATTEMPT, store.CONTROL):
			for statement in _ddl(doctype, tables[doctype], BACKEND):
				primary.sql(statement)
			created.append(tables[doctype])
		primary.sql(
			f"create table {primary.table('answer')} (name varchar(140) primary key, body varchar(140))"
		)
		created.append(tables["answer"])
		primary.insert({"doctype": store.CONTROL, "name": "site", "scope": "site", "generation": 0})
		for name in ("parent-a", "parent-b", "parent-c"):
			primary.insert(
				{
					"doctype": "Optimus Session",
					"name": name,
					"session_uuid": "fake-" + name,
					"status": "Ready",
				}
			)
		for i in range(40):
			primary.sql(f"insert into {primary.table('answer')} values (%s, '')", [f"row-{i:02d}"])
		primary.commit()

		@contextmanager
		def using(db):
			old = facade.db
			facade.db = db
			try:
				yield db
			finally:
				facade.db = old

		yield SimpleNamespace(
			primary=primary, connect=connect, using=using, facade=facade, logged=counter_rows
		)
	finally:
		for db in connections:
			db.rollback()
		for table in reversed(created):
			primary.sql("DROP TABLE " + primary.quoted(table))
		primary.commit()
		for db in connections:
			db.connection.close()


def admit(name="run-a", parent="parent-a", cap=5, scope="all", user="fake-user", now=10, user_cap=5, **kw):
	return store.admit(
		run_id=name,
		docname=parent,
		session_uuid="fake-" + parent,
		requested_by=user,
		scope=scope,
		now=now,
		max_seconds=3600,
		site_cap=cap,
		user_cap=user_cap,
		cap=20,
		include_fixes=True,
		include_steps=False,
		regenerate_all=False,
		**kw,
	)


def claimed(name="run-a", parent="parent-a", worker="worker-a"):
	assert ai_jobs._retry_sql(lambda: admit(name, parent))["status"] == "queued"
	return ai_jobs._retry_sql(
		lambda: store.claim(name, docname=parent, slice_no=0, worker_token=worker, now=11, lease_seconds=300)
	)


def begun(name="run-a", parent="parent-a", worker="worker-a", target="finding-a"):
	return ai_jobs._retry_sql(
		lambda: store.begin_attempt(
			name, docname=parent, worker_token=worker, kind="fix", target=target, input_hash="fake", now=12
		)
	)


def settle(attempt, name="run-a", parent="parent-a", worker="worker-a", tokens=7, persist=None):
	return lambda: store.settle_attempt(
		name,
		attempt["name"],
		docname=parent,
		worker_token=worker,
		outcome="succeeded",
		tokens=tokens,
		usage_complete=True,
		now=13,
		persist=persist,
	)


def save(sql, attempt, tokens=7, **kw):
	return ai_jobs._settle(
		settle(attempt, tokens=tokens, **kw),
		run_id="run-a",
		docname="parent-a",
		attempt_id=attempt["name"],
		tokens=tokens,
		session_uuid="fake-parent-a",
	)


def value(sql, doctype, name, field):
	"""A committed value, read in a fresh transaction of the primary connection."""
	sql.primary.commit()
	out = sql.primary.get_value(doctype, name, field, order_by=None if doctype == "answer" else "creation")
	sql.primary.commit()
	return out


_BLOCKED = {
	# A locking statement still running on another connection while every lock holder is
	# paused: it waits for a row lock. (A primary-key read that waits during the optimizer's
	# "Statistics" stage has no INNODB_TRX entry yet, so the process list is the reliable view.)
	"mariadb": (
		"SELECT COUNT(*) AS n FROM information_schema.PROCESSLIST "
		"WHERE ID <> CONNECTION_ID() AND DB = DATABASE() AND COMMAND = 'Query' AND INFO IS NOT NULL "
		"AND (INFO LIKE '%%FOR UPDATE%%' OR INFO LIKE 'UPDATE %%' OR INFO LIKE 'INSERT %%')"
	),
	"postgres": (
		"SELECT COUNT(*) AS n FROM pg_stat_activity "
		"WHERE datname = current_database() AND wait_event_type = 'Lock'"
	),
}


def _lock_waiters(sql):
	if not hasattr(sql, "probe"):
		sql.probe = sql.connect()
	probe = sql.probe
	count = probe.sql(_BLOCKED[BACKEND])[0]["n"]
	probe.commit()
	return count


def wait_until_blocked(sql):
	"""Until a transaction waits for a row lock (the server's own lock views)."""
	for _ in range(1000):
		if _lock_waiters(sql):
			return
		time.sleep(0.01)
	raise AssertionError("no transaction waited for the lock")


def finished_or_blocked(sql, thread):
	"""Until ``thread`` ended or some transaction waits for a row lock."""
	for _ in range(1000):
		if not thread.is_alive():
			return
		if _lock_waiters(sql):
			return
		time.sleep(0.01)
	raise AssertionError("the thread neither finished nor waited")


def ends_without_waiting(sql, thread, streak=100):
	"""True when ``thread`` ends; False once a lock wait lasted ``streak`` polls in a row (a
	running statement ends within a few, a lock wait lasts until its holder is released)."""
	waiting = 0
	for _ in range(5000):
		if not thread.is_alive():
			return True
		waiting = waiting + 1 if _lock_waiters(sql) else 0
		if waiting >= streak:
			return False
		time.sleep(0.005)
	raise AssertionError("the thread neither ended nor waited")


def in_thread(sql, name, fn):
	"""Run ``fn`` on its own connection in a thread; the result or the raised error."""
	out = {}

	def target():
		db = sql.connect()
		out["db"] = db
		sql.facade.db = db
		try:
			out["value"] = fn()
		except Exception as exc:
			out["error"] = exc

	thread = threading.Thread(target=target, name=name)
	thread.start()
	return thread, out


def test_the_database_runs_the_isolation_the_leg_claims(sql):
	if BACKEND == "mariadb":
		rows = sql.primary.sql(
			"SHOW SESSION VARIABLES WHERE Variable_name IN "
			"('innodb_snapshot_isolation', 'tx_isolation', 'transaction_isolation')"
		)
		variables = {row["Variable_name"]: row["Value"] for row in rows}
		assert (variables["innodb_snapshot_isolation"] == "ON") is SNAPSHOT
		assert variables.get("transaction_isolation", variables.get("tx_isolation")) == "REPEATABLE-READ"
	else:
		assert sql.primary.sql("SHOW transaction_isolation")[0]["transaction_isolation"] == "repeatable read"


def test_an_older_snapshot_cannot_admit_beyond_site_capacity(sql):
	older = sql.connect()
	older.get_value("Optimus Session", "parent-b", "status")  # opens a read view
	assert ai_jobs._retry_sql(lambda: admit(cap=1))["status"] == "queued"
	with sql.using(older):
		assert ai_jobs._retry_sql(lambda: admit("run-b", "parent-b", cap=1)) == {
			"status": "refused",
			"reason": "site_cap",
		}
	# Exact without a retry: the old read view is committed away before the admission, so
	# neither a 1020 (MariaDB 11.8) nor a 40001 (Postgres) is needed to see run-a.
	assert older.conflicts == []


def test_an_admission_waiting_on_its_session_still_counts_a_run_admitted_meanwhile(sql, monkeypatch):
	"""B's transaction starts (its session lock), A admits and commits, then B counts: the
	count is a plain read after B takes the site mutex, so it sees A. On Postgres B's
	snapshot is older than A's commit: the mutex row A changed fails B with 40001, and the
	retry counts A."""
	paused, resume = threading.Event(), threading.Event()
	real_read = store._read

	def read(table, name, **kw):
		row = real_read(table, name, **kw)
		if threading.current_thread().name == "admission-b" and table == "Optimus Session":
			if not paused.is_set():
				paused.set()
				assert resume.wait(10)
		return row

	monkeypatch.setattr(store, "_read", read)
	thread, b = in_thread(
		sql, "admission-b", lambda: ai_jobs._retry_sql(lambda: admit("run-b", "parent-b", cap=1))
	)
	assert paused.wait(10)
	assert ai_jobs._retry_sql(lambda: admit("run-a", "parent-a", cap=1))["status"] == "queued"
	resume.set()
	thread.join(30)
	assert b["value"] == {"status": "refused", "reason": "site_cap"}
	if BACKEND == "postgres":
		assert "40001" in b["db"].conflicts
	else:
		assert not b["db"].conflicts


def test_phase2_commit_fences_an_older_ai_admission_snapshot(sql):
	older = sql.connect()
	older.get_value("Optimus Session", "parent-a", "status")
	def reserve():
		assert store.lock_phase2_parent("parent-a") is None
		sql.primary.insert({"doctype": "Optimus Phase Two Run", "name": "phase2-a",
			"parent": "parent-a", "status": "Recording"})
		phase2._write({"name": "phase2-a", "parent": "parent-a"}, status="Recording")
	ai_jobs._transaction(reserve)
	with sql.using(older):
		assert ai_jobs._retry_sql(admit) == {"status": "refused", "reason": "phase2"}
	assert older.conflicts == []  # the public SQL wrapper closes the old snapshot first


def test_ai_admission_fences_an_older_phase2_snapshot(sql):
	older = sql.connect()
	older.get_value("Optimus Session", "parent-a", "status")
	assert ai_jobs._retry_sql(admit)["status"] == "queued"
	with sql.using(older):
		assert ai_jobs._retry_sql(lambda: store.lock_phase2_parent("parent-a")) == "ai_refresh"


@pytest.mark.parametrize("scope", ["all", "fixes_missing"])
def test_phase2_waiting_on_ai_admission_cannot_miss_its_reservation(sql, scope):
	# PostgreSQL opens a snapshot when the locking SELECT begins, before the
	# winner commits. Automatic admission must change the parent even though
	# it does not increment the manual refresh counter.
	sql.primary.get_value("Optimus Session", "parent-a", "status", for_update=True)
	thread, waiting = in_thread(
		sql, "phase2-admission", lambda: ai_jobs._retry_sql(lambda: store.lock_phase2_parent("parent-a"))
	)
	wait_until_blocked(sql)
	try:
		assert admit(scope=scope)["status"] == "queued"
	finally:
		sql.primary.commit()
		thread.join(30)
	assert "error" not in waiting and waiting["value"] == "ai_refresh"
	if BACKEND == "postgres":
		assert "40001" in waiting["db"].conflicts
	assert value(sql, "Optimus Session", "parent-a", "ai_refresh_count") == int(scope == "all")


@pytest.mark.parametrize("scope", ["all", "fixes_missing"])
def test_ai_waiting_on_phase2_admission_cannot_start_a_provider_run(sql, scope):
	assert store.lock_phase2_parent("parent-a") is None
	thread, waiting = in_thread(sql, "ai-admission", lambda: ai_jobs._retry_sql(lambda: admit(scope=scope)))
	wait_until_blocked(sql)
	try:
		sql.primary.insert({"doctype": phase2.TABLE, "name": "phase2-a", "parent": "parent-a", "status": "Recording"})
		phase2._write({"name": "phase2-a", "parent": "parent-a"}, status="Recording")
	finally:
		sql.primary.commit()
		thread.join(30)
	assert "error" not in waiting and waiting["value"] == {"status": "refused", "reason": "phase2"}
	assert value(sql, "Optimus Session", "parent-a", "ai_refresh_count") == 0
	assert value(sql, store.RUN, "run-a", "name") is None


def test_duplicate_delivery_from_an_older_snapshot_claims_only_once(sql):
	ai_jobs._retry_sql(admit)
	older = sql.connect()
	older.get_value(store.RUN, "run-a", "state")

	def claim():
		return store.claim(
			"run-a", docname="parent-a", slice_no=0, worker_token="same-worker", now=11, lease_seconds=300
		)

	assert ai_jobs._retry_sql(claim)
	with sql.using(older):
		assert ai_jobs._retry_sql(claim) is None


def test_a_concurrent_duplicate_delivery_is_a_no_op_not_a_snapshot_conflict(sql, monkeypatch):
	"""The second delivery waits on the session lock the first holds. Its first
	statement is that lock, so on MariaDB 11.8 the run row the first changed is no 1020."""
	ai_jobs._retry_sql(admit)
	locked, release = threading.Event(), threading.Event()
	real_locked = store.lock_run

	def pausing(run_id, docname, **kw):
		out = real_locked(run_id, docname, **kw)
		if threading.current_thread().name == "first" and not locked.is_set():
			locked.set()
			assert release.wait(10)
		return out

	monkeypatch.setattr(store, "lock_run", pausing)

	def deliver(token):
		return lambda: ai_jobs._retry_sql(
			lambda: store.claim(
				"run-a", docname="parent-a", slice_no=0, worker_token=token, now=11, lease_seconds=300
			)
		)

	first, one = in_thread(sql, "first", deliver("worker-1"))
	assert locked.wait(10)
	second, two = in_thread(sql, "second", deliver("worker-2"))
	wait_until_blocked(sql)
	release.set()
	first.join(30)
	second.join(30)
	assert one["value"]["worker_token"] == "worker-1"
	assert "error" not in two and two["value"] is None
	assert 1020 not in two["db"].conflicts
	assert value(sql, store.RUN, "run-a", "worker_token") == "worker-1"


def test_a_transition_waiting_on_a_session_writer_is_not_a_snapshot_conflict(sql):
	"""#72's sync Refresh (or a Desk save) holds the session row while the engine begins
	an attempt. The engine's transaction opens with the session lock, so the writer's commit
	is no 1020 on MariaDB 11.8; Postgres retries its serialization failure."""
	claimed()
	writer = sql.connect()
	writer.sql(
		f"UPDATE {writer.table('Optimus Session')} SET {writer.quoted('ai_tokens_spent')} = "
		f"COALESCE({writer.quoted('ai_tokens_spent')}, 0) + 5 WHERE name = 'parent-a'"
	)
	thread, out = in_thread(sql, "engine", lambda: begun())
	wait_until_blocked(sql)
	writer.commit()
	thread.join(30)
	assert "error" not in out and out["value"]["state"] == "calling"
	assert 1020 not in out["db"].conflicts and 1213 not in out["db"].conflicts
	if BACKEND == "mariadb":
		assert not out["db"].conflicts


def test_two_runs_beginning_attempts_together_never_deadlock(sql, monkeypatch):
	"""A locking read of a not-yet-existing attempt key took a gap lock, and two
	runs then deadlocked on each other's INSERT. The INSERT alone takes no gap lock."""
	claimed("run-a", "parent-a", "worker-a")
	claimed("run-b", "parent-b", "worker-b")
	barrier = threading.Barrier(2, timeout=10)
	real_insert = store._insert

	def insert(table, name, **values):
		if table == store.ATTEMPT:
			barrier.wait()  # both runs hold their own locks, right before the INSERT
		return real_insert(table, name, **values)

	monkeypatch.setattr(store, "_insert", insert)
	threads = [
		in_thread(sql, run, lambda run=run, parent=parent, worker=worker: begun(run, parent, worker))
		for run, parent, worker in (("run-a", "parent-a", "worker-a"), ("run-b", "parent-b", "worker-b"))
	]
	for thread, _ in threads:
		thread.join(30)
	for _, out in threads:
		assert "error" not in out and out["value"]["state"] == "calling"
		assert not out["db"].conflicts


def test_a_target_already_attempted_is_refused_by_its_unique_key(sql):
	claimed()
	attempt = begun()
	assert save(sql, attempt) is True
	assert begun() is None  # the INSERT hit the attempt's key: already attempted
	assert ai_jobs._retry_sql(
		lambda: store.checkpoint("run-a", docname="parent-a", worker_token="worker-a", now=14, total_items=1)
	)
	assert value(sql, store.RUN, "run-a", "attempted") == 1
	assert value(sql, store.RUN, "run-a", "total_items") == 1


def history(sql, count=40):
	"""A journal with ``count`` older terminal runs, analyzed, so the optimizer reads the run
	indexes the way it does on a site that has used the engine for a while."""
	db = sql.primary
	for i in range(count):
		db.sql(
			f"INSERT INTO {db.table(store.RUN)} (name, creation, modified, run_id, session_name, "
			"session_uuid, requested_by, scope, state) VALUES (%s, %s, %s, %s, 'parent-c', "
			"'fake-parent-c', 'fake-user', 'all', 'complete')",
			[f"old-{i:02d}", datetime(2026, 1, 1, 0, i), datetime(2026, 1, 1, 0, i), f"old-{i:02d}"],
		)
	db.commit()
	db.sql(("ANALYZE TABLE " if BACKEND == "mariadb" else "ANALYZE ") + db.table(store.RUN))
	db.commit()


@pytest.mark.parametrize("transition", ["finish", "continue_run", "cancel"])
def test_admission_never_deadlocks_another_sessions_worker(sql, monkeypatch, transition):
	"""The admission's FOR UPDATE reads (the reservation probe, the capacity scan)
	locked index ranges that another session's worker needed to finish, continue or be
	cancelled; the worker was the deadlock victim. The admission now reads other runs
	without locks."""
	history(sql)
	claimed("run-b", "parent-b", "worker-b")
	sql.primary.set_value(store.RUN, "run-b", {"completed": 1}, update_modified=False)
	sql.primary.commit()
	locked, release = threading.Event(), threading.Event()
	real_locked = store.lock_run

	def pausing(run_id, docname, **kw):
		out = real_locked(run_id, docname, **kw)
		if threading.current_thread().name == "worker-b" and not locked.is_set():
			locked.set()
			assert release.wait(10)
		return out

	monkeypatch.setattr(store, "lock_run", pausing)
	operations = {
		"finish": lambda: store.finish(
			"run-b", docname="parent-b", worker_token="worker-b", now=12, reason="complete"
		),
		"continue_run": lambda: store.continue_run(
			"run-b", docname="parent-b", worker_token="worker-b", now=12
		),
		"cancel": lambda: store.cancel("run-b", docname="parent-b", now=12),
	}
	worker, done = in_thread(sql, "worker-b", lambda: ai_jobs._retry_sql(operations[transition]))
	assert locked.wait(10)
	admission, admitted = in_thread(sql, "admission-a", lambda: ai_jobs._retry_sql(admit))
	finished_or_blocked(sql, admission)  # the admission ran while the worker holds its rows
	release.set()
	worker.join(30)
	admission.join(30)
	assert "error" not in done and "error" not in admitted
	assert admitted["value"]["status"] == "queued"
	assert (
		done["value"]["state"]
		== {"finish": "complete", "continue_run": "queued", "cancel": "cancelled"}[transition]
	)
	for out in (done, admitted):
		assert 1213 not in out["db"].conflicts and "40P01" not in out["db"].conflicts
	if transition == "finish":
		# One refresh, counted once by #72's real counter when it was admitted (Q1); the
		# finish added nothing.
		assert value(sql, "Optimus Session", "parent-b", "ai_refresh_count") == 1


@pytest.mark.parametrize("transition", ["finish", "cancel"])
def test_an_admission_holds_no_lock_another_sessions_worker_needs(sql, monkeypatch, transition):
	"""The admission is paused after all its reads, holding every lock it took; another
	session's worker must still end its run without waiting. A FOR UPDATE probe of the
	reservation or a locking capacity scan held a gap or next-key lock the finishing
	worker's index update had to wait for."""
	history(sql)
	claimed("run-b", "parent-b", "worker-b")
	sql.primary.set_value(store.RUN, "run-b", {"completed": 1}, update_modified=False)
	sql.primary.commit()
	paused, release = threading.Event(), threading.Event()
	real_insert_new = store._insert_new

	def pausing(table, name, **values):
		if table == store.RUN and threading.current_thread().name == "admission-a":
			paused.set()
			assert release.wait(30)
		return real_insert_new(table, name, **values)

	monkeypatch.setattr(store, "_insert_new", pausing)
	admission, admitted = in_thread(sql, "admission-a", lambda: ai_jobs._retry_sql(admit))
	assert paused.wait(10)
	operations = {
		"finish": lambda: store.finish(
			"run-b", docname="parent-b", worker_token="worker-b", now=12, reason="complete"
		),
		"cancel": lambda: store.cancel("run-b", docname="parent-b", now=12),
	}
	worker, done = in_thread(sql, "worker-b", lambda: ai_jobs._retry_sql(operations[transition]))
	blocked = not ends_without_waiting(sql, worker)
	release.set()
	worker.join(60)
	admission.join(60)
	assert not blocked, "another session's worker waited for a lock the admission held"
	assert "error" not in done and done["value"]["active_session"] is None
	assert "error" not in admitted and admitted["value"]["status"] == "queued"


def test_an_admission_waiting_on_its_session_never_holds_the_site_mutex(sql):
	"""The session row is locked before the site mutex, so another session's admission
	is never refused (or failed) while this one waits."""
	writer = sql.connect()
	writer.get_value("Optimus Session", "parent-a", "status", for_update=True)
	thread, waiting = in_thread(sql, "admission-a", lambda: ai_jobs._retry_sql(admit))
	wait_until_blocked(sql)
	assert ai_jobs._retry_sql(lambda: admit("run-b", "parent-b"))["status"] == "queued"
	writer.commit()
	thread.join(30)
	assert "error" not in waiting and waiting["value"]["status"] == "queued"


def test_a_held_site_mutex_is_a_busy_refusal_without_retrying(sql):
	"""Q4: NOWAIT contention answers busy at once, instead of raising or retrying, and an
	analyze-time admission never takes the mutex at all."""
	sql.primary.get_value(store.CONTROL, "site", "name", for_update=True)
	contender = sql.connect()
	with sql.using(contender):
		assert ai_jobs._retry_sql(admit) == {"status": "refused", "reason": "busy"}
		assert contender.conflicts == ([1205] if BACKEND == "mariadb" else ["55P03"])
		assert (
			ai_jobs._retry_sql(lambda: admit("run-b", "parent-b", scope="fixes_missing"))["status"]
			== "queued"
		)
	sql.primary.commit()
	with sql.using(contender):
		assert ai_jobs._retry_sql(admit)["status"] == "queued"


def test_a_second_active_run_of_a_session_is_refused_by_the_unique_reservation(sql, monkeypatch):
	ai_jobs._retry_sql(lambda: admit("run-a", "parent-a", scope="fixes_missing"))
	real_get_value = SQLDatabase.get_value

	def blind(self, doctype, filters, *a, **kw):
		# The snapshot of a waiting Postgres admission cannot see the other run.
		if doctype == store.RUN and isinstance(filters, dict) and "active_session" in filters:
			return None
		return real_get_value(self, doctype, filters, *a, **kw)

	monkeypatch.setattr(SQLDatabase, "get_value", blind)
	out = ai_jobs._retry_sql(lambda: admit("run-b", "parent-a", scope="fixes_missing"))
	assert out == {"status": "already_running", "run": None}
	monkeypatch.setattr(SQLDatabase, "get_value", real_get_value)
	assert value(sql, store.RUN, "run-b", "name") is None
	assert value(sql, store.RUN, "run-a", "active_session") == "parent-a"


def test_answer_outcome_and_usage_commit_once_with_the_real_counter(sql):
	claimed()
	attempt = begun()

	def persist():
		sql.facade.db.sql(
			f"UPDATE {sql.facade.db.table('answer')} SET body = 'fake answer' WHERE name = 'row-00'"
		)

	for _ in range(2):
		assert save(sql, attempt, persist=persist) is True
	assert value(sql, "answer", "row-00", "body") == "fake answer"
	assert value(sql, store.ATTEMPT, attempt["name"], "settled") == 1
	assert value(sql, store.RUN, "run-a", "tokens_reported") == 7
	assert value(sql, store.RUN, "run-a", "completed") == 1
	assert value(sql, "Optimus Session", "parent-a", "ai_tokens_spent") == 7
	assert not sql.logged


def test_totals_above_the_int_range_add_up_in_the_session_counter(sql):
	"""The largest count ``ai_fix._token_count`` accepts, twice: the session's Long Int total
	passes 2**31 - 1, and a repeated settlement charges nothing more."""
	claimed()
	largest = ai_fix.MAX_TOKEN_COUNT
	for target in ("finding-a", "finding-b"):
		attempt = begun(target=target)
		for _ in range(2):
			save(sql, attempt, tokens=largest)
	assert value(sql, "Optimus Session", "parent-a", "ai_tokens_spent") == 2 * largest
	assert value(sql, store.RUN, "run-a", "tokens_reported") == 2 * largest
	assert value(sql, store.RUN, "run-a", "completed") == 2


def test_a_counter_statement_failure_keeps_the_answer_and_logs_the_dropped_spend(sql):
	"""#72's log-and-continue trade-off on a real database: the counter's statement fails (BIGINT overflow
	here), only the counter is rolled back to its savepoint, the failure is logged once and
	the answer and the attempt's outcome commit. ``tokens_reported`` keeps the tokens, so the
	journal stays the source for reconciling the session's ``ai_tokens_spent``."""
	claimed()
	top = 2**63 - 3
	sql.primary.set_value("Optimus Session", "parent-a", {"ai_tokens_spent": top}, update_modified=False)
	sql.primary.commit()
	attempt = begun()

	def persist():
		sql.facade.db.sql(f"UPDATE {sql.facade.db.table('answer')} SET body = 'kept' WHERE name = 'row-00'")

	assert save(sql, attempt, persist=persist) is True
	assert value(sql, "answer", "row-00", "body") == "kept"
	assert value(sql, store.ATTEMPT, attempt["name"], "settled") == 1
	assert value(sql, store.RUN, "run-a", "tokens_reported") == 7
	assert value(sql, "Optimus Session", "parent-a", "ai_tokens_spent") == top
	[(title, _exc, context)] = sql.logged
	assert title == "optimus ai spend" and context["amount"] == 7
	assert context["savepoint_rollback"] == "done"


def _deadlock_timeout(db, setting):
	"""``SET deadlock_timeout`` on one Postgres connection. Only a superuser may set it; CI's
	``POSTGRES_USER`` is one, so a refusal is a misconfigured service: fail, never skip."""
	try:
		db.sql(f"SET deadlock_timeout = '{setting}'")
	except Exception as exc:
		db.rollback()
		pytest.fail(
			f"Postgres refused SET deadlock_timeout ({type(exc).__name__}): the journal test role "
			"must be a superuser, as CI's POSTGRES_USER is",
			pytrace=False,
		)
	db.commit()


def _until_waiting(sql, backend_id):
	"""Until that one connection waits for a row lock. Any other waiter in the database (a
	transient one included) must not count: the order of the waits decides the victim."""
	query = (
		"SELECT COUNT(*) AS n FROM information_schema.PROCESSLIST WHERE ID = %s AND COMMAND = 'Query' "
		"AND INFO LIKE '%%FOR UPDATE%%'"
		if BACKEND == "mariadb"
		else "SELECT COUNT(*) AS n FROM pg_stat_activity WHERE pid = %s AND wait_event_type = 'Lock'"
	)
	if not hasattr(sql, "probe"):
		sql.probe = sql.connect()
	for _ in range(1000):
		waiting = sql.probe.sql(query, [backend_id])[0]["n"]
		sql.probe.commit()
		if waiting:
			return
		time.sleep(0.01)
	raise AssertionError("the connection never waited for the lock")


def test_a_deadlock_during_settlement_is_retried_and_charged_once(sql):
	"""A whole-transaction rollback in the middle of the settlement: a heavier transaction holds
	the answer row and waits for the session row the settlement holds. Whichever side the
	database aborts, the session is charged once and the answer stored once; no provider is
	involved.

	The settlement is made the victim on both databases, so the test also proves that
	``_settle`` rolls back and writes again. The rival waits for the session row first, and the
	settlement's request for the answer row then closes the cycle. MariaDB rolls back the
	lighter transaction: the rival changed 39 rows, the settlement none yet. Postgres aborts the
	backend whose ``deadlock_timeout`` runs out first while the cycle exists: the settlement's
	1 s starts when its request closes the cycle, the rival's 10 s started earlier. With equal
	timers the first waiter checks first, which now and then made the rival the victim."""
	claimed()
	attempt = begun()
	rival, settling = sql.connect(), sql.connect()
	if BACKEND == "postgres":
		_deadlock_timeout(rival, "10s")
		_deadlock_timeout(settling, "1s")
	rival_id = rival.sql(
		"SELECT CONNECTION_ID() AS id" if BACKEND == "mariadb" else "SELECT pg_backend_pid() AS id"
	)[0]["id"]
	answer = rival.table("answer")
	rival.sql(f"UPDATE {answer} SET body = 'rival' WHERE name <> 'row-00'")  # the heavier side
	rival.sql(f"SELECT name FROM {answer} WHERE name = 'row-00' FOR UPDATE")
	holding, persisted, rival_errors = threading.Event(), [], []

	def persist():
		persisted.append(True)
		if len(persisted) == 1:
			holding.set()  # the settlement holds the session row...
			_until_waiting(sql, rival_id)  # ...and the rival waits for it before the cycle closes
		sql.facade.db.sql(f"UPDATE {sql.facade.db.table('answer')} SET body = 'engine' WHERE name = 'row-00'")

	def run_rival():
		if holding.wait(10):
			try:
				rival.get_value("Optimus Session", "parent-a", "name", for_update=True)
			except Exception as exc:
				rival_errors.append(exc)
		rival.commit()

	thread = threading.Thread(target=run_rival)
	thread.start()
	with sql.using(settling) as db:
		assert save(sql, attempt, persist=persist) is True
	thread.join(30)
	# Whichever side was aborted: one charge, one stored answer, one settled outcome.
	assert value(sql, "answer", "row-00", "body") == "engine"
	assert value(sql, store.ATTEMPT, attempt["name"], "settled") == 1
	assert value(sql, store.RUN, "run-a", "completed") == 1
	assert value(sql, "Optimus Session", "parent-a", "ai_tokens_spent") == 7
	# The settlement was the victim (see the docstring), so it was rolled back and retried.
	assert not rival_errors, rival_errors
	assert db.conflicts == [1213 if BACKEND == "mariadb" else "40P01"]
	assert persisted == [True, True]


def test_refresh_state_answers_the_progress_of_the_sessions_newest_run(sql, monkeypatch):
	monkeypatch.setattr(ai_jobs, "_now", lambda: 30.0)  # run-b's lease and deadline are ahead
	assert ai_jobs.refresh_state("parent-a") is None
	claimed()
	ai_jobs._retry_sql(lambda: store.cancel("run-a", docname="parent-a", now=12))
	assert ai_jobs._retry_sql(lambda: admit("run-b", now=20))["status"] == "queued"
	state = ai_jobs.refresh_state("parent-a")
	assert state["run_id"] == "run-b" and state["state"] == "queued" and "worker_token" not in state
	assert ai_jobs.refresh_state("parent-b") is None


def test_completion_and_nullable_reservation_allow_a_later_run(sql):
	claimed()
	ai_jobs._retry_sql(lambda: store.cancel("run-a", docname="parent-a", now=12))
	assert ai_jobs._retry_sql(lambda: admit("run-b"))["status"] == "queued"
	assert value(sql, store.RUN, "run-a", "active_session") is None
	assert value(sql, store.RUN, "run-b", "active_session") == "parent-a"


def _bounded_contender(sql, monkeypatch, seconds=1):
	"""A connection for one HTTP request whose row lock waits end after ``seconds``. On MariaDB
	that is the server's ``innodb_lock_wait_timeout``, set on this connection to stand in for
	the server's value. On Postgres the connection is left as Frappe leaves it (``lock_timeout``
	0: no limit) and the bound is the engine's own (``HTTP_LOCK_WAIT_SECONDS``)."""
	contender = sql.connect()
	if BACKEND == "mariadb":
		contender.sql(f"SET SESSION innodb_lock_wait_timeout={seconds}")
	else:
		monkeypatch.setattr(ai_jobs, "HTTP_LOCK_WAIT_SECONDS", seconds)
	contender.commit()
	return contender


def _held_session(sql, release_after=20):
	"""A connection holding the session row's lock (``_hold``)."""
	return _hold(sql.connect(), release_after=release_after)


def _hold(writer, docname="parent-a", release_after=20):
	"""``writer`` takes the session row's lock. A request that waits for it with no lock bound
	(Postgres has none unless one is set) would wait for ever, so the holder lets go after
	``release_after`` seconds: such a request then fails its test instead of hanging the suite.
	``_release`` ends the hold."""
	writer.get_value("Optimus Session", docname, "status", for_update=True)
	writer.release = threading.Timer(release_after, writer.commit)
	writer.release.start()
	return writer


def _release(writer):
	writer.release.cancel()
	writer.commit()


def test_an_http_admission_waits_for_a_held_session_row_once_then_answers_busy(sql, monkeypatch):
	"""A user refresh blocked on its session row waits one lock wait timeout, then answers busy
	without a second attempt, so a web request never outlives the server's timeout on retries.
	The wait is shortened to one second."""
	monkeypatch.setattr(ai_jobs, "_check_run", lambda run, **kw: None)
	monkeypatch.setattr(ai_jobs, "_workers_listening", lambda docname=None: True)
	monkeypatch.setattr(ai_jobs, "_dispatch_pending", lambda run_id, docname, **kw: True)
	monkeypatch.setattr(ai_fix, "is_available", lambda **kw: True)
	writer = _held_session(sql)
	contender = _bounded_contender(sql, monkeypatch)
	with sql.using(contender):
		out = ai_jobs.start_refresh(
			docname="parent-a", session_uuid="fake-parent-a", requested_by="fake-user"
		)
	_release(writer)
	assert out == {"status": "refused", "reason": "busy"}
	assert contender.conflicts == ([1205] if BACKEND == "mariadb" else ["55P03"])
	assert value(sql, store.RUN, "run-a", "name") is None


def test_a_settlement_waiting_on_a_session_writer_keeps_the_answer(sql):
	"""The settlement waits on a session row that a writer (#72's counter, a Desk save)
	then commits. Postgres raises a raw serialization failure (40001) on the session lock;
	settlement retries it (a superset of #72's rule) and keeps the paid answer. MariaDB
	needs no retry: the lock is the transaction's first statement."""
	claimed()
	attempt = begun()
	writer = sql.connect()
	writer.sql(
		f"UPDATE {writer.table('Optimus Session')} SET {writer.quoted('ai_tokens_spent')} = "
		f"COALESCE({writer.quoted('ai_tokens_spent')}, 0) + 5 WHERE name = 'parent-a'"
	)

	def persist():
		sql.facade.db.sql(f"UPDATE {sql.facade.db.table('answer')} SET body = 'engine' WHERE name = 'row-00'")

	thread, out = in_thread(sql, "settle", lambda: save(sql, attempt, persist=persist))
	wait_until_blocked(sql)
	writer.commit()
	thread.join(30)
	assert "error" not in out and out["value"] is True
	assert out["db"].conflicts == (["40001"] if BACKEND == "postgres" else [])
	assert value(sql, "answer", "row-00", "body") == "engine"
	assert value(sql, store.RUN, "run-a", "completed") == 1
	assert value(sql, "Optimus Session", "parent-a", "ai_tokens_spent") == 12
	assert not sql.logged


def test_an_admission_takes_over_an_expired_run_in_its_own_transaction(sql, monkeypatch):
	"""A worker killed mid-call left its run running; the next click (here at t=400,
	past the 311 lease) ends it interrupted with its call uncertain and admits the new run,
	with no scheduler. #72's real counter counts the new click once and the interrupted run not at all (Q1).
	The call made uncertain and the interrupted run are one Error Log row each, written after
	the takeover committed."""
	claimed()
	attempt = begun()
	sql.primary.set_value(store.RUN, "run-a", {"completed": 2}, update_modified=False)
	sql.primary.commit()
	_http_ready(monkeypatch)
	monkeypatch.setattr(ai_jobs, "_now", lambda: 400.0)
	committed, probe = [], sql.connect()
	real_log = ai_fix.log_ai_failure

	def log(title, exc=None, **context):
		# what another connection sees when the row is written: the takeover is committed
		committed.append(probe.get_value(store.ATTEMPT, attempt["name"], "state"))
		probe.commit()
		return real_log(title, exc, **context)

	monkeypatch.setattr(ai_fix, "log_ai_failure", log)
	out = _click()
	assert out["status"] == "queued"
	run_b = out["refresh"]["run_id"]
	assert value(sql, store.RUN, "run-a", "state") == "interrupted"
	assert value(sql, store.RUN, "run-a", "end_reason") == "lease_expired"
	assert value(sql, store.RUN, "run-a", "active_session") is None
	assert value(sql, store.ATTEMPT, attempt["name"], "state") == "uncertain"
	assert value(sql, store.RUN, run_b, "active_session") == "parent-a"
	assert (
		value(sql, "Optimus Session", "parent-a", "ai_refresh_count") == 2
	)  # each click once; the takeover adds none
	assert [(title, context) for title, _exc, context in sql.logged] == [
		(
			"optimus ai refresh uncertain",
			{"docname": "parent-a", "run_id": "run-a", "attempt": attempt["name"], "reason": "lease_expired"},
		),
		(
			"optimus ai refresh ended",
			{"docname": "parent-a", "run_id": "run-a", "state": "interrupted", "reason": "lease_expired"},
		),
	]
	assert committed == ["uncertain", "uncertain"]


def test_a_late_settlement_and_a_takeover_serialize_on_the_session_row(sql, monkeypatch):
	"""The dead worker was only slow: its late, known outcome holds the session row while a
	click takes the run over. The click waits, then sees the settled attempt: the tokens
	count once, the stale answer is not written, the run ends interrupted and the new run is
	admitted. No 1020 on MariaDB 11.8 (the session lock comes first); Postgres retries the
	serialization failure the settlement's counter update causes."""
	claimed()
	attempt = begun()
	locked, release = threading.Event(), threading.Event()
	real_locked = store.lock_run

	def pausing(run_id, docname, **kw):
		out = real_locked(run_id, docname, **kw)
		if threading.current_thread().name == "late-worker" and not locked.is_set():
			locked.set()
			assert release.wait(10)
		return out

	monkeypatch.setattr(store, "lock_run", pausing)

	def persist():
		sql.facade.db.sql(f"UPDATE {sql.facade.db.table('answer')} SET body = 'stale' WHERE name = 'row-00'")

	def late():
		return store.settle_attempt(
			"run-a",
			attempt["name"],
			docname="parent-a",
			worker_token="worker-a",
			outcome="succeeded",
			tokens=7,
			usage_complete=True,
			now=400,
			persist=persist,
		)

	worker, settled = in_thread(sql, "late-worker", lambda: ai_jobs._retry_sql(late))
	assert locked.wait(10)
	click, admitted = in_thread(
		sql, "click", lambda: ai_jobs._retry_sql(lambda: admit("run-b", now=401), retry_lock_waits=False)
	)
	wait_until_blocked(sql)
	release.set()
	worker.join(30)
	click.join(30)
	assert "error" not in settled and settled["value"] is True
	assert "error" not in admitted and admitted["value"]["status"] == "queued"
	assert admitted["db"].conflicts == (["40001"] if BACKEND == "postgres" else [])
	assert value(sql, store.ATTEMPT, attempt["name"], "state") == "discarded"
	assert value(sql, store.RUN, "run-a", "state") == "interrupted"
	assert value(sql, store.RUN, "run-a", "uncertain") == 0
	assert value(sql, "Optimus Session", "parent-a", "ai_tokens_spent") == 7
	assert value(sql, "answer", "row-00", "body") == ""
	assert value(sql, store.RUN, "run-b", "active_session") == "parent-a"


def test_the_sweep_skips_a_session_a_dead_transaction_holds(sql, monkeypatch):
	"""A dead worker host's transaction can hold a session row until wait_timeout; each
	sweep row waited a whole lock timeout and then aborted the sweep. The sweep's NOWAIT
	lock is refused at once: that run waits for a later sweep, the others are recovered."""
	claimed("run-a", "parent-a", "worker-a")
	claimed("run-b", "parent-b", "worker-b")
	monkeypatch.setattr(ai_jobs, "_now", lambda: 1000.0)
	holder = sql.connect()
	holder.get_value("Optimus Session", "parent-a", "status", for_update=True)
	sweep, out = in_thread(sql, "sweep", ai_jobs.recover_pending)
	waited = not ends_without_waiting(sql, sweep)
	holder.commit()
	sweep.join(30)
	assert not waited, "the sweep waited for a lock a dead transaction held"
	assert "error" not in out
	assert out["value"] == {"interrupted": 1, "dispatched": 0, "busy": 1, "failed": 0}
	assert out["db"].conflicts == ([1205] if BACKEND == "mariadb" else ["55P03"])
	assert value(sql, store.RUN, "run-b", "state") == "interrupted"
	assert value(sql, store.RUN, "run-a", "state") == "running"
	assert [(title, context["run_id"]) for title, _exc, context in sql.logged] == [
		("optimus ai refresh ended", "run-b")
	]
	assert ai_jobs.recover_pending()["interrupted"] == 1
	assert value(sql, store.RUN, "run-a", "state") == "interrupted"
	assert [(title, context["run_id"]) for title, _exc, context in sql.logged][1:] == [
		("optimus ai refresh ended", "run-a")
	]


def test_an_http_cancel_waits_for_a_held_session_row_once_then_answers_busy(sql, monkeypatch):
	"""The cancel endpoint's wait is bounded by one lock wait timeout, like admission."""
	claimed()
	writer = _held_session(sql)
	contender = _bounded_contender(sql, monkeypatch)
	with sql.using(contender):
		out = ai_jobs.cancel_refresh("run-a", docname="parent-a", requested_by="fake-user")
	_release(writer)
	assert out == {"status": "refused", "reason": "busy"}
	assert contender.conflicts == ([1205] if BACKEND == "mariadb" else ["55P03"])
	assert value(sql, store.RUN, "run-a", "state") == "running"


def _recording(db):
	"""Every statement ``db`` runs, with its commits as "COMMIT", in order."""
	statements = []
	run, commit = db.sql, db.commit

	def recorded(query, values=(), **kw):
		statements.append(query)
		return run(query, values, **kw)

	def committed():
		statements.append("COMMIT")
		commit()

	db.sql, db.commit = recorded, committed
	return statements


@pytest.mark.parametrize("path", ["click", "cancel"])
def test_an_http_request_on_a_held_session_row_waits_once_and_leaves_nothing_waiting(sql, monkeypatch, path):
	"""Postgres waits for a row lock for as long as it takes unless ``lock_timeout`` is set, and
	Frappe sets none: a click or a cancel on a session row another transaction held waited until
	that transaction ended, or until the web server killed the request while its backend went on
	waiting. The admission and the cancellation now bound their own wait on Postgres, set first
	in their transaction (``SET LOCAL``): the request answers busy, no backend is left waiting
	for the row, and the connection keeps its own setting. MariaDB keeps the server's timeout
	and gets no SET."""
	_http_ready(monkeypatch)
	if path == "cancel":
		claimed()
	writer = sql.connect()
	writer.get_value("Optimus Session", "parent-a", "status", for_update=True)
	contender = _bounded_contender(sql, monkeypatch)
	statements = _recording(contender)
	out = {}

	def request():
		sql.facade.db = contender
		try:
			if path == "click":
				out["value"] = _click()
			else:
				out["value"] = ai_jobs.cancel_refresh("run-a", docname="parent-a", requested_by="fake-user")
		except Exception as exc:
			out["error"] = exc

	thread = threading.Thread(target=request, name=path)
	thread.start()
	thread.join(15)
	waiting = thread.is_alive()
	waiters = _lock_waiters(sql)  # the writer still holds the row
	writer.commit()
	thread.join(30)
	assert not waiting, "the request still waited for the session row after 15 s"
	assert out == {"value": {"status": "refused", "reason": "busy"}}
	assert contender.conflicts == ([1205] if BACKEND == "mariadb" else ["55P03"])
	assert waiters == 0
	bound = [i for i, query in enumerate(statements) if query.upper().startswith(("SET", "SHOW"))]
	if BACKEND == "mariadb":
		assert bound == []
	else:
		i = bound[0]
		assert statements[i - 1 : i + 2] == ["COMMIT", "SHOW lock_timeout", "SET LOCAL lock_timeout = %s"]
		assert "FOR UPDATE" in statements[i + 2]
		assert contender.table("Optimus Session") in statements[i + 2]
		assert bound == [i, i + 1]
		assert contender.sql("SHOW lock_timeout")[0]["lock_timeout"] == "0"
	contender.commit()


def test_the_lock_bound_opens_no_read_view_before_the_session_lock(sql, monkeypatch):
	"""``SHOW`` and ``SET`` take no snapshot on Postgres, so the session lock stays the
	admission's first read: a change committed to the session row right after the bound's first
	statement is no serialization failure (40001). MariaDB gets neither."""
	_http_ready(monkeypatch)
	contender, writer = sql.connect(), sql.connect()
	run = contender.sql
	bound = []

	def change_after_the_first(query, values=(), **kw):
		out = run(query, values, **kw)
		if query.upper().startswith(("SET", "SHOW")):
			bound.append(query)
			if len(bound) == 1:
				writer.set_value("Optimus Session", "parent-a", {"title": "changed after the SHOW"})
				writer.commit()
		return out

	contender.sql = change_after_the_first
	with sql.using(contender):
		out = _click()
	assert out["status"] == "queued" and contender.conflicts == []
	assert bound == ([] if BACKEND == "mariadb" else ["SHOW lock_timeout", "SET LOCAL lock_timeout = %s"])
	assert value(sql, "Optimus Session", "parent-a", "title") == (
		None if BACKEND == "mariadb" else "changed after the SHOW"
	)


def test_a_stricter_lock_wait_setting_is_kept(sql, monkeypatch):
	"""A ``lock_timeout`` of the role, the database or the server that is stricter than the
	engine's bound is kept: the request reads it and sets nothing, so it waits that long and not
	the bound (patched to 60 s here). MariaDB's wait is always the server's."""
	_http_ready(monkeypatch)
	writer = _held_session(sql)
	contender = sql.connect()
	if BACKEND == "mariadb":
		contender.sql("SET SESSION innodb_lock_wait_timeout=1")
	else:
		contender.sql("SET lock_timeout = '1s'")  # SHOW reads a role's setting the same way
		monkeypatch.setattr(ai_jobs, "HTTP_LOCK_WAIT_SECONDS", 60)
	contender.commit()
	statements = _recording(contender)
	out = {}

	def request():
		sql.facade.db = contender
		try:
			out["value"] = _click()
		except Exception as exc:
			out["error"] = exc

	thread = threading.Thread(target=request, name="click")
	thread.start()
	thread.join(15)
	waiting = thread.is_alive()
	_release(writer)
	thread.join(70)
	assert not waiting, "the request waited longer than the stricter one-second setting"
	assert out == {"value": {"status": "refused", "reason": "busy"}}
	assert contender.conflicts == ([1205] if BACKEND == "mariadb" else ["55P03"])
	assert [q for q in statements if q.upper().startswith("SET")] == []
	assert statements.count("SHOW lock_timeout") == (0 if BACKEND == "mariadb" else 1)


def _http_ready(monkeypatch):
	monkeypatch.setattr(ai_jobs, "_check_run", lambda run, **kw: None)
	monkeypatch.setattr(ai_jobs, "_workers_listening", lambda docname=None: True)
	monkeypatch.setattr(ai_jobs, "_call_timeout", lambda: 60)
	monkeypatch.setattr(ai_jobs, "_selection_counts", lambda *a, **kw: (0, 0))
	monkeypatch.setattr(ai_fix, "is_available", lambda **kw: True)


def _click():
	return ai_jobs.start_refresh(docname="parent-a", session_uuid="fake-parent-a", requested_by="fake-user")


def test_an_http_dispatch_never_waits_and_leaves_the_intent_pending(sql, monkeypatch):
	"""After the admission commits, its dispatch finds the session row held (here by
	a Desk save). The admission already waited, so the dispatch takes the row with NOWAIT,
	answers queued and leaves the committed intent for the session's next click."""
	_http_ready(monkeypatch)
	writer = sql.connect()
	dispatch = ai_jobs._dispatch_pending

	def after_commit(run_id, docname, **kw):
		writer.get_value("Optimus Session", docname, "status", for_update=True)
		return dispatch(run_id, docname, **kw)

	monkeypatch.setattr(ai_jobs, "_dispatch_pending", after_commit)
	thread, out = in_thread(sql, "click", _click)
	waited = not ends_without_waiting(sql, thread)
	writer.commit()
	thread.join(30)
	assert not waited, "the dispatch waited for the session row after the admission"
	assert "error" not in out and out["value"]["status"] == "queued"
	assert out["db"].conflicts == ([1205] if BACKEND == "mariadb" else ["55P03"])
	assert not sql.facade.enqueued and not sql.logged
	assert value(sql, store.RUN, out["value"]["refresh"]["run_id"], "dispatch_pending") == 1


def test_a_click_resends_a_committed_intent_its_dispatch_left_pending(sql, monkeypatch):
	"""The first click's dispatch found the session row busy; a later click
	re-sends the pending intent once the 30 s throttle allows, instead of only attaching for
	the whole 1800 s queued lease (the sweep is not scheduled)."""
	_http_ready(monkeypatch)
	clock = [100.0]
	monkeypatch.setattr(ai_jobs, "_now", lambda: clock[0])
	writer = sql.connect()
	dispatch = ai_jobs._dispatch_pending
	held = [True]

	def after_commit(run_id, docname, **kw):
		if held[0]:
			_hold(writer, docname)
		return dispatch(run_id, docname, **kw)

	monkeypatch.setattr(ai_jobs, "_dispatch_pending", after_commit)
	with sql.using(_bounded_contender(sql, monkeypatch)):
		first = _click()
	_release(writer)
	held[0] = False
	assert first["status"] == "queued" and not sql.facade.enqueued
	run_id = first["refresh"]["run_id"]
	clock[0] = 160.0
	again = _click()
	assert again["status"] == "already_running" and again["refresh"]["run_id"] == run_id
	assert [kw["job_id"] for _, kw in sql.facade.enqueued] == [f"optimus-ai-{run_id}-0"]
	clock[0] = 170.0  # inside the 30 s throttle: attaching only
	assert _click()["status"] == "already_running" and len(sql.facade.enqueued) == 1
	assert value(sql, store.RUN, run_id, "dispatch_pending") == 1
	assert not sql.logged


def test_one_refresh_click_waits_for_one_lock_timeout_at_most(sql, monkeypatch):
	"""The admission gets its session row just before its lock timeout; the
	dispatch then finds the row held again. It refuses at once instead of waiting a second
	timeout, so the click stays well inside the web server's timeout."""
	_http_ready(monkeypatch)
	first, second = sql.connect(), sql.connect()
	first.get_value("Optimus Session", "parent-a", "status", for_update=True)
	threading.Timer(1.7, first.commit).start()
	dispatch = ai_jobs._dispatch_pending

	def after_commit(run_id, docname, **kw):
		_hold(second, docname)
		return dispatch(run_id, docname, **kw)

	monkeypatch.setattr(ai_jobs, "_dispatch_pending", after_commit)
	contender = _bounded_contender(sql, monkeypatch, seconds=2)
	with sql.using(contender):
		out = _click()
	_release(second)
	assert out["status"] == "queued"
	# one wait: the admission's lock was granted; the dispatch's NOWAIT is the only conflict
	assert contender.conflicts == ([1205] if BACKEND == "mariadb" else ["55P03"])


def test_a_forced_release_in_a_refused_admission_writes_its_row(sql, monkeypatch):
	"""The takeover of an inconsistent expired run commits with a site_cap
	refusal; its one recovery row is written too."""
	_http_ready(monkeypatch)
	claimed("run-a", "parent-a", "worker-a")
	attempt = begun()
	sql.primary.sql(f"DELETE FROM {sql.primary.table(store.ATTEMPT)} WHERE name = %s", [attempt["name"]])
	sql.primary.commit()
	assert ai_jobs._retry_sql(lambda: admit("run-b", "parent-b", now=900))["status"] == "queued"
	sql.facade.conf["optimus_ai_max_active_refreshes"] = 1
	monkeypatch.setattr(ai_jobs, "_now", lambda: 1000.0)
	assert _click() == {"status": "refused", "reason": "site_cap"}
	assert value(sql, store.RUN, "run-a", "state") == "interrupted"
	assert value(sql, store.RUN, "run-a", "usage_incomplete") == 1
	[(title, _exc, context)] = [row for row in sql.logged if row[0] == "optimus ai refresh release"]
	assert context["run_id"] == "run-a" and context["docname"] == "parent-a"


def test_a_refresh_is_counted_once_when_it_is_accepted_by_the_real_counter(sql):
	"""Q1: #72's real counter (a savepoint-wrapped SQL increment) adds one in the admission's
	own transaction, a resume adds one more, and no ending of a run adds any."""
	claimed()
	assert value(sql, "Optimus Session", "parent-a", "ai_refresh_count") == 1
	ai_jobs._retry_sql(
		lambda: store.finish("run-a", docname="parent-a", worker_token="worker-a", now=14, reason="complete")
	)
	assert value(sql, "Optimus Session", "parent-a", "ai_refresh_count") == 1
	out = ai_jobs._retry_sql(lambda: admit("run-b", now=20))
	assert out["status"] == "queued"
	assert value(sql, "Optimus Session", "parent-a", "ai_refresh_count") == 2
	ai_jobs._retry_sql(lambda: store.cancel("run-b", docname="parent-a", now=21))
	out = ai_jobs._retry_sql(lambda: admit("run-c", now=22, resume_from="run-b"))
	assert out["status"] == "queued" and out["run"]["retry_no"] == 1
	assert value(sql, "Optimus Session", "parent-a", "ai_refresh_count") == 3
	ai_jobs._retry_sql(lambda: store.cancel("run-c", docname="parent-a", now=23))
	assert value(sql, "Optimus Session", "parent-a", "ai_refresh_count") == 3
	assert not sql.logged


def test_a_refused_admission_counts_nothing_by_the_real_counter(sql):
	claimed()
	refused = ai_jobs._retry_sql(lambda: admit("run-b", "parent-b", cap=1))
	assert refused == {"status": "refused", "reason": "site_cap"}
	assert value(sql, "Optimus Session", "parent-b", "ai_refresh_count") == 0
	attached = ai_jobs._retry_sql(lambda: admit("run-c", now=12))
	assert attached["status"] == "already_running"
	assert value(sql, "Optimus Session", "parent-a", "ai_refresh_count") == 1


def test_an_analyze_time_run_counts_no_refresh_by_the_real_counter(sql):
	assert ai_jobs._retry_sql(lambda: admit("run-a", scope="fixes_missing"))["status"] == "queued"
	assert value(sql, "Optimus Session", "parent-a", "ai_refresh_count") == 0


def test_the_selections_counts_are_stored_on_the_run(sql):
	"""Item 5: ``gated_count`` and ``excluded_count`` are Int columns of the Run DocType."""
	out = ai_jobs._retry_sql(lambda: admit("run-a", gated=3, excluded=2))
	assert out["run"]["gated_count"] == 3
	assert value(sql, store.RUN, "run-a", "gated_count") == 3
	assert value(sql, store.RUN, "run-a", "excluded_count") == 2
	assert ai_jobs.public_state(sql.primary.get_value(store.RUN, "run-a", "*", as_dict=True))["gated"] == 3


def test_a_late_delivery_cannot_revive_a_run_the_refresh_treats_as_dead(sql, monkeypatch):
	"""A queued run whose 1800 s lease lapsed (deadline not passed) is dead to ``live_run``,
	so #72's Refresh may run; the queue's late delivery must not claim it and send a call beside
	the Refresh. The next admission takes it over."""
	assert ai_jobs._retry_sql(admit)["status"] == "queued"  # now=10: lease 1810, deadline 3610
	monkeypatch.setattr(ai_jobs, "_now", lambda: 1900)
	assert ai_jobs.live_run("parent-a") is None
	late = ai_jobs._retry_sql(
		lambda: store.claim(
			"run-a", docname="parent-a", slice_no=0, worker_token="late", now=1900, lease_seconds=300
		)
	)
	assert late is None
	assert value(sql, store.RUN, "run-a", "state") == "queued"
	out = ai_jobs._retry_sql(lambda: admit("run-b", now=1900), retry_lock_waits=False)
	assert out["status"] == "queued" and out["taken_over"]["name"] == "run-a"
	assert value(sql, store.RUN, "run-a", "state") == "interrupted"


def _crashed_workers(user_b="fake-user"):
	"""User refreshes on parent-a and parent-b, claimed at 11 (leases until 311) by workers
	that were then killed. Nothing ends them: no click on their sessions, no sweep."""
	assert claimed("run-a", "parent-a", "worker-a")
	assert ai_jobs._retry_sql(lambda: admit("run-b", "parent-b", user=user_b))["status"] == "queued"
	assert ai_jobs._retry_sql(
		lambda: store.claim(
			"run-b", docname="parent-b", slice_no=0, worker_token="worker-b", now=11, lease_seconds=300
		)
	)


@pytest.mark.parametrize(
	"dead,now", [("worker-died", 1000), ("lease-ends-now", 311), ("deadline-passed", 300)]
)
def test_runs_of_other_sessions_that_can_no_longer_call_hold_no_site_cap_slot(sql, dead, now):
	"""Two crashed workers held both slots of a site cap of 2 until the sweep (not scheduled)
	or a click on each of their sessions: every other session was refused. A run whose lease
	or deadline has passed can neither claim nor begin a call, so it holds no slot; a live one
	still does, to its last second."""
	_crashed_workers(user_b="other-user")
	if dead == "deadline-passed":  # a slice delivered after the deadline: its lease is still live
		for run in ("run-a", "run-b"):
			sql.primary.set_value(store.RUN, run, {"deadline": 300}, update_modified=False)
		sql.primary.commit()
	live = ai_jobs._retry_sql(lambda: admit("run-c", "parent-c", cap=2, user="third-user", now=299))
	assert live == {"status": "refused", "reason": "site_cap"}
	out = ai_jobs._retry_sql(lambda: admit("run-c", "parent-c", cap=2, user="third-user", now=now))
	assert out["status"] == "queued" and "taken_over" not in out
	assert value(sql, store.RUN, "run-a", "state") == "running"
	assert value(sql, store.RUN, "run-b", "state") == "running"


def test_a_cancel_with_a_filter_for_the_run_id_cancels_nothing(sql):
	"""A filter in place of the run id became a locking read of the Run table by filter
	(``WHERE state = 'running' ... FOR UPDATE``) that cancelled the session's run without
	naming it."""
	claimed()
	out = ai_jobs.cancel_refresh({"state": "running"}, docname="parent-a", requested_by="fake-user")
	assert out == {"status": "refused", "reason": "not_running"}
	assert value(sql, store.RUN, "run-a", "state") == "running"
	assert value(sql, store.RUN, "run-a", "active_session") == "parent-a"


def test_the_http_admission_ignores_runs_that_can_no_longer_call(sql, monkeypatch):
	_crashed_workers(user_b="other-user")
	_http_ready(monkeypatch)
	monkeypatch.setattr(ai_jobs, "_now", lambda: 1000.0)
	out = ai_jobs.start_refresh(docname="parent-c", session_uuid="fake-parent-c", requested_by="third-user")
	assert out["status"] == "queued"
	assert value(sql, store.RUN, out["refresh"]["run_id"], "active_session") == "parent-c"


def test_runs_of_a_user_that_can_no_longer_call_hold_no_user_cap_slot(sql):
	_crashed_workers()
	live = ai_jobs._retry_sql(lambda: admit("run-c", "parent-c", cap=10, user_cap=2, now=300))
	assert live == {"status": "refused", "reason": "user_cap"}
	out = ai_jobs._retry_sql(lambda: admit("run-c", "parent-c", cap=10, user_cap=2, now=1000))
	assert out["status"] == "queued"


def test_a_dead_run_of_a_deleted_session_holds_no_cap_slot(sql):
	"""No admission on a deleted session can take its run over (``not_ready`` comes first),
	and the sweep is not scheduled: the reservation stays, but holds no slot once its lease
	has passed."""
	assert claimed("run-a", "parent-a", "worker-a")
	sql.primary.sql(f"DELETE FROM {sql.primary.table('Optimus Session')} WHERE name = 'parent-a'")
	sql.primary.commit()
	assert ai_jobs._retry_sql(lambda: admit("run-x", "parent-a", cap=1, now=1000)) == {
		"status": "refused",
		"reason": "not_ready",
	}
	out = ai_jobs._retry_sql(lambda: admit("run-b", "parent-b", cap=1, user="other-user", now=1000))
	assert out["status"] == "queued"
	assert value(sql, store.RUN, "run-a", "active_session") == "parent-a"


# --- both journal tables keep a session_name index ----------------------------------------


def _indexed_columns(db, table):
	"""``{column: number of indexes whose only column it is}`` for one table, from the catalog."""
	if BACKEND == "mariadb":
		rows = db.sql(f"SHOW INDEX FROM {db.quoted(table)}")
		columns = {}
		for row in rows:
			if row["Seq_in_index"] == 1 and not any(
				r["Key_name"] == row["Key_name"] and r["Seq_in_index"] == 2 for r in rows
			):
				columns[row["Column_name"]] = columns.get(row["Column_name"], 0) + 1
		return columns
	columns = {}
	for row in db.sql(
		"SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = %s", [table]
	):
		match = re.search(r'\("?([a-z_]+)"?\)$', row["indexdef"].strip())
		if match:
			columns[match.group(1)] = columns.get(match.group(1), 0) + 1
	return columns


def _index_patch_on(sql, monkeypatch):
	"""The index patch run against the facade's real catalog. Only the names move: the
	facade's tables carry a test prefix, so the patch's ``tab...`` table names are mapped
	to them and the index names it picks are prefixed (index names are schema-global)."""
	from importlib import import_module

	patch = import_module("optimus.patches.v0_12_0.initialize_ai_refresh_store")
	tables = {"tab" + doctype: sql.primary.tables[doctype] for doctype in patch.JOURNAL_DOCTYPES}
	prefix = sql.primary.tables[store.RUN].removesuffix("run")
	created = []

	def fields(doctype):
		folder = doctype.lower().replace(" ", "_")
		doc = json.loads((_DOCTYPES / folder / (folder + ".json")).read_text())
		return [SimpleNamespace(**{"unique": 0, "search_index": 0, **f}) for f in doc["fields"]]

	def add_index(doctype, columns, index_name):
		created.append(index_name)
		table = sql.primary.tables[doctype]
		sql.primary.sql(f'CREATE INDEX IF NOT EXISTS "{prefix + index_name}" ON "{table}" ("{columns[0]}")')
		sql.primary.commit()

	db = SimpleNamespace(
		db_type=BACKEND,
		db_schema="public",
		sql=lambda query, values: (
			[tuple(r.values()) for r in sql.primary.sql(query, [values[0], tables[values[1]]])]
			if BACKEND == "postgres"
			else pytest.fail("MariaDB must not read the catalog")
		),
		add_index=add_index,
	)
	monkeypatch.setattr(
		patch, "frappe", SimpleNamespace(db=db, get_meta=lambda d: SimpleNamespace(fields=fields(d)))
	)
	return patch, created


def test_both_journal_tables_keep_their_session_index(sql, monkeypatch):
	"""Frappe names a PostgreSQL index after its column, so the second table to claim
	``session_name`` got none. After the patch both have one, and a second run changes nothing.
	MariaDB names indexes per table: both have one before the patch, and it adds none."""
	run, attempt = sql.primary.tables[store.RUN], sql.primary.tables[store.ATTEMPT]
	before = (_indexed_columns(sql.primary, run), _indexed_columns(sql.primary, attempt))
	if BACKEND == "postgres":
		# the collision, as Frappe's own DDL leaves it: exactly one of the two has the index
		assert [c.get("session_name", 0) for c in before] in ([1, 0], [0, 1])
	else:
		assert [c.get("session_name", 0) for c in before] == [1, 1]
	patch, created = _index_patch_on(sql, monkeypatch)
	patch.ensure_indexes()
	after = (_indexed_columns(sql.primary, run), _indexed_columns(sql.primary, attempt))
	assert [c["session_name"] for c in after] == [1, 1]
	assert all(
		c.get(k)
		for c, cols in zip(after, (("requested_by", "state", "resume_from"), ("run_id",)), strict=True)
		for k in cols
	)
	if BACKEND == "postgres":
		assert len(created) == 1 and "session_name" in created[0]
	else:
		assert created == [] and after == before
	patch.ensure_indexes()
	again = (_indexed_columns(sql.primary, run), _indexed_columns(sql.primary, attempt))
	assert again == after
	assert len(created) <= 1  # the second run created nothing


# --- the runbook's reconciliation query, run as written ------------------------------------

_DOCS = Path(__file__).resolve().parents[2] / "docs"


def _runbook_sql(marker):
	"""The fenced SQL block of AI-REFRESH.md whose first line is ``-- <marker>``."""
	text = (_DOCS / "AI-REFRESH.md").read_text(encoding="utf-8")
	blocks = re.findall(r"```sql\n(-- " + re.escape(marker) + r"\n.*?)```", text, re.S)
	assert len(blocks) == 1, marker
	return blocks[0]


def test_the_uncertain_query_exposes_the_provider_usage_window(sql):
	"""Run the documented query, retaining only this session's uncertain attempts."""
	for name, session, state in (
		("unknown-call", "parent-a", "uncertain"),
		("known-call", "parent-a", "succeeded"),
		("other-call", "parent-b", "uncertain"),
	):
		sql.primary.insert({
			"doctype": store.ATTEMPT, "name": name, "attempt_id": name,
			"run_id": "fake-run", "session_name": session, "kind": "fix",
			"target_name": "fake-finding", "state": state, "started_at_epoch": 100,
			"finished_at_epoch": 120, "tokens_reported": 7, "error_kind": "timeout",
		})
	sql.primary.commit()
	query = _runbook_sql("uncertain attempts (MariaDB)").replace("<session name>", "parent-a")
	if BACKEND == "postgres":
		query = query.replace("`", '"')
	for doctype, table in sql.primary.tables.items():
		query = query.replace("tab" + doctype, table)
	assert sql.primary.sql(query) == [{
		"run_id": "fake-run", "kind": "fix", "target_name": "fake-finding", "error_kind": "timeout",
		"started_at_epoch": 100, "finished_at_epoch": 120, "tokens_reported": 7,
	}]


def test_the_reconciliation_query_separates_a_gap_from_other_sources(sql):
	"""A session's ``ai_tokens_spent`` against what the journal reported and the increments the
	counter logged as dropped (``optimus ai spend``). Seeded: a reconciled session, a session
	whose counter lost 30 tokens with no row, and one with another source's tokens plus a
	stale dropped-increment candidate from a settlement that later committed. The query
	is the runbook's own text, with only the table names mapped to the test's tables."""
	primary = sql.primary
	log = primary.tables["Optimus Session"].removesuffix("session") + "errorlog"
	primary.tables["Error Log"] = log
	text = "varchar(140)"
	body = "longtext" if BACKEND == "mariadb" else "text"
	primary.sql(
		f"create table {primary.quoted(log)} (name varchar(140) primary key, method {text}, "
		f"reference_doctype {text}, reference_name {text}, error {body})"
		+ (" CHARACTER SET=utf8mb4 COLLATE=utf8mb4_unicode_ci" if BACKEND == "mariadb" else "")
	)
	try:
		spent = {"parent-a": 110, "parent-b": 90, "parent-c": 150}
		for name, tokens in spent.items():
			primary.set_value("Optimus Session", name, {"ai_tokens_spent": tokens})
		for run_id, session, tokens in (
			("r-a1", "parent-a", 70),
			("r-a2", "parent-a", 50),
			("r-b1", "parent-b", 120),
			("r-c1", "parent-c", 100),
		):
			primary.insert(
				{
					"doctype": store.RUN,
					"name": run_id,
					"run_id": run_id,
					"session_name": session,
					"tokens_reported": tokens,
				}
			)
		for i, (title, session, error) in enumerate(
			(
				(
					"optimus ai spend",
					"parent-a",
					"optimus ai spend\nfield=ai_tokens_spent\namount=10\nsavepoint_rollback=done\nTraceback",
				),
				("optimus ai refresh count", "parent-a", "optimus ai refresh count\namount=1"),
				# the whole transaction was gone and the settlement was retried and charged
				(
					"optimus ai spend",
					"parent-a",
					"optimus ai spend\namount=25\nsavepoint_rollback=failed: OperationalError",
				),
				("optimus ai spend", "parent-x", "optimus ai spend\namount=999"),
				(
					"optimus ai spend",
					"parent-c",
					"optimus ai spend\namount=10\nsavepoint_rollback=done\noutcome=committed on try 2",
				),
			)
		):
			primary.sql(
				f"insert into {primary.quoted(log)} values (%s, %s, 'Optimus Session', %s, %s)",
				[f"e{i}", title, session, error],
			)
		primary.commit()
		query = _runbook_sql("reconcile (MariaDB)" if BACKEND == "mariadb" else "reconcile (PostgreSQL)")
		for doctype, table in primary.tables.items():
			query = query.replace("tab" + doctype, table)
		rows = {r["session"]: r for r in primary.sql(query.replace("%", "%%"))}
		primary.rollback()
		assert set(rows) == {"parent-a", "parent-b", "parent-c"}
		assert [
			int(rows["parent-a"][k])
			for k in ("spent", "journal_tokens", "dropped_increments", "other_sources")
		] == [
			110,
			120,
			10,
			0,
		]
		assert int(rows["parent-b"]["other_sources"]) == -30  # the gap nothing explains
		# The query includes this candidate even though the settlement later committed.
		# Its raw difference is not proof: the operator must exclude the stale ten tokens.
		assert int(rows["parent-c"]["dropped_increments"]) == 10
		assert int(rows["parent-c"]["other_sources"]) == 60  # 50 other-source tokens + 10 stale
		# the self-check finds the run whose attempts do not add up to its total, and only it
		for run_id, attempt, tokens in (("r-a1", "at-1", 70), ("r-a2", "at-2", 20), ("r-a2", "at-3", 20)):
			primary.insert(
				{
					"doctype": store.ATTEMPT,
					"name": attempt,
					"attempt_id": attempt,
					"run_id": run_id,
					"tokens_reported": tokens,
				}
			)
		primary.commit()
		check = _runbook_sql("self-check (MariaDB)")
		if BACKEND == "postgres":
			check = check.replace("`", '"')
		for doctype, table in primary.tables.items():
			check = check.replace("tab" + doctype, table)
		assert [r["run"] for r in primary.sql(check)] == ["r-a2"]
		primary.rollback()
	finally:
		primary.rollback()
		primary.sql("DROP TABLE " + primary.quoted(log))
		primary.commit()
		del primary.tables["Error Log"]


def test_the_active_runs_query_flags_an_expired_run_on_every_database(sql):
	"""The runbook's first query as written on MariaDB, and its Postgres form from the
	runbook's own comment. Epochs are seconds, as the engine stores them."""
	primary = sql.primary
	for run_id, session, lease, active in (
		("r-dead", "parent-a", 5.0, "parent-a"),
		("r-live", "parent-b", 9e10, "parent-b"),
		("r-done", "parent-c", 0.0, None),
	):
		primary.insert(
			{
				"doctype": store.RUN,
				"name": run_id,
				"run_id": run_id,
				"session_name": session,
				"active_session": active,
				"state": "running",
				"lease_until": lease,
				"deadline": 9e10,
			}
		)
	primary.commit()
	text = (_DOCS / "AI-REFRESH.md").read_text(encoding="utf-8")
	query = _runbook_sql("active runs, and whether they have expired (run in: bench --site <site> mariadb)")
	query = query.split(";")[0]
	if BACKEND == "postgres":
		now = re.search(r"--   lease_until <= (extract\(epoch from now\(\)\)) OR", text).group(1)
		query = query.replace("UNIX_TIMESTAMP()", now).replace("`", '"')
	for doctype, table in primary.tables.items():
		query = query.replace("tab" + doctype, table)
	rows = {r["name"]: bool(r["expired"]) for r in primary.sql(query.replace("%", "%%"))}
	primary.rollback()
	assert rows == {"r-dead": True, "r-live": False}


def phase2_admit(sql):
	sql.primary.insert({"doctype": phase2.TABLE, "name": "fake-child", "parent": "parent-a",
		"run_uuid": "fake-phase2", "status": "Failed"})
	sql.primary.connection.commit()
	out = ai_jobs._retry_sql(lambda: phase2._admit("parent-a", "fake-parent-a", "fake-phase2", "fake-user",
		generation="generation-a", now=100))
	assert out["status"] == "queued"


def phase2_claim():
	return ai_jobs._retry_sql(lambda: phase2._claim("parent-a", "fake-phase2", "generation-a", "worker-a", now=101))


def test_phase2_duplicate_delivery_from_old_snapshot_claims_once(sql):
	phase2_admit(sql)
	older = sql.connect()
	older.get_value(phase2.TABLE, "fake-child", "analyze_worker_token")
	assert phase2_claim()
	with sql.using(older):
		assert phase2_claim() is None
	assert older.conflicts == []  # the public SQL wrapper closes the old snapshot first


def test_phase2_answer_and_ready_outcome_roll_back_together(sql):
	sql.primary.set_value("answer", "row-00", {"body": "previous answer"}, update_modified=False)
	sql.primary.commit()
	phase2_admit(sql)
	run = phase2_claim()
	def fail():
		sql.primary.set_value("answer", "row-00", {"body": "fake phase2 answer"}, update_modified=False)
		raise RuntimeError("fake save failure")
	with pytest.raises(RuntimeError):
		ai_jobs._transaction(lambda: phase2._complete(run, now=102, persist=fail))
	assert value(sql, "answer", "row-00", "body") == "previous answer"
	assert sql.primary.get_value(phase2.TABLE, "fake-child", "status") == "Analyzing"


def test_phase2_ambiguous_commit_replay_does_not_append_twice(sql):
	phase2_admit(sql)
	run = phase2_claim()
	def persist():
		analyze._bump_ai_refresh_count("parent-a")  # stand-in append count, same real SQL transaction
	assert ai_jobs._transaction(lambda: phase2._complete(run, now=102, persist=persist))
	assert not ai_jobs._transaction(lambda: phase2._complete(run, now=103, persist=persist))
	assert not ai_jobs._transaction(lambda: phase2._fail(run, now=103, reason="failed"))
	assert sql.primary.get_value("Optimus Session", "parent-a", "ai_refresh_count") == 1
	assert sql.primary.get_value(phase2.TABLE, "fake-child", "status") == "Ready"


def test_phase2_old_worker_cannot_commit_after_expired_generation_is_replaced(sql):
	phase2_admit(sql)
	run = phase2_claim()
	out = ai_jobs._retry_sql(lambda: phase2._admit("parent-a", "fake-parent-a", "fake-phase2", "fake-user",
		generation="generation-b", now=3000))
	assert out["status"] == "queued"
	writes = []
	assert not ai_jobs._transaction(lambda: phase2._complete(run, now=3001, persist=lambda: writes.append(True)))
	assert not writes
	assert sql.primary.get_value(phase2.TABLE, "fake-child", "analyze_generation") == "generation-b"


@pytest.mark.parametrize("committed", [False, True])
def test_phase2_result_commit_readback_uses_durable_generation(sql, monkeypatch, committed):
	phase2_admit(sql)
	run = phase2_claim()
	monkeypatch.setattr(phase2, "_now", lambda: 102)
	commit = sql.primary.commit
	lost = False
	writes = []
	def lose_ack():
		nonlocal lost
		if not lost and sql.primary.get_value(phase2.TABLE, "fake-child", "status") == "Ready":
			lost = True
			if committed:
				commit()
			raise ConnectionError("fake lost commit acknowledgement")
		commit()
	monkeypatch.setattr(sql.primary, "commit", lose_ack)
	def persist():
		writes.append(True)
		sql.primary.set_value("answer", "row-00", {"body": "durable phase2 answer"}, update_modified=False)
	if committed:
		assert phase2._save_results(run, persist)
	else:
		with pytest.raises(ConnectionError):
			phase2._save_results(run, persist)
	assert lost and writes == [True]
	assert sql.primary.get_value(phase2.TABLE, "fake-child", "status") == ("Ready" if committed else "Analyzing")
	assert (value(sql, "answer", "row-00", "body") == "durable phase2 answer") is committed


def test_provider_settings_and_credential_share_a_sql_snapshot(sql, monkeypatch):
	"""The production SELECT cannot pair an old endpoint with a new password."""
	import frappe

	primary, writer = sql.primary, sql.connect()
	names = {"tabSingles": primary.tables["Optimus Session"] + "_singles",
		"__Auth": primary.tables["Optimus Session"] + "_auth"}
	created = []
	queries = []
	def read(queries_by_backend, values):
		query = queries_by_backend[sql.primary.backend]
		for original, replacement in names.items():
			query = query.replace(primary.quoted(original), primary.quoted(replacement))
		queries.append(query)
		return [tuple(row.values()) for row in primary.sql(query, values)]
	try:
		for original, columns in (("tabSingles", "doctype VARCHAR(140), field VARCHAR(140), value TEXT"),
			("__Auth", "doctype VARCHAR(140), name VARCHAR(140), fieldname VARCHAR(140), password TEXT, encrypted INTEGER")):
			primary.sql("CREATE TABLE " + primary.quoted(names[original]) + " (" + columns + ")"
				+ (" ENGINE=InnoDB" if sql.primary.backend == "mariadb" else ""))
			created.append(names[original])
		for field, value in {"ai_enabled": "1", "ai_provider": "OpenAI-compatible", "ai_base_url": "https://first.invalid/v1", "ai_model": "fake"}.items():
			primary.sql("INSERT INTO " + primary.quoted(names["tabSingles"]) + " VALUES (%s, %s, %s)", ("Optimus Settings", field, value))
		primary.sql("INSERT INTO " + primary.quoted(names["__Auth"]) + " VALUES (%s, %s, %s, %s, %s)",
			("Optimus Settings", "Optimus Settings", "ai_api_key", "first encrypted value", 1))
		primary.connection.commit()

		monkeypatch.setattr(frappe, "db", SimpleNamespace(multisql=read), raising=False)
		before, ciphertext = ai_fix._read_provider_snapshot()
		assert (before.ai_base_url, ciphertext) == ("https://first.invalid/v1", "first encrypted value")
		writer.sql("UPDATE " + writer.quoted(names["tabSingles"]) + " SET value=%s WHERE field=%s",
			("https://second.invalid/v1", "ai_base_url"))
		writer.sql("UPDATE " + writer.quoted(names["__Auth"]) + " SET password=%s", ("second encrypted value",))
		writer.connection.commit()
		old, ciphertext = ai_fix._read_provider_snapshot()
		assert (old.ai_base_url, ciphertext) == (before.ai_base_url, "first encrypted value")
		primary.connection.commit()
		fresh, ciphertext = ai_fix._read_provider_snapshot()
		assert (fresh.ai_base_url, ciphertext) == ("https://second.invalid/v1", "second encrypted value")
		assert len(queries) == 3
	finally:
		primary.rollback()
		writer.rollback()
		for name in reversed(created):
			primary.sql("DROP TABLE " + primary.quoted(name))
		primary.connection.commit()


def test_force_stop_cannot_erase_concurrently_claimed_capture_from_old_snapshot(sql):
	phase2_admit(sql)
	sql.primary.set_value(phase2.TABLE, "fake-child", {"status": "Recording", "recording_user": "capture-user"})
	sql.primary.connection.commit()
	older = sql.connect()
	assert older.get_value(phase2.TABLE, "fake-child", "status") == "Recording"
	out = ai_jobs._retry_sql(lambda: phase2._admit("parent-a", "fake-parent-a", "fake-phase2", "capture-user",
		generation="generation-b", now=200, from_recording=True))
	assert out["status"] == "queued"
	with sql.using(older):
		assert not ai_jobs._retry_sql(lambda: phase2._force_stop_capture("parent-a", "fake-phase2", "capture-user"))
	assert sql.primary.get_value(phase2.TABLE, "fake-child", "status") == "Analyzing"
	assert older.conflicts == []  # the shared wrapper ends the old snapshot before locking


def test_capture_recovery_query_selects_only_actual_or_legacy_recording_actor(sql):
	sql.primary.set_value("Optimus Session", "parent-a", {"user": "legacy-user"})
	for name, actor, status in (("own", "capture-user", "Recording"), ("foreign", "other-user", "Recording"),
		("done", "capture-user", "Ready"), ("legacy", None, "Recording")):
		sql.primary.insert({"doctype": phase2.TABLE, "name": name, "parent": "parent-a", "run_uuid": name,
			"recording_user": actor, "status": status, "creation": "2026-10-03"})
	sql.primary.connection.commit()
	assert [row["run_uuid"] for row in phase2._capture_candidates("capture-user")] == ["own"]
	assert [row["run_uuid"] for row in phase2._capture_candidates("legacy-user")] == ["legacy"]
	assert not phase2._capture_candidates("capture-user' OR 1=1 --")


def test_parent_deletion_and_journal_removal_roll_back_together_and_fence_late_result(sql):
	claimed()
	attempt = ai_jobs._retry_sql(lambda: store.begin_attempt("run-a", docname="parent-a", worker_token="worker-a",
		kind="fix", target="fake-finding", input_hash="fake-hash", now=12))
	def delete(fail=False):
		store.delete_session_journal("parent-a", "fake-parent-a")
		sql.primary.delete("Optimus Session", {"name": "parent-a"})
		if fail:
			raise RuntimeError("fake delete failure")
	with pytest.raises(RuntimeError, match="fake delete failure"):
		ai_jobs._transaction(lambda: delete(True))
	assert sql.primary.get_value("Optimus Session", "parent-a")
	assert sql.primary.get_value(store.RUN, "run-a")
	assert sql.primary.get_value(store.ATTEMPT, attempt["name"], "state") == "calling"
	ai_jobs._transaction(delete)
	assert sql.primary.get_value("Optimus Session", "parent-a") is None
	assert sql.primary.get_value(store.ATTEMPT, attempt["name"]) is None
	assert not ai_jobs._retry_sql(lambda: store.settle_attempt("run-a", attempt["name"], docname="parent-a",
		worker_token="worker-a", now=13, outcome="succeeded", tokens=100, usage_complete=True,
		persist=lambda: pytest.fail("late result persisted")))
