# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Bump ``redundant_cache_threshold`` on Optimus Settings from 10 to 50.

Cache lookups aren't individually timed, so Redundant Cache findings report
0ms impact; at threshold 10 they flooded the findings list. Conservative:
only when the Single exists, only when the value is still exactly the old
default of 10 (never overwrites a tuned value), then invalidates the settings
cache so the change takes effect without a restart.
"""

import frappe

OLD_DEFAULT = 10
NEW_DEFAULT = 50


def execute():
	if not frappe.db.exists("DocType", "Optimus Settings"):
		return
	try:
		current = frappe.db.get_single_value(
			"Optimus Settings", "redundant_cache_threshold"
		)
	except Exception:
		return

	# Only flip the exact old default respect any deliberate tuning.
	try:
		current_int = int(current) if current is not None else None
	except (TypeError, ValueError):
		return

	if current_int != OLD_DEFAULT:
		return

	frappe.db.set_single_value(
		"Optimus Settings", "redundant_cache_threshold", NEW_DEFAULT
	)
	# Blow the settings cache so analyzers pick up the new value
	# without waiting for a bench restart.
	try:
		frappe.cache.delete_value("optimus_settings_cached")
	except Exception:
		pass
	frappe.db.commit()
