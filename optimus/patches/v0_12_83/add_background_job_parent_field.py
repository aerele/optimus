# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Add parent_recording_uuid to Optimus Background Job."""

import frappe


def execute():
	try:
		frappe.reload_doc("optimus", "doctype", "optimus_background_job")
	except Exception:
		frappe.log_error(title="optimus patch: reload optimus_background_job")
