# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Reload the Optimus Finding doctype so its finding_type Select picks up the
new "Lock Contention" option from disk on existing installs. Without the reload,
analyze fails Select validation when it writes a Lock Contention finding row.
Fresh installs get the option automatically.
"""

import frappe


def execute():
	frappe.reload_doc("optimus", "doctype", "optimus_finding")
	frappe.clear_cache(doctype="Optimus Finding")
