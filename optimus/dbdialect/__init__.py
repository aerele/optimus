# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Dialect factory.

``get_dialect()`` returns the adapter for the active ``frappe.db.db_type``
(mariadb / postgres). It is memoized on the db_type *string* (not on the
``frappe.db`` object) so it survives the per-test ``frappe.db`` proxy swaps
(frappe.db is a Werkzeug Local proxy) — we read the string fresh each call and
only build a new adapter when the db_type changes.
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
]

_cache: dict = {}


def active_db_type() -> str:
	"""The active database type, lowercased: 'mariadb' | 'postgres'. Defaults
	to 'mariadb' when it can't be determined (matches Frappe's own default)."""
	import frappe

	# frappe.db is a Werkzeug Local proxy: reading .db_type when no DB is bound
	# (unit tests) raises RuntimeError("object is not bound"), which getattr's
	# default does NOT catch — so guard explicitly.
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
	return (dbt or "mariadb").lower()


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
					message=f"db_type={dbt!r} not recognized — using the MariaDB dialect",
				)
			except Exception:
				pass
		from optimus.dbdialect.mariadb import MariaDBDialect

		dialect = MariaDBDialect()

	_cache[dbt] = dialect
	return dialect
