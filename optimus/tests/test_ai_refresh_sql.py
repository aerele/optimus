"""Real SQL isolation/atomicity checks on disposable CI databases.

The small adapter implements the database operations the journal uses, with
real SELECT FOR UPDATE locks and REPEATABLE READ transactions. It deliberately
does not claim to test Frappe document hooks; real-bench integration covers
framework installation. Every table is uniquely named and owned by this test.
Set OPTIMUS_TEST_SQL to mariadb or postgres. A configured backend never skips.
"""

import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from optimus import ai_fix, ai_jobs
from optimus import ai_refresh_store as store
from optimus.line_profile import jobs as phase2


class SQLDatabase:
	def __init__(self, connection, tables, backend):
		self.connection, self.tables, self.backend = connection, tables, backend
		self.conflicts = []

	def quoted(self, value):
		assert re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", value)
		quote = "`" if self.backend == "mariadb" else '"'
		return quote + value + quote

	def execute(self, query, values=()):
		with self.connection.cursor() as cursor:
			try:
				cursor.execute(query, values)
			except Exception as exc:
				self.conflicts.append(getattr(exc, "pgcode", None) or exc.args[0])
				raise
			if cursor.description:
				# Both Frappe backends expose DECIMAL/Float as Python float.
				return [
					dict(
						zip(
							(col[0] for col in cursor.description),
							(float(v) if isinstance(v, Decimal) else v for v in row),
							strict=True,
						)
					)
					for row in cursor.fetchall()
				]
		return []

	def get_values(self, table, filters, fields, *, for_update=False, wait=True, limit=0, **kw):
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
		columns = (
			"*"
			if fields == "*"
			else ",".join(self.quoted(k) for k in ([fields] if isinstance(fields, str) else fields))
		)
		query = (
			"SELECT " + columns + " FROM " + self.quoted(self.tables[table]) + " WHERE " + " AND ".join(where)
		)
		if limit:
			assert type(limit) is int and limit > 0
			query += " LIMIT " + str(limit)
		if for_update:
			query += " FOR UPDATE" + ("" if wait else " NOWAIT")
		return self.execute(query, params)

	def get_value(self, table, filters, fields="name", *, as_dict=False, **kw):
		rows = self.get_values(table, filters, fields, limit=1, **kw)
		if not rows:
			return None
		return rows[0] if as_dict or fields == "*" else next(iter(rows[0].values()))

	def set_value(self, table, name, values, **kw):
		assignments = ",".join(self.quoted(key) + "=%s" for key in values)
		self.execute(
			"UPDATE " + self.quoted(self.tables[table]) + " SET " + assignments + " WHERE name=%s",
			[*values.values(), name],
		)

	def table_exists(self, table):
		return table in self.tables

	def delete(self, table, filters):
		where = " AND ".join(self.quoted(key) + "=%s" for key in filters)
		self.execute("DELETE FROM " + self.quoted(self.tables[table]) + " WHERE " + where, list(filters.values()))

	def insert(self, values):
		values = dict(values)
		table = values.pop("doctype")
		self.execute(
			"INSERT INTO "
			+ self.quoted(self.tables[table])
			+ " ("
			+ ",".join(self.quoted(key) for key in values)
			+ ") VALUES ("
			+ ",".join(["%s"] * len(values))
			+ ")",
			list(values.values()),
		)

	def increment(self, name, field, amount):
		column = self.quoted(field)
		self.execute(
			"UPDATE "
			+ self.quoted(self.tables["Optimus Session"])
			+ " SET "
			+ column
			+ "=COALESCE("
			+ column
			+ ",0)+%s WHERE name=%s",
			[amount, name],
		)

	def rollback(self):
		self.connection.rollback()

	def multisql(self, queries, values, *, as_dict=False):
		query = queries[self.backend]
		quote = "`" if self.backend == "mariadb" else '"'
		for table, name in self.tables.items():
			query = query.replace(quote + "tab" + table + quote, self.quoted(name))
		return self.execute(query, values)


