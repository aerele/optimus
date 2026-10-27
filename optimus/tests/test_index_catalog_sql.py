"""Real catalog evidence must not turn unusable indexes into coverage proofs."""

import json
import os
import uuid
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("v15_like", [False, True], ids=["v16", "v15"])
def test_purge_confirms_exact_paths_on_real_like_candidates(catalog, monkeypatch, v15_like):
	from optimus import maintenance

	c = catalog
	tables = {"Error Log": c.table + "_logs", "Deleted Document": c.table + "_deleted"}
	allowed = {"name", "error", "data", "deleted_doctype"}
	def quote(field):
		assert field in allowed or field in tables.values()
		return c.quote + field + c.quote

	candidates = set()
	def get_all(doctype, *, filters=(), or_filters=(), fields, limit_page_length, limit_start=0, order_by):
		assert order_by == "name asc"
		params = []
		def condition(item):
			field, op, value = item
			assert op in {"=", ">", "<=", "like"}
			if op == "like":
				if v15_like:
					value = value.replace("\\", "\\\\")
				if c.backend == "postgres":
					op = "ilike"  # Frappe's Postgres LIKE translation.
			params.append(value)
			return f"{quote(field)} {op} %s"
		clauses = [condition(f) for f in filters]
		if or_filters:
			clauses.append("(" + " OR ".join(condition(f) for f in or_filters) + ")")
		rows = c.sql(
			f"SELECT {','.join(quote(f) for f in fields)} FROM {quote(tables[doctype])}"
			+ (" WHERE " + " AND ".join(clauses) if clauses else "")
			+ f" ORDER BY {quote('name')} LIMIT %s OFFSET %s",
			[*params, limit_page_length, limit_start], as_dict=True,
		)
		if or_filters:
			candidates.update((doctype, r["name"]) for r in rows)
		return rows

	def delete(doctype, filters):
		op, names = filters["name"]
		assert op == "in" and names
		c.sql(
			f"DELETE FROM {quote(tables[doctype])} WHERE {quote('name')} IN ({','.join(['%s'] * len(names))})",
			names,
		)

	created = []
	try:
		for table in tables.values():
			c.sql(
				f"CREATE TABLE {quote(table)} (name varchar(50) PRIMARY KEY, error text, data text, deleted_doctype varchar(50))"
				+ (" CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci" if c.backend == "mariadb" else "")
			)
			created.append(table)
		paths = ["optimus/aiXfix.py", "frappeXprofiler/ai_fix.py", "optimus/AI_FIX.PY",
			"optimus/ai_fix.py", "frappe_profiler/ai_fix.py"]
		for table in tables.values():
			for i, path in enumerate(paths):
				text = f'File "apps/{path}", line 10'
				c.sql(f"INSERT INTO {quote(table)} VALUES (%s,%s,%s,%s)",
					[str(i), text, json.dumps({"error": text}), "Error Log"])
			c.sql(f"INSERT INTO {quote(table)} VALUES (%s,%s,%s,%s)", ["other", "", "optimus/ai_fix.py", "Note"])
		monkeypatch.setattr(maintenance, "frappe", SimpleNamespace(get_all=get_all, db=SimpleNamespace(delete=delete)))
		monkeypatch.setattr(maintenance, "safe_commit", c.connection.commit)
		monkeypatch.setattr(maintenance, "_BATCH", 2)
		monkeypatch.setattr(maintenance, "_WINDOW", 3)
		assert maintenance.purge_ai_error_logs(dry_run=True) == {"error_logs": 2, "deleted_documents": 2}
		assert candidates == {(doctype, str(i)) for doctype in tables for i in range(5)}
		assert maintenance.purge_ai_error_logs(dry_run=False) == {"error_logs": 2, "deleted_documents": 2}
		for table in tables.values():
			assert {r[0] for r in c.sql(f"SELECT name FROM {quote(table)}")} == {"0", "1", "2", "other"}
	finally:
		c.connection.rollback()
		for table in reversed(created):
			c.sql(f"DROP TABLE {quote(table)}")


