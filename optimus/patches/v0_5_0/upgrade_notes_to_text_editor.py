# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Upgrade Optimus Session.notes from Text to Text Editor.

Reloads the doctype definition so existing installs see the new fieldtype and
label. Existing plain-text note values are valid Text Editor input, so no data
migration is needed (the DB column stays, only the metadata changes).
"""

import frappe


def execute():
	frappe.reload_doc("optimus", "doctype", "optimus_session")
	frappe.clear_cache(doctype="Optimus Session")