@pytest.fixture
def sql(monkeypatch):
	backend = os.environ.get("OPTIMUS_TEST_SQL")
	if not backend:
		pytest.skip("Requires a disposable SQL validation service")
	assert backend in {"mariadb", "postgres"}
	prefix = "opr_" + secrets.token_hex(6)
	tables = {
		table: prefix + "_" + suffix
		for table, suffix in (
			("Optimus Session", "session"),
			("Optimus Phase Two Run", "phase2"),
			(store.RUN, "run"),
			(store.ATTEMPT, "attempt"),
			(store.CONTROL, "control"),
		)
	}
	connections = []

	def connect():
		# Deliberately fixed fake CI credentials/database, never a bench config
		# or arbitrary DSN that could accidentally select production data.
		if backend == "mariadb":
			import pymysql

			connection = pymysql.connect(
				host="127.0.0.1",
				port=3306,
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
				port=5432,
				user="optimus_test",
				password="optimus_test",
				dbname="optimus_test",
				connect_timeout=5,
			)
		db = SQLDatabase(connection, tables, backend)
		if backend == "mariadb":
			db.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
			db.execute("SET SESSION innodb_lock_wait_timeout=2")
		else:
			connection.set_session(isolation_level="REPEATABLE READ")
			db.execute("SET lock_timeout = '2s'")
		connection.commit()
		connections.append(db)
		return db

	primary = connect()
	local = threading.local()
	local.db = primary

	class Facade:
		@property
		def db(self):
			return local.db

		def get_doc(self, values):
			return SimpleNamespace(insert=lambda **kw: self.db.insert(values))

	facade = Facade()
	monkeypatch.setattr(store, "frappe", facade)
	monkeypatch.setattr(ai_jobs, "frappe", facade)
	monkeypatch.setattr(phase2, "frappe", facade)
	monkeypatch.setattr(phase2, "_authorized", lambda *a: True)
	monkeypatch.setattr(ai_jobs, "_touch_session", lambda name: facade.db.set_value("Optimus Session", name, {"modified": "changed"}))
	monkeypatch.setattr(ai_jobs, "safe_commit", lambda: facade.db.connection.commit())
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **kw: None)
	monkeypatch.setattr(
		"optimus.analyze._add_ai_spend",
		lambda name, amount: facade.db.increment(name, "ai_tokens_spent", amount),
	)
	monkeypatch.setattr(
		"optimus.analyze._bump_ai_refresh_count",
		lambda name: facade.db.increment(name, "ai_refresh_count", 1),
	)
	created = []
	try:
		parent_columns = {
			"user": "VARCHAR(140)",
			"session_uuid": "VARCHAR(140)",
			"status": "VARCHAR(140)",
			"ai_tokens_spent": "BIGINT DEFAULT 0",
			"ai_refresh_count": "INTEGER DEFAULT 0",
			"answer": "TEXT",
			"modified": "VARCHAR(140)",
		}
		for table in tables:
			unique = []
			if table == "Optimus Session":
				columns = parent_columns
			elif table == "Optimus Phase Two Run":
				columns = {"parent": "VARCHAR(140)", "status": "VARCHAR(140)", "run_uuid": "VARCHAR(140)",
					"recording_user": "VARCHAR(140)", "creation": "VARCHAR(140)",
					"warnings_json": "TEXT", "analyze_generation": "VARCHAR(140)",
					"analyze_requested_by": "VARCHAR(140)", "analyze_worker_token": "VARCHAR(140)",
					"analyze_lease_until": "DECIMAL(21,9) DEFAULT 0", "analyze_dispatch_at": "DECIMAL(21,9) DEFAULT 0",
					"analyze_dispatch_pending": "SMALLINT DEFAULT 0", "analyze_render_pending": "SMALLINT DEFAULT 0",
					"analyze_attempts": "INTEGER DEFAULT 0"}
			else:
				folder = table.lower().replace(" ", "_")
				path = (
					Path(__file__).resolve().parents[1] / "optimus" / "doctype" / folder / (folder + ".json")
				)
				fields = json.loads(path.read_text())["fields"]
				types = {
					"Data": "VARCHAR(140)",
					"Select": "VARCHAR(140)",
					"Check": "SMALLINT DEFAULT 0",
					"Int": "INTEGER DEFAULT 0",
					"Long Int": "BIGINT DEFAULT 0",
					"Float": "DECIMAL(21,9) DEFAULT 0",
				}
				columns = {f["fieldname"]: types[f["fieldtype"]] for f in fields}
				unique = [f["fieldname"] for f in fields if f.get("unique")]
			ddl = ["name VARCHAR(140) PRIMARY KEY"] + [
				primary.quoted(k) + " " + v + (" UNIQUE" if k in unique else "") for k, v in columns.items()
			]
			primary.execute(
				"CREATE TABLE "
				+ primary.quoted(tables[table])
				+ " ("
				+ ",".join(ddl)
				+ ")"
				+ (" ENGINE=InnoDB" if backend == "mariadb" else "")
			)
			created.append(table)
		primary.insert({"doctype": store.CONTROL, "name": "site", "scope": "site", "generation": 0})
		for name in ("parent-a", "parent-b"):
			primary.insert(
				{
					"doctype": "Optimus Session",
					"name": name,
					"session_uuid": "fake-" + name,
					"status": "Ready",
				}
			)
		primary.connection.commit()

		@contextmanager
		def using(db):
			old = local.db
			local.db = db
			try:
				yield db
			finally:
				local.db = old

		yield SimpleNamespace(primary=primary, connect=connect, using=using, backend=backend)
	finally:
		for db in connections:
			db.rollback()
		for table in reversed(created):
			primary.execute("DROP TABLE " + primary.quoted(tables[table]))
		primary.connection.commit()
		for db in connections:
			db.connection.close()


