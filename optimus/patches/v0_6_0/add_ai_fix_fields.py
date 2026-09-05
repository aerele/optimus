# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Add the AI "suggest a fix" fields (additive).

Reloads Optimus Settings (ai_section + ai_enabled / ai_provider / ai_base_url /
ai_model / ai_api_key) and Optimus Finding (llm_fix_json) so ``bench migrate``
adds their columns deterministically during the patch run. Idempotent.
"""

import frappe


def execute():
	for doctype in ("optimus_settings", "optimus_finding"):
		try:
			frappe.reload_doc("optimus", "doctype", doctype)
		except Exception:
			frappe.log_error(title=f"v0.6.0 patch: reload {doctype} (add AI fix fields)")
	frappe.db.commit()
