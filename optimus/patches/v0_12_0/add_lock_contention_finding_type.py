# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Reload Optimus Finding so existing installs get the "Lock Contention" type."""

import frappe


def execute():
	frappe.reload_doc("optimus", "doctype", "optimus_finding")
	frappe.clear_cache(doctype="Optimus Finding")