def admit(name="run-a", parent="parent-a", cap=2):
	return store.admit(
		run_id=name,
		docname=parent,
		session_uuid="fake-" + parent,
		requested_by="fake-user",
		scope="all",
		now=10,
		max_seconds=3600,
		site_cap=cap,
		user_cap=2,
		cap=20,
		include_fixes=True,
		include_steps=False,
		regenerate_all=False,
	)


def claimed(sql):
	ai_jobs._retry_sql(admit)
	return ai_jobs._retry_sql(
		lambda: store.claim("run-a", slice_no=0, worker_token="worker-a", now=11, lease_seconds=300)
	)


def test_old_snapshot_cannot_admit_beyond_site_capacity(sql):
	older = sql.connect()
	older.get_value("Optimus Session", "parent-b", "status")
	assert ai_jobs._retry_sql(lambda: admit(cap=1))["status"] == "queued"
	with sql.using(older):
		assert ai_jobs._retry_sql(lambda: admit("run-b", "parent-b", cap=1)) == {
			"status": "refused",
			"reason": "site_cap",
		}
	if sql.backend == "postgres":
		assert "40001" in older.conflicts


def test_phase2_commit_fences_an_older_ai_admission_snapshot(sql):
	older = sql.connect()
	older.get_value("Optimus Session", "parent-a", "status")
	def phase2():
		assert store.lock_phase2_parent("parent-a") is None
		sql.primary.insert({"doctype": "Optimus Phase Two Run", "name": "phase2-a",
			"parent": "parent-a", "status": "Recording"})
	ai_jobs._transaction(phase2)
	with sql.using(older):
		assert ai_jobs._retry_sql(admit) == {"status": "refused", "reason": "phase2"}
	if sql.backend == "postgres":
		assert "40001" in older.conflicts


def test_ai_admission_fences_an_older_phase2_snapshot(sql):
	older = sql.connect()
	older.get_value("Optimus Session", "parent-a", "status")
	assert ai_jobs._retry_sql(admit)["status"] == "queued"
	with sql.using(older):
		assert ai_jobs._retry_sql(lambda: store.lock_phase2_parent("parent-a")) == "ai_refresh"


def test_duplicate_delivery_from_an_older_snapshot_claims_only_once(sql):
	ai_jobs._retry_sql(admit)
	older = sql.connect()
	older.get_value(store.RUN, "run-a", "state")

	def operation():
		return store.claim("run-a", slice_no=0, worker_token="same-worker", now=11, lease_seconds=300)

	assert ai_jobs._retry_sql(operation)
	with sql.using(older):
		assert ai_jobs._retry_sql(operation) is None


