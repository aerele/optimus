# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Seed the SQL admission mutex and make sure the journal's indexes exist.

Both steps are idempotent. They run as a patch on an upgrade, from ``after_install`` on a
fresh site, and from ``after_migrate`` on every migrate (see ``optimus.install``).
"""

import re

import frappe

JOURNAL_DOCTYPES = ("Optimus AI Refresh Run", "Optimus AI Refresh Attempt")
# The Phase Two Run table is scanned by status on every scheduler tick. Its ``status``
# is declared with search_index like the journals' columns, so Frappe's schema sync keeps
# the index; it shares their PostgreSQL name-collision repair below.
INDEXED_DOCTYPES = (*JOURNAL_DOCTYPES, "Optimus Phase Two Run")


def execute():
	"""Never raise: a failure here would block ``bench migrate``. It is logged once and
	the next migrate retries (``after_migrate`` runs the same steps); admission fails
	closed meanwhile."""
	for title, step in (
		("optimus ai refresh store seed", seed_control),
		("optimus ai refresh store indexes", ensure_indexes),
	):
		try:
			step()
		except Exception:
			from optimus import safe_call

			try:
				frappe.db.rollback()
			except Exception:
				pass
			safe_call.log_hook_error(title)


def seed_control():
	if not frappe.db.exists("Optimus AI Refresh Control", "site"):
		frappe.get_doc({"doctype": "Optimus AI Refresh Control", "scope": "site"}).insert(
			ignore_permissions=True
		)


def ensure_indexes():
	"""Give every indexed column of the journal tables an index of its own on PostgreSQL.

	Frappe names a PostgreSQL index after its column alone (``CREATE INDEX IF NOT EXISTS
	"session_name"``), and PostgreSQL index names are unique per schema. The Run and the
	Attempt table both index ``session_name``, so the second table to be created silently
	got no index (a NOTICE, not an error), and every read by session scanned it. Any other
	app's table that indexes a column of the same name would do the same.

	The missing index is created here under a name that includes the table:
	``<table>_<column>_index``. It is created only when no index covers the column, so the
	table that did get Frappe's index gets no second one. Frappe's later schema syncs leave
	the extra index alone: it sees an index on the column and has nothing to add. MariaDB
	names an index per table, so it has no collision and nothing is done there.

	Frappe v15's PostgreSQL database has no ``db_schema``: the schema is then read from the
	site config, as Frappe v15 reads it, and is "public" when the config names none."""
	if frappe.db.db_type != "postgres":
		return
	schema = getattr(frappe.db, "db_schema", None) or frappe.conf.get("db_schema") or "public"
	for doctype in INDEXED_DOCTYPES:
		table = "tab" + doctype
		if not frappe.db.table_exists(doctype):
			continue
		columns = [
			field.fieldname
			for field in frappe.get_meta(doctype).fields
			if field.search_index and not field.unique
		]
		definitions = [
			row[0]
			for row in frappe.db.sql(
				"SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = %s",
				(schema, table),
			)
		]
		for column in columns:
			if any(
				re.search(rf'\("?{re.escape(column)}"?\)$', definition.strip()) for definition in definitions
			):
				continue
			frappe.db.add_index(doctype, [column], index_name=f"{table}_{column}_index")

