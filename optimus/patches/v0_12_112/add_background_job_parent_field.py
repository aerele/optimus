# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Add parent_recording_uuid to Optimus Background Job."""

import frappe


def execute():
	frappe.reload_doc("optimus", "doctype", "optimus_background_job")