def test_outcome_answer_and_usage_roll_back_together(sql, monkeypatch):
	claimed(sql)
	attempt = ai_jobs._transaction(
		lambda: store.begin_attempt(
			"run-a", worker_token="worker-a", kind="fix", target="finding-a", input_hash="fake", now=12
		)
	)

	def fail(*a):
		raise RuntimeError("fake accounting unavailable")

	with monkeypatch.context() as patch:
		patch.setattr("optimus.analyze._add_ai_spend", fail)
		with pytest.raises(RuntimeError):
			ai_jobs._transaction(
				lambda: store.settle_attempt(
					"run-a",
					attempt["name"],
					worker_token="worker-a",
					outcome="succeeded",
					tokens=7,
					usage_complete=True,
					now=13,
					persist=lambda: sql.primary.set_value(
						"Optimus Session", "parent-a", {"answer": "fake answer"}
					),
				)
			)
	assert sql.primary.get_value("Optimus Session", "parent-a", "answer") is None
	assert sql.primary.get_value("Optimus Session", "parent-a", "ai_tokens_spent") == 0
	assert sql.primary.get_value(store.ATTEMPT, attempt["name"], "state") == "calling"


def test_duplicate_settlement_and_large_totals_use_one_atomic_increment(sql):
	claimed(sql)
	for i in range(2):
		attempt = ai_jobs._transaction(
			lambda: store.begin_attempt(
				"run-a", worker_token="worker-a", kind="fix", target=f"finding-{i}", input_hash="fake", now=12
			)
		)
		for _ in range(2):
			ai_jobs._settle(
				lambda: store.settle_attempt(
					"run-a",
					attempt["name"],
					worker_token="worker-a",
					outcome="succeeded",
					tokens=4_000_000_000,
					usage_complete=True,
					now=13,
				)
			)
	assert sql.primary.get_value("Optimus Session", "parent-a", "ai_tokens_spent") == 8_000_000_000
	assert sql.primary.get_value(store.RUN, "run-a", "completed") == 2


def test_completion_and_nullable_reservation_allow_a_later_run(sql):
	claimed(sql)
	ai_jobs._transaction(lambda: store.cancel("run-a", now=12))
	assert ai_jobs._retry_sql(lambda: admit("run-b"))["status"] == "queued"
	assert sql.primary.get_value(store.RUN, "run-a", "active_session") is None
	assert sql.primary.get_value(store.RUN, "run-b", "active_session") == "parent-a"


def test_busy_admission_mutex_fails_promptly_then_recovers(sql):
	contender = sql.connect()
	sql.primary.get_value(store.CONTROL, "site", for_update=True)
	started = time.monotonic()
	with sql.using(contender), pytest.raises(Exception) as error:
		ai_jobs._retry_sql(admit)
	assert time.monotonic() - started < 1
	assert getattr(error.value, "pgcode", None) == "55P03" or error.value.args[0] == 1205
	assert len(contender.conflicts) == 3
	sql.primary.connection.commit()
	with sql.using(contender):
		assert ai_jobs._retry_sql(admit)["status"] == "queued"


def test_completion_counter_failure_cannot_release_the_reservation(sql, monkeypatch):
	claimed(sql)
	sql.primary.set_value(store.RUN, "run-a", {"completed": 1})
	sql.primary.connection.commit()

	def fail(name):
		sql.primary.increment(name, "ai_refresh_count", 1)
		raise RuntimeError("fake interrupted completion")

	with monkeypatch.context() as patch:
		patch.setattr("optimus.analyze._bump_ai_refresh_count", fail)
		with pytest.raises(RuntimeError):
			ai_jobs._transaction(
				lambda: store.finish("run-a", worker_token="worker-a", now=12, reason="complete")
			)
	assert sql.primary.get_value(store.RUN, "run-a", "active_session") == "parent-a"
	assert sql.primary.get_value("Optimus Session", "parent-a", "ai_refresh_count") == 0
	for _ in range(2):
		ai_jobs._transaction(
			lambda: store.finish("run-a", worker_token="worker-a", now=12, reason="complete")
		)
	assert sql.primary.get_value("Optimus Session", "parent-a", "ai_refresh_count") == 1
	assert sql.primary.get_value(store.RUN, "run-a", "active_session") is None



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
	if sql.backend == "postgres":
		assert "40001" in older.conflicts


