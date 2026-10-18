"""Real Frappe document, PDF and rollback boundaries on disposable sites."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from optimus import ai_jobs, analyze, api, janitor, pdf_export, redis_keys, session
from optimus.artifacts import read_artifact
from optimus.managed import artifact_writer, save_session


class TestManagedArtifactBoundaries(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.parents = []
		self.files = []

	def tearDown(self):
		frappe.db.rollback()
		for name in self.files:
			if frappe.db.exists("File", name):
				frappe.delete_doc("File", name, ignore_permissions=True, delete_permanently=True)
		for name in self.parents:
			if frappe.db.exists("Optimus Session", name):
				frappe.delete_doc("Optimus Session", name, ignore_permissions=True, delete_permanently=True)
		frappe.db.commit()
		super().tearDown()

	def parent(self, age=0):
		doc = frappe.get_doc(
			{
				"doctype": "Optimus Session",
				"session_uuid": "artifact-" + frappe.generate_hash(length=16),
				"user": "Administrator",
				"status": "Ready",
				"title": "Synthetic artifact fixture",
				"started_at": add_to_date(now_datetime(), days=-age),
			}
		)
		save_session(doc, insert=True)
		self.parents.append(doc.name)
		frappe.db.commit()
		return doc

	def artifact(self, doc, field, content):
		ext = "pdf" if field == "raw_report_pdf_file" else "html"
		if ext == "pdf":
			import io

			from pypdf import PdfWriter

			buffer = io.BytesIO()
			writer = PdfWriter()
			writer.add_blank_page(width=100, height=100)
			writer.add_metadata({"/Title": str(content)})
			writer.write(buffer)
			content = buffer.getvalue()
		file = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "artifact-" + frappe.generate_hash(length=16) + "." + ext,
				"is_private": 1,
				"attached_to_doctype": "Optimus Session",
				"attached_to_name": doc.name,
				"attached_to_field": field,
				"content": content,
			}
		)
		request = getattr(frappe.local, "request", None)
		try:
			frappe.local.request = None
			with artifact_writer(doc.name, field):
				file.insert(ignore_permissions=True)
		finally:
			frappe.local.request = request
		self.files.append(file.name)
		frappe.db.set_value("Optimus Session", doc.name, field, file.file_url)
		doc.set(field, file.file_url)
		frappe.db.commit()
		return file

	def test_external_creation_is_refused_even_with_ignore_permissions(self):
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Optimus Session",
					"session_uuid": "forged",
					"user": "Administrator",
					"status": "Ready",
					"started_at": now_datetime(),
				}
			).insert(ignore_permissions=True)
		frappe.db.rollback()

	def test_foreign_reference_cannot_be_read_or_deleted_as_this_sessions_cache(self):
		parent, victim = self.parent(), self.parent()
		file = self.artifact(victim, "raw_report_pdf_file", b"%PDF synthetic foreign bytes")
		frappe.db.set_value("Optimus Session", parent.name, "raw_report_pdf_file", file.file_url)
		frappe.db.commit()
		pdf_export.clear_cached_pdf(parent.session_uuid)
		self.assertTrue(frappe.db.exists("File", file.name))
		self.assertTrue(Path(file.get_full_path()).exists())
		self.assertFalse(frappe.db.get_value("Optimus Session", parent.name, "raw_report_pdf_file"))

	def test_real_text_file_content_can_be_converted_and_cached(self):
		parent = self.parent()
		self.artifact(
			parent,
			"raw_report_file",
			"<html><body><h1>Synthetic PDF report</h1><p>Expected report text.</p></body></html>",
		)
		url = pdf_export.get_or_generate_pdf(parent.session_uuid)
		self.assertEqual(url, pdf_export.get_or_generate_pdf(parent.session_uuid))
		pdf = read_artifact(parent.name, "raw_report_pdf_file", url)
		self.assertTrue(pdf.startswith(b"%PDF"))
		name = frappe.db.get_value("File", {"file_url": url, "attached_to_name": parent.name}, "name")
		self.files.append(name)

	def test_changed_source_after_render_publishes_no_pdf(self):
		parent = self.parent()
		self.artifact(parent, "raw_report_file", "<html><body>First report</body></html>")

		def render(content):
			frappe.db.set_value("Optimus Session", parent.name, "raw_report_file", None)
			frappe.db.commit()
			return b"%PDF synthetic stale result"

		with patch.object(pdf_export, "_html_to_pdf", render), self.assertRaises(frappe.ValidationError):
			pdf_export.get_or_generate_pdf(parent.session_uuid)
		self.assertFalse(frappe.db.get_value("Optimus Session", parent.name, "raw_report_pdf_file"))
		self.assertFalse(
			frappe.db.exists(
				"File", {"attached_to_name": parent.name, "attached_to_field": "raw_report_pdf_file"}
			)
		)

	def test_retention_rolls_back_rows_and_callbacks_after_a_late_file_failure(self):
		from frappe.core.doctype.file.file import File

		parent = self.parent(age=100)
		first = self.artifact(
			parent, "raw_report_file", "<html><body>Synthetic first retained file</body></html>"
		)
		second = self.artifact(parent, "raw_report_pdf_file", b"%PDF synthetic second retained file")
		real = File.on_trash
		deleted = []
		callbacks = []
		failure = [True]

		def trash(file):
			if file.attached_to_name == parent.name:
				deleted.append(file.name)
				if len(deleted) == 2 and failure[0]:
					raise RuntimeError("PRIVATE-RETENTION-CANARY")
			return real(file)

		original = ai_jobs.delete_session_state

		def cleanup(doc):
			original(doc)
			if doc.name == parent.name:
				frappe.db.after_commit.add(lambda: callbacks.append(doc.name))

		with (
			patch.object(File, "on_trash", trash),
			patch.object(ai_jobs, "delete_session_state", cleanup),
			patch("optimus.settings.get_config", lambda: SimpleNamespace(session_retention_days=30)),
		):
			janitor._sweep_old_sessions()
			self.assertTrue(frappe.db.exists("Optimus Session", parent.name))
			self.assertTrue(frappe.db.exists("File", first.name))
			self.assertTrue(frappe.db.exists("File", second.name))
			self.assertEqual(callbacks, [])
			missing = [f for f in (first, second) if not Path(f.get_full_path()).exists()]
			self.assertEqual(len(missing), 1)
			with self.assertRaises(frappe.ValidationError):
				read_artifact(parent.name, missing[0].attached_to_field, missing[0].file_url)
			frappe.db.rollback()
			failure[0] = False
			janitor._sweep_old_sessions()
			self.assertFalse(frappe.db.exists("Optimus Session", parent.name))
			self.assertEqual(callbacks, [parent.name])
		logs = frappe.get_all(
			"Error Log",
			filters={"method": "optimus retention delete"},
			fields=["error"],
			limit_page_length=10,
		)
		self.assertTrue(logs)
		self.assertTrue(all("PRIVATE-RETENTION-CANARY" not in row.error for row in logs))

	def test_captured_steps_provenance_survives_sanitizing_and_is_revoked_by_a_save(self):
		from optimus.auto_steps import may_rewrite

		parent = self.parent()
		recordings = [
			{
				"uuid": "recording-" + str(n),
				"path": "/api/method/example.run",
				"cmd": "example.run",
				"method": "POST",
				"duration": 12.34,
				"form_dict": {"doctype": "Synthetic &<>'\""},
				"calls": [],
			}
			for n in range(55)
		]
		recordings.append({"uuid": "noise", "path": "/assets/noise.js", "duration": 1, "calls": []})
		analyze._persist(parent.name, analyze.AnalyzeContext(parent.session_uuid, parent.name), recordings)
		current = frappe.get_doc("Optimus Session", parent.name)
		self.assertTrue(may_rewrite(current))
		self.assertIn("5 more action(s)", current.notes)
		self.assertIn("1 background", current.notes)
		current.title = "A full Desk save"
		current.save(ignore_permissions=True)
		frappe.db.commit()
		self.assertFalse(may_rewrite(frappe.get_doc("Optimus Session", parent.name)))

	def test_all_retention_failures_rollback_before_the_next_parent_and_log_commit(self):
		parents = [self.parent(age=100), self.parent(age=100)]
		names = {parent.name for parent in parents}
		callbacks = []
		real = frappe.delete_doc

		def fail_parent(doctype, name, *args, **kwargs):
			if doctype == "Optimus Session" and name in names:
				frappe.db.set_value(doctype, name, "title", "Must roll back")
				frappe.db.after_commit.add(lambda: callbacks.append(name))
				raise RuntimeError("PRIVATE-SIBLING-CANARY")
			return real(doctype, name, *args, **kwargs)

		with (
			patch.object(frappe, "delete_doc", fail_parent),
			patch("optimus.settings.get_config", lambda: SimpleNamespace(session_retention_days=30)),
		):
			janitor._sweep_old_sessions()
		self.assertEqual(callbacks, [])
		for parent in parents:
			self.assertEqual(frappe.db.get_value("Optimus Session", parent.name, "title"), parent.title)

	def test_concurrent_pdf_downloads_publish_one_attachment(self):
		import threading

		parent = self.parent()
		self.artifact(
			parent, "raw_report_file", "<html><body>Concurrent " + parent.session_uuid + "</body></html>"
		)
		binary = pdf_export._html_to_pdf("<html><body>Concurrent " + parent.session_uuid + "</body></html>")
		barrier = threading.Barrier(2)
		urls = []
		errors = []
		sites_path = str(Path(frappe.get_site_path()).resolve().parent)
		site = frappe.local.site

		def render(content):
			barrier.wait(timeout=10)
			return binary

		def download():
			frappe.init(site=site, sites_path=sites_path)
			frappe.connect()
			frappe.set_user("Administrator")
			try:
				urls.append(pdf_export.get_or_generate_pdf(parent.session_uuid))
			except Exception as exc:
				errors.append(type(exc).__name__)
			finally:
				frappe.db.rollback()
				frappe.destroy()

		with patch.object(pdf_export, "_html_to_pdf", render):
			threads = [threading.Thread(target=download, daemon=True) for _ in range(2)]
			for thread in threads:
				thread.start()
			for thread in threads:
				thread.join(timeout=30)
			self.assertTrue(all(not thread.is_alive() for thread in threads))
		self.assertEqual(errors, [])
		self.assertEqual(len(urls), 2)
		self.assertEqual(urls[0], urls[1])
		frappe.db.rollback()
		names = frappe.get_all(
			"File",
			filters={"attached_to_name": parent.name, "attached_to_field": "raw_report_pdf_file"},
			pluck="name",
		)
		self.files.extend(names)
		self.assertEqual(len(names), 1)

	def test_unscoped_file_insert_removes_its_written_blob_on_rollback(self):
		parent = self.parent()
		file = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "rejected-" + parent.session_uuid + ".txt",
				"is_private": 1,
				"attached_to_doctype": "Optimus Session",
				"attached_to_name": parent.name,
				"attached_to_field": "raw_report_file",
				"content": parent.session_uuid.encode(),
			}
		)
		file.flags.optimus_internal = True
		with self.assertRaises(frappe.ValidationError):
			file.insert(ignore_permissions=True)
		path = Path(file.get_full_path())
		self.assertTrue(path.exists())
		frappe.db.rollback()
		self.assertFalse(path.exists())
		self.assertFalse(frappe.db.exists("File", file.name))
		self.assertFalse(frappe.db.get_value("Optimus Session", parent.name, "raw_report_file"))

	def test_content_hash_alias_privacy_is_guarded_before_core_moves_bytes(self):
		parent = self.parent()
		content = ("hash-alias-" + parent.session_uuid).encode()
		alias = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "alias-" + parent.session_uuid + ".txt",
				"is_private": 1,
				"content": content,
			}
		).insert(ignore_permissions=True)
		self.files.append(alias.name)
		frappe.db.commit()
		bound = self.artifact(parent, "raw_report_file", content)
		separate_url = "/private/files/separate-" + parent.session_uuid + ".txt"
		separate_path = Path(
			frappe.get_site_path("private", "files", "separate-" + parent.session_uuid + ".txt")
		)
		separate_path.write_bytes(content)
		frappe.db.set_value("File", alias.name, "file_url", separate_url)
		frappe.db.commit()
		try:
			alias = frappe.get_doc("File", alias.name)
			self.assertNotEqual(alias.file_url, bound.file_url)
			self.assertEqual(alias.content_hash, bound.content_hash)
			alias.is_private = 0
			with self.assertRaises(frappe.ValidationError):
				alias.save(ignore_permissions=True)
			frappe.db.rollback()
			self.assertTrue(separate_path.exists())
			self.assertEqual(frappe.db.get_value("File", alias.name, "is_private"), 1)
			self.assertEqual(frappe.db.get_value("File", bound.name, "is_private"), 1)
		finally:
			separate_path.unlink(missing_ok=True)

	def test_phase_one_refusal_rolls_back_parent_and_cleans_only_its_metadata(self):
		self.assertFalse(session.get_active_session_for("Administrator"))
		key = redis_keys.lp_active("Administrator")
		previous = frappe.cache.get_value(key)
		frappe.cache.set_value(key, "synthetic-phase-two", expires_in_sec=60)
		created = []
		set_meta = session.set_session_meta

		def record_meta(sid, metadata):
			created.append(sid)
			return set_meta(sid, metadata)

		try:
			with patch.object(session, "set_session_meta", record_meta):
				with self.assertRaises(frappe.ValidationError):
					api.start(label="Refused synthetic Phase 1")
			self.assertEqual(len(created), 1)
			self.assertFalse(frappe.db.exists("Optimus Session", {"session_uuid": created[0]}))
			self.assertFalse(session.get_session_meta(created[0]))
			self.assertFalse(session.get_active_session_for("Administrator"))
			self.assertEqual(frappe.cache.get_value(key), "synthetic-phase-two")
		finally:
			frappe.cache.delete_value(key)
			if previous is not None:
				frappe.cache.set_value(key, previous, expires_in_sec=60)
