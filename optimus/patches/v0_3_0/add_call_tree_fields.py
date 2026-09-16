# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Reload Optimus Action / Session / Finding DocTypes for the call-tree fields.

Reloads three DocTypes so their new schema applies:
  - optimus_action: call_tree_json, call_tree_size_bytes, call_tree_overflow_file
  - optimus_session: total_python_ms, total_sql_ms, hot_frames_json, session_time_breakdown_json
  - optimus_finding: adds Slow Hot Path, Hook Bottleneck, Repeated Hot Frame and
    Redundant Call to the finding_type Select (otherwise analyze fails Select
    validation in _persist()).

The new columns are nullable; existing rows get NULL and render via the
renderer's fallbacks.
"""

import frappe


def execute():
	frappe.reload_doc("optimus", "doctype", "optimus_action")
	frappe.reload_doc("optimus", "doctype", "optimus_session")
	frappe.reload_doc("optimus", "doctype", "optimus_finding")