def test_phase2_answer_and_ready_outcome_roll_back_together(sql):
	phase2_admit(sql)
	run = phase2_claim()
	def fail():
		sql.primary.set_value("Optimus Session", "parent-a", {"answer": "fake phase2 answer"})
		raise RuntimeError("fake save failure")
	with pytest.raises(RuntimeError):
		ai_jobs._transaction(lambda: phase2._complete(run, now=102, persist=fail))
	assert sql.primary.get_value("Optimus Session", "parent-a", "answer") is None
	assert sql.primary.get_value(phase2.TABLE, "fake-child", "status") == "Analyzing"


def test_phase2_ambiguous_commit_replay_does_not_append_twice(sql):
	phase2_admit(sql)
	run = phase2_claim()
	def persist():
		sql.primary.increment("parent-a", "ai_refresh_count", 1)  # stand-in append count
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


def test_provider_settings_and_credential_share_a_sql_snapshot(sql, monkeypatch):
	"""The production SELECT cannot pair an old endpoint with a new password."""
	import frappe

	primary, writer = sql.primary, sql.connect()
	names = {"tabSingles": primary.tables["Optimus Session"] + "_singles",
		"__Auth": primary.tables["Optimus Session"] + "_auth"}
	created = []
	queries = []
	def read(queries_by_backend, values):
		query = queries_by_backend[sql.backend]
		for original, replacement in names.items():
			query = query.replace(primary.quoted(original), primary.quoted(replacement))
		queries.append(query)
		return [tuple(row.values()) for row in primary.execute(query, values)]
	try:
		for original, columns in (("tabSingles", "doctype VARCHAR(140), field VARCHAR(140), value TEXT"),
			("__Auth", "doctype VARCHAR(140), name VARCHAR(140), fieldname VARCHAR(140), password TEXT, encrypted INTEGER")):
			primary.execute("CREATE TABLE " + primary.quoted(names[original]) + " (" + columns + ")"
				+ (" ENGINE=InnoDB" if sql.backend == "mariadb" else ""))
			created.append(names[original])
		for field, value in {"ai_enabled": "1", "ai_provider": "OpenAI-compatible", "ai_base_url": "https://first.invalid/v1", "ai_model": "fake"}.items():
			primary.execute("INSERT INTO " + primary.quoted(names["tabSingles"]) + " VALUES (%s, %s, %s)", ("Optimus Settings", field, value))
		primary.execute("INSERT INTO " + primary.quoted(names["__Auth"]) + " VALUES (%s, %s, %s, %s, %s)",
			("Optimus Settings", "Optimus Settings", "ai_api_key", "first encrypted value", 1))
		primary.connection.commit()

		monkeypatch.setattr(frappe, "db", SimpleNamespace(multisql=read), raising=False)
		before, ciphertext = ai_fix._read_provider_snapshot()
		assert (before.ai_base_url, ciphertext) == ("https://first.invalid/v1", "first encrypted value")
		writer.execute("UPDATE " + writer.quoted(names["tabSingles"]) + " SET value=%s WHERE field=%s",
			("https://second.invalid/v1", "ai_base_url"))
		writer.execute("UPDATE " + writer.quoted(names["__Auth"]) + " SET password=%s", ("second encrypted value",))
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
			primary.execute("DROP TABLE " + primary.quoted(name))
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
	if sql.backend == "postgres":
		assert "40001" in older.conflicts


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
	claimed(sql)
	attempt = ai_jobs._retry_sql(lambda: store.begin_attempt("run-a", worker_token="worker-a",
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
	assert not ai_jobs._retry_sql(lambda: store.settle_attempt("run-a", attempt["name"],
		worker_token="worker-a", now=13, outcome="succeeded", tokens=100, usage_complete=True,
		persist=lambda: pytest.fail("late result persisted")))
