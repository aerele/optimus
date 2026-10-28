# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Make sure the local-CLI AI subscription fields exist on Optimus Settings.

Optimus Settings is a Single doctype, so its fields have no table columns. This is a
defensive, idempotent ``reload_doc`` that re-syncs the doctype meta (ai_connection_type,
ai_subscription_cli, ai_cli_path, ai_subscription_status) during the patch run.
"""

import frappe


def execute():
	try:
		frappe.reload_doc("optimus", "doctype", "optimus_settings")
	except Exception:
		frappe.log_error(title="v0.13.0 patch: reload optimus_settings (add subscription CLI fields)")
	frappe.db.commit()
