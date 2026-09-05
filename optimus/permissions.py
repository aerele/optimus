# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Server-side permission gates for profiler artifacts.

Exposes one gate: a ``has_permission`` for the File DocType that
double-checks downloads of the profiler report (raw_report_file +
raw_report_pdf_file), so a user who guesses the file URL can't fetch it
directly. Admin/owner-scoped.
"""

import frappe

PROFILER_SESSION_DOCTYPE = "Optimus Session"
_GATED_FIELDS = frozenset({"raw_report_file", "raw_report_pdf_file"})


def file_has_permission(doc, ptype=None, user=None):
	"""Gate downloads of the profiler report (HTML + PDF).

	On top of Frappe's standard parent permission check, restricts the report
	files to the System Manager or the recording user (even if another role
	got read access to the parent). Returns None to defer to standard checks,
	False to deny.
	"""
	if not doc:
		return None

	# Only intercept files attached to a Optimus Session.
	if doc.attached_to_doctype != PROFILER_SESSION_DOCTYPE:
		return None

	# Only intercept the report files (HTML + lazy PDF).
	if doc.attached_to_field not in _GATED_FIELDS:
		return None

	user = user or frappe.session.user
	roles = frappe.get_roles(user)

	if "System Manager" in roles or "Administrator" in roles:
		return None  # defer to standard checks

	# Otherwise the user must be the recording user
	if not doc.attached_to_name:
		return False
	recording_user = frappe.db.get_value(
		PROFILER_SESSION_DOCTYPE,
		doc.attached_to_name,
		"user",
	)
	if recording_user != user:
		return False

	return None  # passed our gate; let standard checks run
