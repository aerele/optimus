"""Real catalog evidence must not turn unusable indexes into coverage proofs."""

import os
import uuid
from types import SimpleNamespace

import pytest


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
