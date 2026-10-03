# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Render outside locks; replace the report and PDF reference in one transaction.

Callers authorize access and log failures through the AI logging chokepoint.
This helper owns its transactions and must run only after core data commits.
Old File records are retained until normal session/attachment retention. A
failed replacement never deletes the last report or its cached PDF.
"""

import uuid

import frappe

from optimus import ai_fix, ai_jobs, analyze, renderer
from optimus import ai_refresh_store as store
from optimus.ai_jobs import _touch_session


class StaleReport(RuntimeError):
	"""The rendering input changed or another operation owns the session."""


def render_report(docname, recordings=None, *, run_id=None, worker_token=None):
	guard = ai_fix._InterruptGuard(base=True)
	with guard:
		return _render_and_save(docname, recordings, run_id=run_id, worker_token=worker_token)
	if guard.pending():
		recordings = None
		raise guard.interrupt()


def _render_and_save(docname, recordings, *, run_id, worker_token):
	# A permission/config read may have opened an old REPEATABLE READ view.
	ai_jobs._transaction(lambda: None)
	doc = frappe.get_doc("Optimus Session", docname)
	if recordings is None:
		recordings = analyze.load_recordings_light(doc)
	ai_jobs._transaction(lambda: None)
	content = renderer.render_raw(doc, recordings)
	if not isinstance(content, str) or not content.strip():
		raise ValueError("Report renderer returned no HTML")
	ai_jobs._transaction(lambda: None)

	def save():
		parent = frappe.db.get_value("Optimus Session", docname, "*", as_dict=True, for_update=True)
		if (
			not parent or parent.get("status") not in {"Ready", "Failed", "Analyzing"}
			or parent.get("modified") != doc.get("modified")
			or parent.get("session_uuid") != doc.get("session_uuid")
		):
			raise StaleReport("Session changed during report rendering")
		run = frappe.db.get_value(store.RUN, {"active_session": docname}, "*", as_dict=True, for_update=True)
		if run_id:
			if (not run or run["name"] != run_id or not store._owns(run, worker_token, ai_jobs._now())
				or run["deadline"] <= ai_jobs._now()):
				raise StaleReport("Refresh no longer owns report replacement")
			ai_jobs._check_run(run, locked=True)
		elif run:
			raise StaleReport("An active refresh owns report replacement")
		file_doc = frappe.get_doc({
			"doctype": "File", "file_name": f"optimus_report_{uuid.uuid4().hex}.html",
			"attached_to_doctype": "Optimus Session", "attached_to_name": docname,
			"attached_to_field": "raw_report_file", "content": content.encode("utf-8"), "is_private": 1,
		})
		request = getattr(frappe.local, "request", None)
		try:
			# Keep the existing code-generated HTML extension exception narrow.
			frappe.local.request = None
			file_doc.insert(ignore_permissions=True)
		finally:
			frappe.local.request = request
		url = file_doc.file_url
		if not isinstance(url, str) or not url.startswith("/private/files/") or not file_doc.is_private:
			raise ValueError("Report attachment did not produce a private file")
		_touch_session(docname, raw_report_file=url, raw_report_pdf_file=None)
		pending = frappe.db.get_values(
			store.RUN, {"session_name": docname, "render_pending": 1},
			["name", "seq"], as_dict=True, for_update=True, limit=10001,
		)
		if len(pending) > 10000:
			raise store.JournalUnavailable("Report history exceeds the recovery limit")
		for previous in pending:
			store._update(store.RUN, previous, render_pending=0)
		phase2_pending = frappe.db.get_values(
			"Optimus Phase Two Run", {"parent": docname, "analyze_render_pending": 1},
			["name"], as_dict=True, for_update=True, limit=10001,
		)
		if len(phase2_pending) > 10000:
			raise store.JournalUnavailable("Phase 2 report history exceeds the recovery limit")
		for previous in phase2_pending:
			frappe.db.set_value("Optimus Phase Two Run", previous["name"],
				{"analyze_render_pending": 0}, update_modified=False)

	ai_jobs._transaction(save)
	return {
		"regenerated": True, "recordings_available": len(recordings), "actions_total": len(doc.actions or []),
	}
