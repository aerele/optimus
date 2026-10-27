# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Reload the Optimus Session DocType so existing sites get the new form layout."""

import frappe


def execute():
	frappe.reload_doc("optimus", "doctype", "optimus_session")
	frappe.clear_cache(doctype="Optimus Session")