@pytest.fixture
def catalog(monkeypatch):
	backend = os.environ.get("OPTIMUS_TEST_SQL")
	if backend not in {"postgres", "mariadb"}:
		pytest.skip("Requires the disposable SQL validation service")
	if backend == "postgres":
		import psycopg2

		from optimus.dbdialect.postgres import PostgresDialect

		connection = psycopg2.connect(
			host="127.0.0.1",
			port=int(os.environ.get("OPTIMUS_TEST_SQL_PORT", 5432)),
			user="optimus_test",
			password="optimus_test",
			dbname="optimus_test",
		)
		connection.autocommit = True
		dialect, quote = PostgresDialect(), '"'
	else:
		import pymysql

		from optimus.dbdialect.mariadb import MariaDBDialect

		connection = pymysql.connect(
			host="127.0.0.1",
			port=int(os.environ.get("OPTIMUS_TEST_SQL_PORT", 3306)),
			user="root",
			password="optimus_test",
			database="optimus_test",
			autocommit=True,
		)
		dialect, quote = MariaDBDialect(), "`"
	table = "tabOptimusProof_" + uuid.uuid4().hex[:12]

	def sql(query, params=None, as_dict=False):
		with connection.cursor() as cursor:
			cursor.execute(query, params)
			if not cursor.description:
				return []
			rows = cursor.fetchall()
			return (
				[dict(zip([col[0] for col in cursor.description], row, strict=True)) for row in rows]
				if as_dict
				else rows
			)

	import frappe

	monkeypatch.setattr(
		frappe,
		"db",
		SimpleNamespace(
			sql=sql,
			savepoint=lambda name: sql("SAVEPOINT " + name),
			rollback=lambda *, save_point: sql("ROLLBACK TO SAVEPOINT " + save_point),
			release_savepoint=lambda name: sql("RELEASE SAVEPOINT " + name),
		),
		raising=False,
	)
	monkeypatch.setattr(frappe, "conf", {"db_schema": "public"}, raising=False)
	sql(
		f"CREATE TABLE {quote}{table}{quote} (name varchar(50) PRIMARY KEY, customer varchar(100), status varchar(30))"
	)
	sql(f"INSERT INTO {quote}{table}{quote} VALUES ('a','same','Draft'), ('b','same','Submitted')")
	try:
		yield SimpleNamespace(
			backend=backend, sql=sql, table=table, quote=quote, dialect=dialect, connection=connection
		)
	finally:
		connection.rollback()
		if backend == "postgres":
			connection.autocommit = True
		sql(f"DROP TABLE IF EXISTS {quote}{table}{quote}")
		connection.close()


def test_unsupported_indexes_cannot_prove_full_column_coverage(catalog):
	c = catalog
	table = f"{c.quote}{c.table}{c.quote}"
	if c.backend == "postgres":
		c.sql(f"CREATE INDEX \"{c.table}_partial\" ON {table}(customer) WHERE status='Draft'")
		c.sql(f'CREATE INDEX "{c.table}_expression" ON {table}(lower(status), customer)')
		c.sql(f'CREATE INDEX "{c.table}_include" ON {table}(customer) INCLUDE(status)')
		c.sql(f'CREATE INDEX "{c.table}_hash" ON {table} USING hash(customer)')
		c.sql(f'CREATE INDEX "{c.table}_gin" ON {table} USING gin(to_tsvector(\'simple\', status))')
		with pytest.raises(Exception):
			c.sql(f'CREATE UNIQUE INDEX CONCURRENTLY "{c.table}_invalid" ON {table}(customer)')
		c.connection.autocommit = False
	else:
		c.sql(f"CREATE INDEX prefix_customer ON {table}(customer(5))")
		c.sql(f"CREATE FULLTEXT INDEX text_status ON {table}(status)")
		c.sql(f"CREATE INDEX ignored_customer ON {table}(customer) IGNORED")
	indexes = {ix.name: ix for ix in c.dialect.existing_indexes(c.table)}
	if c.backend == "postgres":
		assert not indexes[c.table + "_partial"].eligible
		assert not indexes[c.table + "_expression"].eligible
		assert indexes[c.table + "_expression"].columns == [None, "customer"]
		assert not indexes[c.table + "_invalid"].eligible
		assert indexes[c.table + "_include"].columns == ["customer"]
		assert indexes[c.table + "_include"].eligible
		assert not indexes[c.table + "_hash"].eligible
		assert not indexes[c.table + "_gin"].eligible
	else:
		assert all(
			not indexes[name].eligible for name in {"prefix_customer", "text_status", "ignored_customer"}
		)
	assert any(ix.unique and ix.columns == ["name"] for ix in indexes.values())


def test_composite_prefix_keeps_full_leading_column_coverage(catalog, monkeypatch):
	if catalog.backend != 'mariadb':
		pytest.skip('MariaDB prefix key parts')
	from optimus.analyzers import index_suggestions

	catalog.sql(f'CREATE INDEX customer_status_prefix ON `{catalog.table}` (customer, status(5))')
	index = next(ix for ix in catalog.dialect.existing_indexes(catalog.table) if ix.name == 'customer_status_prefix')
	assert not index.eligible and index.leftmost == 'customer'
	assert index.usable_prefix == ('customer',)
	monkeypatch.setattr(index_suggestions, 'get_dialect', lambda: catalog.dialect)
	assert 'customer' in index_suggestions._get_indexed_columns(catalog.table)
