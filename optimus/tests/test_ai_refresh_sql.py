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
``OPTIMUS_TEST_SQL_PORT`` overrides the port of the fixed, fake CI credentials. Every table
is uniquely named and owned by the test.
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
	fields = json.loads((_DOCTYPES / folder / (folder + ".json")).read_text())["fields"]
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
		self.conflicts = []  # the driver code of every failed statement

	def quoted(self, value):
		assert re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", value)
		quote = "`" if self.backend == "mariadb" else '"'
		return quote + value + quote

	def table(self, doctype):
		return self.quoted(self.tables[doctype])

	def sql(self, query, values=()):
		"""``Database.sql``: run one statement and raise its error the way Frappe does."""
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
			if isinstance(value, (list, tuple)):
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
			query += " ORDER BY " + self.quoted(order_by) + " DESC"
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


@pytest.fixture
def sql(monkeypatch):
	if not BACKEND:
		pytest.skip("Requires a disposable SQL validation service")
	assert BACKEND in {"mariadb", "postgres"}
	prefix = "opr_" + secrets.token_hex(6)
	tables = {
		doctype: prefix + "_" + suffix
		for doctype, suffix in (
			("Optimus Session", "session"),
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
	monkeypatch.setattr(
		ai_fix, "log_ai_failure", lambda title, exc=None, **kw: counter_rows.append((title, exc, kw))
	)
	functions = ModuleType("frappe.query_builder.functions")
	functions.Coalesce = _Coalesce
	monkeypatch.setitem(sys.modules, "frappe.query_builder", ModuleType("frappe.query_builder"))
	monkeypatch.setitem(sys.modules, "frappe.query_builder.functions", functions)
	created = []
	try:
		for doctype in ("Optimus Session", store.RUN, store.ATTEMPT, store.CONTROL):
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


def admit(name="run-a", parent="parent-a", cap=5, scope="all", user="fake-user"):
	return store.admit(
		run_id=name,
		docname=parent,
		session_uuid="fake-" + parent,
		requested_by=user,
		scope=scope,
		now=10,
		max_seconds=3600,
		site_cap=cap,
		user_cap=5,
		cap=20,
		include_fixes=True,
		include_steps=False,
		regenerate_all=False,
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

	def read(table, name, *, lock=False):
		row = real_read(table, name, lock=lock)
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
	"""P2 / E5: the second delivery waits on the session lock the first holds. Its first
	statement is that lock, so on MariaDB 11.8 the run row the first changed is no 1020."""
	ai_jobs._retry_sql(admit)
	locked, release = threading.Event(), threading.Event()
	real_locked = store._locked

	def pausing(run_id, docname):
		out = real_locked(run_id, docname)
		if threading.current_thread().name == "first" and not locked.is_set():
			locked.set()
			assert release.wait(10)
		return out

	monkeypatch.setattr(store, "_locked", pausing)

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
	"""P2: #72's sync Refresh (or a Desk save) holds the session row while the engine begins
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
	"""R2 / P1: a locking read of a not-yet-existing attempt key took a gap lock, and two
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
	"""C1 / P4: the admission's FOR UPDATE reads (the reservation probe, the capacity scan)
	locked index ranges that another session's worker needed to finish, continue or be
	cancelled; the worker was the deadlock victim. The admission now reads other runs
	without locks."""
	history(sql)
	claimed("run-b", "parent-b", "worker-b")
	sql.primary.set_value(store.RUN, "run-b", {"completed": 1}, update_modified=False)
	sql.primary.commit()
	locked, release = threading.Event(), threading.Event()
	real_locked = store._locked

	def pausing(run_id, docname):
		out = real_locked(run_id, docname)
		if threading.current_thread().name == "worker-b" and not locked.is_set():
			locked.set()
			assert release.wait(10)
		return out

	monkeypatch.setattr(store, "_locked", pausing)
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
		# One refresh, counted once by #72's real counter.
		assert value(sql, "Optimus Session", "parent-b", "ai_refresh_count") == 1


@pytest.mark.parametrize("transition", ["finish", "cancel"])
def test_an_admission_holds_no_lock_another_sessions_worker_needs(sql, monkeypatch, transition):
	"""P4: the admission is paused after all its reads, holding every lock it took; another
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
	"""P3: the session row is locked before the site mutex, so another session's admission
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
	largest = ai_fix._MAX_TOKEN_COUNT
	for target in ("finding-a", "finding-b"):
		attempt = begun(target=target)
		for _ in range(2):
			save(sql, attempt, tokens=largest)
	assert value(sql, "Optimus Session", "parent-a", "ai_tokens_spent") == 2 * largest
	assert value(sql, store.RUN, "run-a", "tokens_reported") == 2 * largest
	assert value(sql, store.RUN, "run-a", "completed") == 2


def test_a_counter_statement_failure_keeps_the_answer_and_logs_the_dropped_spend(sql):
	"""#72's D2 trade-off on a real database: the counter's statement fails (BIGINT overflow
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


def test_a_deadlock_during_settlement_is_retried_and_charged_once(sql):
	"""A whole-transaction rollback in the middle of the settlement (the answer row is held by
	a heavier transaction that then wants the session row): ``_settle`` rolls back and writes
	again, the settled bit and the counter charge exactly once, and no provider is involved."""
	claimed()
	attempt = begun()
	rival = sql.connect()
	answer = rival.table("answer")
	rival.sql(f"UPDATE {answer} SET body = 'rival' WHERE name <> 'row-00'")  # the heavier side
	rival.sql(f"SELECT name FROM {answer} WHERE name = 'row-00' FOR UPDATE")
	persisted = []

	def persist():
		persisted.append(True)
		sql.facade.db.sql(f"UPDATE {sql.facade.db.table('answer')} SET body = 'engine' WHERE name = 'row-00'")

	def run_rival():
		wait_until_blocked(sql)  # the settlement waits on the answer row
		try:
			rival.get_value("Optimus Session", "parent-a", "name", for_update=True)
		except Exception as exc:
			persisted.append(exc)
		rival.commit()

	thread = threading.Thread(target=run_rival)
	thread.start()
	with sql.using(sql.connect()) as db:
		assert save(sql, attempt, persist=persist) is True
	thread.join(30)
	assert db.conflicts and db.conflicts[0] in (1213, "40P01")
	assert persisted[:2] == [True, True]
	assert value(sql, "answer", "row-00", "body") == "engine"
	assert value(sql, store.ATTEMPT, attempt["name"], "settled") == 1
	assert value(sql, store.RUN, "run-a", "completed") == 1
	assert value(sql, "Optimus Session", "parent-a", "ai_tokens_spent") == 7


def test_completion_and_nullable_reservation_allow_a_later_run(sql):
	claimed()
	ai_jobs._retry_sql(lambda: store.cancel("run-a", docname="parent-a", now=12))
	assert ai_jobs._retry_sql(lambda: admit("run-b"))["status"] == "queued"
	assert value(sql, store.RUN, "run-a", "active_session") is None
	assert value(sql, store.RUN, "run-b", "active_session") == "parent-a"


def test_an_http_admission_waits_for_a_held_session_row_once_then_answers_busy(sql, monkeypatch):
	"""F-5: a user refresh blocked on its session row waits one lock wait timeout, then
	answers busy without a second attempt, so a web request never outlives the server's
	timeout on retries. The wait is shortened to one second on this connection."""
	monkeypatch.setattr(ai_jobs, "_check_run", lambda run, **kw: None)
	monkeypatch.setattr(ai_jobs, "_workers_listening", lambda: True)
	monkeypatch.setattr(ai_jobs, "_dispatch_pending", lambda run_id, docname: True)
	monkeypatch.setattr(ai_fix, "is_available", lambda **kw: True)
	writer = sql.connect()
	writer.get_value("Optimus Session", "parent-a", "status", for_update=True)
	contender = sql.connect()
	contender.sql(
		"SET SESSION innodb_lock_wait_timeout=1" if BACKEND == "mariadb" else "SET lock_timeout = '1s'"
	)
	contender.commit()
	with sql.using(contender):
		out = ai_jobs.start_refresh(
			docname="parent-a", session_uuid="fake-parent-a", requested_by="fake-user"
		)
	assert out == {"status": "refused", "reason": "busy"}
	assert contender.conflicts == ([1205] if BACKEND == "mariadb" else ["55P03"])
	writer.commit()
	assert value(sql, store.RUN, "run-a", "name") is None
