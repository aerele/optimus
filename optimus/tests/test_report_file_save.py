# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the HTML report-file save bypass.

Frappe's File DocType throws ``FileTypeNotAllowed`` in before_insert when HTML
is not in the site's allowed-extension allowlist, but skips validation when
there is no ``frappe.request`` (code-generated files). analyze normally runs as
a background RQ job (no request, bypass fires); when the scheduler is disabled
it runs inline in the HTTP handler (request set, bypass would not fire), so
``_save_report_file`` nulls the request around the insert. These tests lock in
that behavior.
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


def test_save_report_file_preserves_original_error_handling():
	"""The outer try/except that logs failures to the Error Log and
	returns None must still wrap everything. Without this, a
	validator failure would crash the whole analyze pipeline
	instead of logging and continuing with no report attachment."""
	from optimus import analyze

	src = inspect.getsource(analyze._save_report_file)
	assert "frappe.log_error" in src
	assert "return None" in src
