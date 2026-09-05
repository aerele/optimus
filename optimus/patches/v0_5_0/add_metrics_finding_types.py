# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Reload the Optimus Finding doctype so its finding_type Select picks up the
seven metrics types (Resource Contention, Memory Pressure, DB Pool Saturation,
Background Queue Backlog, Slow Frontend Render, Network Overhead, Heavy
Response) from disk on existing installs. Fresh installs get them automatically.
"""

import frappe


def execute():
	frappe.reload_doc("optimus", "doctype", "optimus_finding")
	frappe.clear_cache(doctype="Optimus Finding")
