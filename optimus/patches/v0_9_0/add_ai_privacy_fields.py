# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Register the AI privacy-hardening fields on Optimus Settings.

Additive only: ai_privacy_section, ai_excluded_finding_types (Small Text) and
ai_request_timeout_seconds (Int). Reloads the DocType deterministically during
the patch run. Idempotent. See docs/AI-FIXING.md for the data-flow inventory.
"""

import frappe


def execute():
	try:
		frappe.reload_doc("optimus", "doctype", "optimus_settings")
	except Exception:
		frappe.log_error(
			title="v0.9.0 patch: reload optimus_settings (add AI privacy fields)"
		)
	frappe.db.commit()
