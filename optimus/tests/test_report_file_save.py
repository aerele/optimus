# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Generated recording-file insertion keeps the narrow request bypass.

The transactional report writer has separate tests. This legacy helper now
serves recording bundles and must restore request state on every outcome.
"""

import inspect


def test_save_report_file_clears_request_around_insert():
	"""Source-inspection guard: _save_report_file must temporarily
	null out frappe.local.request before calling file_doc.insert().
	That's what triggers File.validate_file_extension's no-request
	bypass for code-generated files."""
	from optimus import analyze

	src = inspect.getsource(analyze._save_report_file)

	# The helper must set frappe.local.request = None before the
	# insert call.
	assert "frappe.local.request = None" in src, (
		"_save_report_file must temporarily null frappe.local.request "
		"before file_doc.insert() so File.validate_file_extension "
		"uses its no-request bypass for code-generated files. Pre-"
		"v0.5.2 the insert fired with a live request context and the "
		"validator threw FileTypeNotAllowed on HTML when the site's "
		"allowlist didn't include it."
	)

	# And MUST restore the original value afterwards.
	assert "frappe.local.request = saved_request" in src, (
		"_save_report_file must restore frappe.local.request after "
		"the insert so downstream request-handling code (e.g. the "
		"response builder in the inline-analyze caller) sees the "
		"real request object unchanged."
	)


def test_save_report_file_restore_is_in_finally():
	"""A failed insert must STILL restore frappe.local.request.
	Restoring only on success would leak None into downstream code
	when insert raises for any other reason."""
	from optimus import analyze

	src = inspect.getsource(analyze._save_report_file)
	# Check the restore is inside a finally block.
	# We grep for the literal sequence: try ... insert ... finally ... restore.
	stash_idx = src.find("saved_request = getattr(frappe.local")
	try_idx = src.find("try:", stash_idx)
	finally_idx = src.find("finally:", try_idx)
	restore_idx = src.find(
		"frappe.local.request = saved_request", finally_idx
	)
	assert stash_idx < try_idx < finally_idx < restore_idx, (
		"Restore of frappe.local.request must be in a finally so it "
		"runs regardless of whether insert succeeded. Current source "
		"doesn't match the expected try/finally structure."
	)


def test_save_recording_file_failure_is_logged_safely_and_remains_optional(monkeypatch):
	import sys
	from types import SimpleNamespace

	from optimus import ai_fix, analyze

	request, logs = object(), []
	local = SimpleNamespace(request=request)
	def insert(**kw):
		assert local.request is None
		raise RuntimeError("fake insert failure")
	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(local=local,
		get_doc=lambda *a: SimpleNamespace(insert=insert)))
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **kw: logs.append((sys.exc_info()[0], kw)))
	assert analyze._save_report_file(docname="fake-doc", filename="fake.json.gz",
		attached_to_field="recordings_file", content=b"fake") is None
	assert local.request is request
	assert logs == [(None, {"reason": "file_write_failed"})]
