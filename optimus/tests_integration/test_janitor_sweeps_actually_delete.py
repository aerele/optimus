# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Real-bench integration test for ``janitor.sweep_old_sessions`` deletion.

The daily cron enforces the retention policy: Ready / Failed sessions older
than ``DEFAULT_RETENTION_DAYS`` (90, configurable via
``site_config.optimus_session_retention_days``) are hard-deleted, capped at
``MAX_DELETIONS_PER_RUN`` (100) per sweep so a large backlog can't lock up
MariaDB.

The unit suite (`test_janitor.py`) mocks the actual deletion, so it can't
prove what these tests do:

  * the cron deletes the underlying DocType row (not just marks/moves it);
  * attached File rows (``raw_report_file``) are cascade-deleted (orphans
    would inflate disk forever);
  * the cutoff math: a 100-day-old session is deleted, a 30-day-old one kept
    (default 90-day retention);
  * active sessions (Recording, Analyzing, Stopping) are untouched regardless
    of age (the daily sweep only prunes terminal-state sessions; the 5-minute
    sweep handles the rest).
"""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from optimus import janitor

_SESSION_DOCTYPE = "Optimus Session"
_FILE_DOCTYPE = "File"


class TestJanitorSweepsActuallyDelete(FrappeTestCase):
	"""End-to-end: sweep_old_sessions deletes the row + attachments."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

	def setUp(self):
		super().setUp()
		# Tracks every session_uuid this test creates so tearDown
		# can wipe whatever the sweep DIDN'T delete (i.e., the
		# in-retention / active sessions used as negative controls).
		self._uuids: list[str] = []

	def tearDown(self):
		for uuid in self._uuids:
			try:
				docname = frappe.db.get_value(_SESSION_DOCTYPE, {"session_uuid": uuid}, "name")
				if docname:
					# File rows might still be around if the sweep
					# didn't touch this session.
					try:
						frappe.db.delete(
							_FILE_DOCTYPE,
							{
								"attached_to_doctype": _SESSION_DOCTYPE,
								"attached_to_name": docname,
							},
						)
					except Exception:
						pass
					frappe.delete_doc(
						_SESSION_DOCTYPE,
						docname,
						force=1,
						ignore_permissions=True,
					)
			except Exception:
				pass
		try:
			frappe.db.commit()
		except Exception:
			pass
		super().tearDown()

	# --- Helpers ------------------------------------------------------

	def _create_session(
		self,
		*,
		days_old: int,
		status: str = "Ready",
		with_attached_file: bool = False,
	) -> dict:
		"""Insert an Optimus Session with a controlled ``started_at``
		and status. Used to set up the sweep boundary conditions."""
		uuid = f"test-{frappe.generate_hash(length=12)}"
		started = add_to_date(now_datetime(), days=-days_old)
		doc = frappe.get_doc(
			{
				"doctype": _SESSION_DOCTYPE,
				"session_uuid": uuid,
				"title": f"integration janitor test {uuid}",
				"user": "Administrator",
				"status": status,
				"started_at": started,
				# stopped_at only matters for terminal states; set it
				# defensively to the same moment for harmless schema
				# happiness.
				"stopped_at": started,
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		self._uuids.append(uuid)

		file_url = None
		if with_attached_file:
			# Attach a synthetic File the way analyze.py's
			# _save_report_file would, so we can verify cascade
			# deletion.
			file_doc = frappe.get_doc(
				{
					"doctype": _FILE_DOCTYPE,
					"file_name": f"optimus_raw_report_{uuid}.html",
					"attached_to_doctype": _SESSION_DOCTYPE,
					"attached_to_name": doc.name,
					"attached_to_field": "raw_report_file",
					"content": b"<!DOCTYPE html><html><body>stub</body></html>",
					"is_private": 1,
				}
			)
			# Avoid validate_file_extension by clearing request same
			# trick analyze._save_report_file uses.
			saved_request = getattr(frappe.local, "request", None)
			try:
				try:
					frappe.local.request = None
				except Exception:
					pass
				file_doc.insert(ignore_permissions=True)
				file_url = file_doc.file_url
			finally:
				try:
					frappe.local.request = saved_request
				except Exception:
					pass
			frappe.db.set_value(_SESSION_DOCTYPE, doc.name, "raw_report_file", file_url)
			frappe.db.commit()
		return {"uuid": uuid, "docname": doc.name, "file_url": file_url}

	def _session_exists(self, uuid: str) -> bool:
		return bool(frappe.db.exists(_SESSION_DOCTYPE, {"session_uuid": uuid}))

	def _file_exists(self, file_url: str | None) -> bool:
		if not file_url:
			return False
		return bool(frappe.db.exists(_FILE_DOCTYPE, {"file_url": file_url}))

	# --- The 4 tests --------------------------------------------------

	def test_sweep_deletes_session_older_than_retention(self):
		"""The canary. A Ready session whose ``started_at`` is older
		than the default retention (90 days) MUST be deleted by the
		daily sweep. Without this, the Optimus Session table grows
		unbounded."""
		fixture = self._create_session(days_old=100, status="Ready")
		assert self._session_exists(fixture["uuid"]), "fixture failed to insert"

		janitor.sweep_old_sessions()

		assert not self._session_exists(fixture["uuid"]), (
			f"sweep_old_sessions did NOT delete the 100-day-old Ready session "
			f"{fixture['uuid']!r} retention policy is broken; the Optimus "
			f"Session table will grow forever"
		)

	def test_sweep_keeps_session_within_retention(self):
		"""Negative control: a Ready session comfortably INSIDE the retention
		window must be left alone.

		Retention is ``Optimus Settings.session_retention_days`` (default 30,
		config-profile dependent), so the fixture age is taken as HALF that
		window rather than a hard-coded 30 days (which would sit on the boundary
		and race the sweep's ``now()`` cutoff).
		"""
		from optimus.settings import get_config

		retention = max(1, int(get_config().session_retention_days or 30))
		within = max(0, retention // 2)
		fixture = self._create_session(days_old=within, status="Ready")
		assert self._session_exists(fixture["uuid"])

		janitor.sweep_old_sessions()

		assert self._session_exists(fixture["uuid"]), (
			f"sweep_old_sessions ate a {within}-day-old session "
			f"{fixture['uuid']!r} (retention={retention}d) too aggressive"
		)

	def test_sweep_keeps_active_sessions_regardless_of_age(self):
		"""Terminal-state contract: non-terminal sessions (Recording, Analyzing,
		Stopping) must NOT be deleted by the daily sweep even when ancient (the
		5-minute ``sweep_stale_sessions`` handles those). The daily sweep's
		filter pins to ``status IN (Ready, Failed)`` exclusively."""
		# Create an OLD Analyzing session the kind that would be
		# tempting to GC but is wrong to.
		fixture = self._create_session(days_old=100, status="Analyzing")
		assert self._session_exists(fixture["uuid"])

		janitor.sweep_old_sessions()

		assert self._session_exists(fixture["uuid"]), (
			f"sweep_old_sessions deleted a non-terminal-status session "
			f"{fixture['uuid']!r} (status=Analyzing) daily sweep must "
			f"only touch Ready/Failed; stale active sessions are the "
			f"5-minute sweep's job"
		)

	def test_sweep_cascades_attached_file_deletion(self):
		"""Disk-hygiene contract: deleting an Optimus Session must cascade to its
		``raw_report_file`` File row. Orphan File rows (the report is often the
		largest per-session artefact) would inflate disk usage forever."""
		fixture = self._create_session(days_old=120, status="Ready", with_attached_file=True)
		assert self._session_exists(fixture["uuid"])
		assert self._file_exists(fixture["file_url"]), "fixture File row didn't insert"

		janitor.sweep_old_sessions()

		assert not self._session_exists(fixture["uuid"]), "session not deleted; can't validate file cascade"
		# The File row MUST also be gone orphans inflate disk forever.
		assert not self._file_exists(fixture["file_url"]), (
			f"File row {fixture['file_url']!r} survived parent session "
			f"deletion orphan File rows inflate disk usage"
		)
