"""Capabilities for the profiler's own document and generated-artifact writers.

These capabilities are process-local context, never document flags supplied by
REST clients. Normal Session saves may edit title and notes only.
"""

from contextlib import contextmanager
from contextvars import ContextVar

import frappe
from frappe import _

SESSION = "Optimus Session"
ARTIFACT_FIELDS = frozenset({"raw_report_file", "raw_report_pdf_file", "recordings_file"})
_session_writers = ContextVar("optimus_session_writers", default=())
_artifact_writers = ContextVar("optimus_artifact_writers", default=())
_IDENTITY_FIELDS = frozenset({"session_uuid", "user", "owner", "status"}) | ARTIFACT_FIELDS


@contextmanager
def session_writer(doc):
	token = _session_writers.set((*_session_writers.get(), doc))
	try:
		yield
	finally:
		_session_writers.reset(token)


@contextmanager
def artifact_writer(docname, field):
	if field not in ARTIFACT_FIELDS or not docname:
		raise ValueError("Invalid profiler artifact capability")
	token = _artifact_writers.set((*_artifact_writers.get(), (docname, field)))
	try:
		yield
	finally:
		_artifact_writers.reset(token)


def save_session(doc, *, insert=False):
	with session_writer(doc):
		return doc.insert(ignore_permissions=True) if insert else doc.save(ignore_permissions=True)


def _deny():
	frappe.throw(
		_("Profiling data is managed by Optimus. Edit only the session title and notes."),
		frappe.ValidationError,
	)


def protect_session(doc, method=None):
	if any(parent is doc for parent in _session_writers.get()):
		return
	if doc.is_new():
		_deny()
	previous = doc.get_doc_before_save()
	if not previous:
		_deny()
	for field in _IDENTITY_FIELDS:
		if str(doc.get(field) or "") != str(previous.get(field) or ""):
			_deny()
	# Restore server-owned values instead of comparing serialized floats,
	# datetimes and child rows from a full Desk save to native DB values.
	for field in previous.as_dict():
		if field not in {"doctype", "title", "notes", "modified", "modified_by", "auto_notes_sha256"}:
			doc.set(field, previous.get(field))

	doc.set("auto_notes_sha256", None)  # Any user save revokes automatic rewriting provenance.


def protect_child(doc, method=None):
	if doc.get("parenttype") != SESSION:
		_deny()
	if not any(parent.get("name") == doc.get("parent") for parent in _session_writers.get()):
		_deny()


def _protected_binding(doc):
	return (
		doc and doc.get("attached_to_doctype") == SESSION and doc.get("attached_to_field") in ARTIFACT_FIELDS
	)


def protect_file(doc, method=None):
	"""Run before File.validate can move bytes or rewrite attachment fields.

	The before_insert event also runs after core File.before_insert wrote a
	new blob; Frappe registers that blob for removal on transaction rollback.
	"""
	previous = doc.get_doc_before_save() if not doc.is_new() else None
	if (
		_protected_binding(doc)
		and doc.get("is_private")
		and (doc.get("attached_to_name"), doc.get("attached_to_field")) in _artifact_writers.get()
	):
		return
	fields = (
		"file_url",
		"content_hash",
		"is_private",
		"attached_to_doctype",
		"attached_to_name",
		"attached_to_field",
	)
	if previous and all(doc.get(key) == previous.get(key) for key in fields) and doc.get("content") is None:
		return
	if _protected_binding(doc) or _protected_binding(previous):
		_deny()
	# An unrelated File can alias a protected blob. Privacy changes in core
	# propagate by content_hash, so checking only the URL is insufficient.
	for field in ("file_url", "content_hash"):
		values = {row.get(field) for row in (doc, previous) if row and row.get(field)}
		if not values:
			continue
		aliases = frappe.get_all(
			"File",
			filters={field: ["in", sorted(values)]},
			fields=["attached_to_doctype", "attached_to_name", "attached_to_field"],
			limit_page_length=1001,
		)
		if len(aliases) > 1000 or any(_protected_binding(alias) for alias in aliases):
			_deny()
