"""Resolve generated files by their private, exact parent/field binding."""

import frappe
from frappe import _

from optimus.managed import ARTIFACT_FIELDS, SESSION


def artifact_file(docname, field, url):
	if field not in ARTIFACT_FIELDS or not url:
		return None
	name = frappe.db.get_value(
		"File",
		{
			"file_url": url,
			"attached_to_doctype": SESSION,
			"attached_to_name": docname,
			"attached_to_field": field,
			"is_private": 1,
		},
		"name",
	)
	return frappe.get_doc("File", name) if name else None


def read_artifact(docname, field, url):
	file = artifact_file(docname, field, url)
	if file:
		try:
			return file.get_content()
		except (FileNotFoundError, OSError):
			pass
	frappe.throw(_("The session artifact is missing. Regenerate the report or retry analysis."))
