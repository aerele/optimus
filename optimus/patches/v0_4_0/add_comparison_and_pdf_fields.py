# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Add comparison and PDF fields to Optimus Session.

Three new fields (all nullable / default 0):
  - compared_to_session (Link): baseline pointer for comparison rendering
  - is_baseline (Check): flag for sessions pinned as baseline
  - safe_report_pdf_file (Attach): lazy-generated PDF cache
"""

import frappe


def execute():
	frappe.reload_doc("optimus", "doctype", "optimus_session")
