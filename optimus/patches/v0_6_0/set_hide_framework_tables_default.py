# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Turn ON ``hide_framework_tables`` for existing Optimus Settings rows stored
as 0 / None.

Only runs if the Optimus Settings Single exists; only flips a falsy stored
value to 1 (a truthy value is left alone). Caveat: an admin who deliberately
unchecked the box (stored 0) also gets flipped to 1; the report footer stamps
the current value so they can re-uncheck. Invalidates the settings cache so the
next request sees the new value without a bench restart.
"""

import frappe


def execute():
	if not frappe.db.exists("DocType", "Optimus Settings"):
		return
	try:
		current = frappe.db.get_single_value(
			"Optimus Settings", "hide_framework_tables"
		)
	except Exception:
		return

	# Coerce: a stored "0" / 0 / None / "" all read as falsy. Anything
	# truthy (1, "1", True) is left alone.
	if current:
		return

	try:
		frappe.db.set_single_value(
			"Optimus Settings", "hide_framework_tables", 1
		)
	except Exception:
		return

	try:
		frappe.cache.delete_value("optimus_settings_cached")
	except Exception:
		pass
	frappe.db.commit()
