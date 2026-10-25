# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Add the local-CLI AI subscription fields (additive).

Reloads Optimus Settings so bench migrate adds the ai_connection_type,
ai_subscription_cli, ai_cli_path and ai_subscription_status columns
deterministically during the patch run. Idempotent.
"""

import frappe


def execute():
	try:
		frappe.reload_doc("optimus", "doctype", "optimus_settings")
	except Exception:
		frappe.log_error(title="v0.13.0 patch: reload optimus_settings (add subscription CLI fields)")
	frappe.db.commit()
