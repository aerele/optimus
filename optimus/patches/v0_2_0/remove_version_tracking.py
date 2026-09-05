# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Delete tabVersion rows for Optimus Session.

Optimus Session no longer tracks changes (track_changes=0), but earlier
sessions each wrote 10+ tabVersion rows per analyze, of no audit value. This
patch deletes those existing rows on bench migrate. Safe to run repeatedly (the
DELETE is idempotent).
"""

import frappe


def execute():
	if not frappe.db.table_exists("tabVersion"):
		return

	# Count before deletion for the log. Portable ORM (no raw SQL/backticks).
	count = frappe.db.count("Version", {"ref_doctype": "Optimus Session"})

	if not count:
		return

	frappe.db.delete("Version", {"ref_doctype": "Optimus Session"})
	frappe.db.commit()

	try:
		frappe.logger().info(
			f"optimus patch v0_2_0 removed {count} Optimus Session version rows"
		)
	except Exception:
		pass
