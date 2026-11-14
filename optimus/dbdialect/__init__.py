# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Dialect factory.

``get_dialect()`` returns the adapter for the active ``frappe.db.db_type``
(mariadb / postgres), memoized on the db_type string (not the ``frappe.db``
object) so it survives per-test ``frappe.db`` proxy swaps: the string is read
fresh each call and a new adapter is built only when the db_type changes.
"""

from __future__ import annotations

from optimus.dbdialect.base import (
	Dialect,
	IndexInfo,
	InfraSnapshot,
	NormalizedPlan,
	PlanTable,
	to_float,
	to_int,
)

__all__ = [
	"Dialect",
	"NormalizedPlan",
	"PlanTable",
	"IndexInfo",
	"InfraSnapshot",
	"to_int",
	"to_float",
	"get_dialect",
	"active_db_type",
	"redaction_dialect",
]

_cache: dict = {}


def active_db_type() -> str:
	"""The active database type, lowercased: 'mariadb' | 'postgres'. Defaults
	to 'mariadb' when it can't be determined (matches Frappe's own default)."""
	return _configured_db_type() or "mariadb"


def redaction_dialect() -> str | None:
	"""'mariadb' or 'postgres' when the site says so, else None. Unlike
	``active_db_type`` it never guesses: SQL redaction reads an unknown dialect
	both ways instead of trusting a default."""
	db_type = _configured_db_type()
	return db_type if db_type in ("mariadb", "postgres") else None


def _configured_db_type() -> str | None:
	import frappe

	# frappe.db is a Werkzeug Local proxy: reading .db_type when no DB is bound
	# (unit tests) raises RuntimeError("object is not bound"), which getattr's
	# default does NOT catch so guard explicitly.
	dbt = None
	try:
		dbt = getattr(frappe.db, "db_type", None)
	except Exception:
		dbt = None
	if not dbt:
		try:
			dbt = frappe.conf.get("db_type")
		except Exception:
			dbt = None
	return dbt.lower() if dbt else None


def get_dialect() -> Dialect:
	"""Return the Dialect adapter for the active database."""
	dbt = active_db_type()
	cached = _cache.get(dbt)
	if cached is not None:
		return cached

	if dbt == "postgres":
		from optimus.dbdialect.postgres import PostgresDialect

		dialect: Dialect = PostgresDialect()
	else:
		if dbt != "mariadb":
			try:
				import frappe

				frappe.log_error(
					title="optimus unknown db_type",
					message=f"db_type={dbt!r} not recognized using the MariaDB dialect",
				)
			except Exception:
				pass
		from optimus.dbdialect.mariadb import MariaDBDialect

		dialect = MariaDBDialect()

	_cache[dbt] = dialect
	return dialect
