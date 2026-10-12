# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Seed the SQL admission mutex and make sure the journal's indexes exist.

Both steps are idempotent. They run as a patch on an upgrade, from ``after_install`` on a
fresh site, and from ``after_migrate`` on every migrate (see ``optimus.install``).
"""

import re

import frappe

JOURNAL_DOCTYPES = ("Optimus AI Refresh Run", "Optimus AI Refresh Attempt")


def execute():
	if not frappe.db.exists("Optimus AI Refresh Control", "site"):
		frappe.get_doc({"doctype": "Optimus AI Refresh Control", "scope": "site"}).insert(
			ignore_permissions=True
		)
	ensure_indexes()


def ensure_indexes():
	"""Give every indexed column of the journal tables an index of its own on PostgreSQL (D2).

	Frappe names a PostgreSQL index after its column alone (``CREATE INDEX IF NOT EXISTS
	"session_name"``), and PostgreSQL index names are unique per schema. The Run and the
	Attempt table both index ``session_name``, so the second table to be created silently
	got no index (a NOTICE, not an error), and every read by session scanned it. Any other
	app's table that indexes a column of the same name would do the same.

	The missing index is created here under a name that includes the table:
	``<table>_<column>_index``. It is created only when no index covers the column, so the
	table that did get Frappe's index gets no second one. Frappe's later schema syncs leave
	the extra index alone: it sees an index on the column and has nothing to add. MariaDB
	names an index per table, so it has no collision and nothing is done there."""
	if frappe.db.db_type != "postgres":
		return
	for doctype in JOURNAL_DOCTYPES:
		table = "tab" + doctype
		columns = [
			field.fieldname
			for field in frappe.get_meta(doctype).fields
			if field.search_index and not field.unique
		]
		definitions = [
			row[0]
			for row in frappe.db.sql(
				"SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = %s",
				(frappe.db.db_schema, table),
			)
		]
		for column in columns:
			if any(
				re.search(rf'\("?{re.escape(column)}"?\)$', definition.strip()) for definition in definitions
			):
				continue
			frappe.db.add_index(doctype, [column], index_name=f"{table}_{column}_index")
