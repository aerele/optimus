"""Capabilities for the profiler's own document and generated-artifact writers.

These capabilities are process-local context, never document flags supplied by
REST clients. Normal Session saves may edit title and notes only.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from urllib.parse import unquote
from weakref import WeakKeyDictionary

import frappe
from frappe import _

SESSION = "Optimus Session"
ARTIFACT_FIELDS = frozenset({"raw_report_file", "raw_report_pdf_file", "recordings_file"})
_session_writers = ContextVar("optimus_session_writers", default=())
_artifact_writers = ContextVar("optimus_artifact_writers", default=())
_file_preflighted = WeakKeyDictionary()
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


def _deny(*, artifact=False):
	frappe.throw(
		_("This file is bound to a managed profiling artifact. Use the session's report actions to replace it.")
		if artifact else _("Profiling data is managed by Optimus. Edit only the session title and notes."),
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


def _file_signature(doc):
	return (getattr(getattr(frappe, "local", None), "site", None),
		doc.get("file_url"), doc.get("content_hash"), doc.get("is_private"))


def _canonical_file_url(url):
	import posixpath
	return url.startswith(("/files/", "/private/files/")) and posixpath.normpath(url) == url and "//" not in url


def _remote_file_prefixes():
	from frappe.core.doctype.file.file import URL_PREFIXES
	return URL_PREFIXES


def _local_file_url(doc):
	"""The local blob core resolves now or on a later get_content()/ZIP call."""
	if not doc or doc.get("is_folder"):
		return None
	url = doc.get("file_url")
	if url:
		if isinstance(url, str) and "/files/" in url and url.startswith(("http://", "https://")):
			from frappe.utils import get_url

			site_url = get_url()
			if url.startswith(site_url):
				url = url.split(site_url, 1)[1]  # Match core get_full_path(), including on updates.
		return url if isinstance(url, str) and url.startswith(("/files/", "/private/files/")) else None
	filename = doc.get("file_name")
	if isinstance(filename, str) and filename:
		return ("/private/files/" if doc.get("is_private") else "/files/") + filename
	return None


def _protect_file_source(doc):
	"""Check the original URL; return whether core classified it as remote."""
	url = doc.get("file_url")
	if doc.get("is_folder") and url:
		frappe.throw(_("A folder cannot have a File URL."), frappe.ValidationError)
	if not url:
		filename = doc.get("file_name")
		if not doc.get("is_folder") and not doc.get("content") and isinstance(filename, str) and (
			"/" in filename or not _canonical_file_url(_local_file_url(doc) or "/files/")
		):
			frappe.throw(_("The File name must use a canonical local path."), frappe.ValidationError)
		return
	url = unquote(url)  # Inspect exactly what core will decode; do not decode twice.
	if url.startswith(_remote_file_prefixes()):
		return True  # Detect core's later conversion of site URLs to local aliases.
	if not _canonical_file_url(url):
		frappe.throw(_("The File URL must use a canonical local path."), frappe.ValidationError)
	if url.startswith("/private/files/") and not callable(getattr(doc, "validate_private_file_access", None)):
		protected = frappe.get_all("File", filters={"file_url": url, "attached_to_doctype": SESSION,
			"attached_to_field": ["in", sorted(ARTIFACT_FIELDS)]}, fields=["name"], limit_page_length=1)
		if protected and not frappe.has_permission("File", "read", protected[0]["name"]):
			_deny(artifact=True)


def _wrap_file_input(original):
	@wraps(original)
	def before_insert(doc, *args, **kwargs):
		active = "optimus" in frappe.get_installed_apps()
		supplied = doc.get("content")
		checked = bool(doc.get("file_url")) or isinstance(supplied, (str, bytes, bytearray)) and bool(supplied)
		remote_source = False
		if active:
			remote_source = _protect_file_source(doc)
		result = original(doc, *args, **kwargs)
		# Native core can turn an absolute site URL into a local alias after
		# its private-source check. Leave those aliases to the scoped fallback.
		if remote_source and not (doc.get("file_url") or "").startswith(_remote_file_prefixes()):
			checked = False
		if active and checked:
			try:
				_file_preflighted[doc] = _file_signature(doc)
			except TypeError:
				pass  # Custom unhashable controllers retain the scoped fallback below.
		return result
	before_insert._optimus_file_input_guard = True
	return before_insert


def install_file_input_guard():
	"""Preserve core/custom method chains; document events run after core insert."""
	try:
		from frappe.core.doctype.file.file import File
	except ImportError:
		return False  # No Frappe, or partial bootstrap; request/job entry retries.
	if not getattr(File.before_insert, "_optimus_file_input_guard", False):
		File.before_insert = _wrap_file_input(File.before_insert)
	return True


def _file_input_checked(doc):
	try:
		return _file_preflighted.get(doc) == _file_signature(doc)
	except TypeError:
		return False


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
		"file_name",
		"content_hash",
		"is_private",
		"is_folder",
		"attached_to_doctype",
		"attached_to_name",
		"attached_to_field",
	)
	local_source = _local_file_url(doc)
	if local_source and not _canonical_file_url(local_source):
		_deny(artifact=True)
	if previous and all(doc.get(key) == previous.get(key) for key in fields) and not doc.get("content"):
		return
	if _protected_binding(doc) or _protected_binding(previous):
		_deny(artifact=True)
	# Uploading supplied bytes cannot move an existing blob's privacy. Core
	# enforces private-URL read access and chooses a public/private duplicate.
	# A later privacy/content change must still pass the alias guard below.
	if doc.is_new():
		if _file_input_checked(doc):
			return
		# Bootstrap/custom-controller fallback: core may already have copied an
		# unreadable source. Never trust a new owner or a rewritten URL alone.
		if not doc.get("is_folder"):
			aliases = {}
			if local_source:
				aliases["file_url"] = local_source
			if doc.get("content_hash"):
				aliases["content_hash"] = doc.get("content_hash")
			protected = frappe.get_all("File", filters={"attached_to_doctype": SESSION,
				"attached_to_field": ["in", sorted(ARTIFACT_FIELDS)]}, or_filters=aliases,
				fields=["name"], limit_page_length=1) if aliases else []
			if protected and not frappe.has_permission("File", "read", protected[0]["name"]):
				_deny(artifact=True)
		return
	if (previous and not doc.get("content")
		and all(doc.get(key) == previous.get(key) for key in ("file_url", "content_hash", "is_private", "is_folder"))
		and (doc.get("file_url") or doc.get("file_name") == previous.get("file_name"))):
		return
	# An unrelated File can alias a protected blob. Privacy changes in core
	# propagate by content_hash, so checking only the URL is insufficient.
	alias_filters = {}
	for field in ("file_url", "content_hash"):
		values = {(_local_file_url(row) if field == "file_url" else row.get(field)) for row in (doc, previous) if row}
		values.discard(None)
		values.discard("")
		if not values:
			continue
		alias_filters[field] = ["in", sorted(values)]
	if alias_filters and frappe.get_all("File",
		filters={"attached_to_doctype": SESSION, "attached_to_field": ["in", sorted(ARTIFACT_FIELDS)]},
		or_filters=alias_filters, fields=["name"], limit_page_length=1):
		_deny(artifact=True)
