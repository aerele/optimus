# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Seed the SQL admission mutex after the journal DocTypes are installed."""

import frappe


def execute():
	if not frappe.db.exists("Optimus AI Refresh Control", "site"):
		frappe.get_doc({"doctype": "Optimus AI Refresh Control", "scope": "site"}).insert(
			ignore_permissions=True
		)
