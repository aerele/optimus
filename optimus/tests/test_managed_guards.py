"""Exercise the real managed-data guards, including client-supplied bypass flags."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from optimus import managed


class Denied(Exception):
	pass


class Doc(dict):
	__hash__ = object.__hash__
	def __init__(self, data, *, before=None, new=False):
		super().__init__(deepcopy(data))
		self.before, self.new = before, new
		self.flags = SimpleNamespace(optimus_internal=True, ignore_permissions=True)
	def is_new(self):
		return self.new
	def get_doc_before_save(self):
		return self.before
	def as_dict(self):
		return dict(self)
	def set(self, key, value):
		self[key] = value


@pytest.fixture(autouse=True)
def guard_site(monkeypatch):
	def deny(*a, **kw):
		raise Denied()
	monkeypatch.setattr(managed, "frappe", SimpleNamespace(throw=deny, ValidationError=Denied,
		get_all=lambda *a, **kw: []))
	monkeypatch.setattr(managed, "_remote_file_prefixes", lambda: ("http://", "https://", "/api/method/"), raising=False)


def session_doc():
	return Doc({"name": "session", "doctype": managed.SESSION, "session_uuid": "uuid", "user": "user",
		"owner": "owner", "status": "Ready", "title": "Original", "notes": "Original notes",
		"total_queries": 17, "ai_tokens_spent": 30, "raw_report_file": "/private/files/report.html",
		"findings": [{"name": "finding", "title": "Preserved"}], "auto_notes_sha256": "proof"})


def test_desk_save_preserves_server_data_and_revokes_automatic_notes():
	before = session_doc()
	doc = Doc(dict(before), before=before)
	doc.update(title="Edited title", notes="My notes", total_queries=0, ai_tokens_spent=0,
		findings=[], auto_notes_sha256="forged")
	managed.protect_session(doc)
	assert doc["title"] == "Edited title" and doc["notes"] == "My notes"
	assert doc["total_queries"] == 17 and doc["ai_tokens_spent"] == 30
	assert doc["findings"] == before["findings"] and doc["auto_notes_sha256"] is None


@pytest.mark.parametrize("field", sorted(managed._IDENTITY_FIELDS))
def test_desk_save_cannot_change_identity_or_artifact_bindings(field):
	before = session_doc()
	doc = Doc(dict(before), before=before)
	doc[field] = "forged"
	with pytest.raises(Denied):
		managed.protect_session(doc)


def test_creation_requires_the_exact_process_local_document_capability():
	doc = Doc({"name": "new", "status": "Ready"}, new=True)
	with pytest.raises(Denied):
		managed.protect_session(doc)
	with managed.session_writer(Doc(dict(doc), new=True)), pytest.raises(Denied):
		managed.protect_session(doc)
	with managed.session_writer(doc):
		managed.protect_session(doc)
	with pytest.raises(Denied):
		managed.protect_session(doc)


@pytest.mark.parametrize("parent_type,parent", [("Other", "session"), (managed.SESSION, "other")])
def test_child_writes_require_session_parent_type_and_parent_capability(parent_type, parent):
	child = Doc({"parenttype": parent_type, "parent": parent})
	with managed.session_writer(session_doc()), pytest.raises(Denied):
		managed.protect_child(child)


def test_direct_child_write_is_denied_but_server_parent_save_is_allowed():
	child = Doc({"parenttype": managed.SESSION, "parent": "session"})
	with pytest.raises(Denied):
		managed.protect_child(child)
	with managed.session_writer(session_doc()):
		managed.protect_child(child)


def artifact(**updates):
	return Doc({"attached_to_doctype": managed.SESSION, "attached_to_name": "session",
		"attached_to_field": "recordings_file", "is_private": 1, "file_url": "/private/files/recordings.json.gz",
		"content_hash": "hash", **updates}, new=True)


def test_direct_artifact_insert_and_detachment_are_denied():
	with pytest.raises(Denied):
		managed.protect_file(artifact())
	before = artifact()
	doc = Doc(dict(before), before=before)
	doc.update(attached_to_doctype=None, attached_to_name=None, attached_to_field=None)
	with pytest.raises(Denied):
		managed.protect_file(doc)


@pytest.mark.parametrize("parent,field,private", [("other", "recordings_file", 1),
	("session", "raw_report_file", 1), ("session", "recordings_file", 0)])
def test_file_capability_is_scoped_to_parent_field_and_private_visibility(parent, field, private):
	with managed.artifact_writer(parent, field), pytest.raises(Denied):
		managed.protect_file(artifact(is_private=private))


def test_server_artifact_writer_allows_its_own_binding_and_cannot_escape_context():
	doc = artifact()
	with managed.artifact_writer("session", "recordings_file"):
		managed.protect_file(doc)
	with pytest.raises(Denied):
		managed.protect_file(doc)


def test_unchanged_unrelated_file_save_is_not_blocked():
	before = Doc({"file_url": "/files/photo.png", "content_hash": "unrelated", "is_private": 0})
	managed.protect_file(Doc(dict(before), before=before))


def test_identical_unrelated_upload_and_ordinary_save_need_no_alias_queries(monkeypatch):
	monkeypatch.setattr(managed.frappe, "get_all", lambda *a, **kw: pytest.fail("site-wide alias query"))
	upload = Doc({"content_hash": "artifact-hash", "content": b"identical supplied bytes", "is_private": 1}, new=True)
	managed._file_preflighted[upload] = managed._file_signature(upload)
	managed.protect_file(upload)
	before = Doc({"file_url": "/files/photo.png", "content_hash": "hash", "is_private": 0})
	doc = Doc({**before, "file_name": "renamed-photo.png", "content": b"", "attached_to_doctype": "Customer"}, before=before)
	managed.protect_file(doc)


def test_privacy_change_uses_one_scoped_probe_and_cannot_move_an_artifact(monkeypatch):
	queries = []
	def aliases(doctype, **kw):
		queries.append(kw)
		return [{"name": "protected"}]
	monkeypatch.setattr(managed.frappe, "get_all", aliases)
	before = Doc({"file_url": "/private/files/copy.txt", "content_hash": "shared-hash", "is_private": 1})
	doc = Doc({**before, "is_private": 0}, before=before)
	with pytest.raises(Denied):
		managed.protect_file(doc)
	assert len(queries) == 1
	assert queries[0]["filters"]["attached_to_doctype"] == managed.SESSION
	assert "shared-hash" in queries[0]["or_filters"]["content_hash"][1]


@pytest.mark.parametrize("can_read", [False, True])
def test_legacy_url_insert_cannot_acquire_an_unreadable_managed_file(monkeypatch, can_read):
	queries = []
	def files(*a, **kw):
		queries.append(kw)
		return [{"name": "protected"}]
	monkeypatch.setattr(managed.frappe, "get_all", files)
	monkeypatch.setattr(managed.frappe, "has_permission", lambda *a: can_read, raising=False)
	doc = Doc({"file_url": "/private/files/protected.json.gz", "is_private": 1}, new=True)
	if can_read:
		managed.protect_file(doc)
	else:
		with pytest.raises(Denied):
			managed.protect_file(doc)
	assert len(queries) == 1 and queries[0]["or_filters"]["file_url"] == doc["file_url"]


def test_modern_url_insert_defers_to_core_private_access_without_queries(monkeypatch):
	doc = Doc({"file_url": "/private/files/protected.json.gz", "is_private": 1}, new=True)
	doc.validate_private_file_access = lambda: None
	managed._file_preflighted[doc] = managed._file_signature(doc)
	def unexpected(*a, **kw):
		raise AssertionError("core already checks this URL")
	monkeypatch.setattr(managed.frappe, "get_all", unexpected)
	managed.protect_file(doc)



def test_legacy_public_copy_of_private_bytes_keeps_source_read_gate(monkeypatch):
	probes = []
	monkeypatch.setattr(managed.frappe, "get_all", lambda *a, **kw: probes.append(kw) or [{"name": "protected"}])
	monkeypatch.setattr(managed.frappe, "has_permission", lambda *a: False, raising=False)
	with pytest.raises(Denied):
		managed.protect_file(Doc({"file_url": "/files/copy.txt", "content_hash": "protected-hash"}, new=True))
	assert probes[0]["or_filters"]["content_hash"] == "protected-hash"


@pytest.mark.parametrize("url", ["/private/files/./secret.txt", "/files/../../private/files/secret.txt", "/private/files//secret.txt"])
def test_new_local_file_urls_must_be_canonical_even_with_modern_core(url):
	doc = Doc({"file_url": url}, new=True)
	doc.validate_private_file_access = lambda: None
	with pytest.raises(Denied):
		managed.protect_file(doc)



def test_file_preflight_checks_source_before_core_rewrites_it(monkeypatch):
	monkeypatch.setattr(managed.frappe, "get_installed_apps", lambda: ["optimus"], raising=False)
	seen = []
	def original(doc):
		seen.append("core read")
		doc["file_url"] = "/files/rewritten.txt"
	wrapped = managed._wrap_file_input(original)
	doc = Doc({"file_url": "/files/../../private/files/secret.txt"}, new=True)
	with pytest.raises(Denied):
		wrapped(doc)
	assert not seen
	# Ordinary supplied bytes have no source URL and cause no alias query.
	def no_probe(*a, **kw):
		raise AssertionError("supplied bytes must not scan aliases")
	monkeypatch.setattr(managed.frappe, "get_all", no_probe)
	upload = Doc({"content": b"my own bytes"}, new=True)
	wrapped(upload)
	managed.protect_file(upload)
	assert seen == ["core read"]
	assert wrapped.__wrapped__ is original


def test_file_preflight_proof_cannot_be_forged_or_reused_after_change(monkeypatch):
	monkeypatch.setattr(managed.frappe, "get_installed_apps", lambda: ["optimus"], raising=False)
	doc = Doc({"content": b"my own bytes", "file_url": None}, new=True)
	managed._wrap_file_input(lambda d: d.update(file_url="/private/files/safe.txt", content_hash="safe"))(doc)
	assert managed._file_input_checked(doc)
	forged = Doc(dict(doc), new=True)
	forged.flags.optimus_file_input_guard = True
	assert not managed._file_input_checked(forged)
	doc["content_hash"] = "changed"
	assert not managed._file_input_checked(doc)



def test_folder_metadata_cannot_alias_file_bytes():
	with pytest.raises(Denied):
		managed._protect_file_source(Doc({"is_folder": 1, "file_url": "/private/files/secret.txt"}, new=True))


@pytest.mark.parametrize("url", ["/api/method/remote", "/api%2Fmethod%2Fremote", "https%3A%2F%2Fexample.invalid/remote.txt"])
def test_native_remote_file_urls_reach_core_after_one_decode(monkeypatch, url):
	from urllib.parse import unquote

	monkeypatch.setattr(managed.frappe, "get_installed_apps", lambda: ["optimus"], raising=False)
	seen = []
	doc = Doc({"file_url": url}, new=True)
	def core(d):
		seen.append(d)
		d["file_url"] = unquote(d["file_url"])
	managed._wrap_file_input(core)(doc)
	assert seen == [doc] and managed._file_input_checked(doc)


def test_absolute_site_url_rewritten_to_private_alias_keeps_permission_fallback(monkeypatch):
	monkeypatch.setattr(managed.frappe, "get_installed_apps", lambda: ["optimus"], raising=False)
	monkeypatch.setattr(managed.frappe, "get_all", lambda *a, **kw: [{"name": "protected"}])
	monkeypatch.setattr(managed.frappe, "has_permission", lambda *a: False, raising=False)
	doc = Doc({"file_url": "https://site.invalid/private/files/private-recording.json.gz"}, new=True)
	managed._wrap_file_input(lambda d: d.update(file_url="/private/files/private-recording.json.gz"))(doc)
	assert not managed._file_input_checked(doc)
	with pytest.raises(Denied):
		managed.protect_file(doc)


def test_file_wrapper_leaves_sites_without_optimus_to_core(monkeypatch):
	monkeypatch.setattr(managed.frappe, "get_installed_apps", lambda: ["frappe"], raising=False)
	doc = Doc({"file_url": "/private/files/./legacy.txt"}, new=True)
	seen = []
	managed._wrap_file_input(lambda d: seen.append(d))(doc)
	assert seen == [doc] and not managed._file_input_checked(doc)



@pytest.mark.parametrize("content", [None, "", b""])
def test_file_name_only_reads_retain_the_alias_fallback(monkeypatch, content):
	monkeypatch.setattr(managed.frappe, "get_installed_apps", lambda: ["optimus"], raising=False)
	monkeypatch.setattr(managed.frappe, "get_all", lambda *a, **kw: [{"name": "protected"}])
	monkeypatch.setattr(managed.frappe, "has_permission", lambda *a: False, raising=False)
	doc = Doc({"file_name": "private-recording.json.gz", "is_private": 1, "content": content}, new=True)
	def core(d):
		d.update(file_url="/private/files/private-recording.json.gz", content_hash="protected-hash")
	managed._wrap_file_input(core)(doc)
	assert not managed._file_input_checked(doc)
	with pytest.raises(Denied):
		managed.protect_file(doc)


@pytest.mark.parametrize("content", [None, "", b""])
def test_file_name_only_metadata_cannot_grant_later_disk_read_access(monkeypatch, content):
	monkeypatch.setattr(managed.frappe, "get_installed_apps", lambda: ["optimus"], raising=False)
	queries = []
	monkeypatch.setattr(managed.frappe, "get_all", lambda *a, **kw: queries.append(kw) or [{"name": "protected"}])
	monkeypatch.setattr(managed.frappe, "has_permission", lambda *a: False, raising=False)
	doc = Doc({"file_name": "private-recording.json.gz", "is_private": 1, "content": content}, new=True)
	managed._wrap_file_input(lambda d: None)(doc)
	with pytest.raises(Denied):
		managed.protect_file(doc)
	assert queries[0]["or_filters"]["file_url"] == "/private/files/private-recording.json.gz"


def test_existing_filename_only_metadata_cannot_be_retargeted_to_artifact(monkeypatch):
	queries = []
	monkeypatch.setattr(managed.frappe, "get_all", lambda *a, **kw: queries.append(kw) or [{"name": "protected"}])
	before = Doc({"file_name": "ordinary.txt", "is_private": 1})
	doc = Doc({"file_name": "private-recording.json.gz", "is_private": 1}, before=before)
	with pytest.raises(Denied):
		managed.protect_file(doc)
	assert "/private/files/private-recording.json.gz" in queries[0]["or_filters"]["file_url"][1]


def test_folder_cannot_be_converted_to_a_filename_only_artifact_alias(monkeypatch):
	monkeypatch.setattr(managed.frappe, "get_all", lambda *a, **kw: [{"name": "protected"}])
	before = Doc({"file_name": "private-recording.json.gz", "is_private": 1, "is_folder": 1})
	doc = Doc({"file_name": "private-recording.json.gz", "is_private": 1, "is_folder": 0}, before=before)
	with pytest.raises(Denied):
		managed.protect_file(doc)


@pytest.mark.parametrize("filename", ["/private/files/secret.txt", "../secret.txt", "folder/secret.txt", ".."])
def test_filename_only_sources_reject_noncanonical_names_before_core(monkeypatch, filename):
	monkeypatch.setattr(managed.frappe, "get_installed_apps", lambda: ["optimus"], raising=False)
	seen = []
	with pytest.raises(Denied):
		managed._wrap_file_input(lambda d: seen.append(d))(Doc({"file_name": filename, "is_private": 1}, new=True))
	assert seen == []


def test_existing_file_cannot_acquire_a_noncanonical_local_alias():
	before = Doc({"file_url": "/files/ordinary.txt", "is_private": 0})
	doc = Doc({"file_url": "/private/files/./private-recording.json.gz", "is_private": 1}, before=before)
	with pytest.raises(Denied):
		managed.protect_file(doc)


def test_existing_file_cannot_acquire_an_absolute_site_artifact_alias(monkeypatch):
	monkeypatch.setattr("frappe.utils.get_url", lambda: "https://site.invalid", raising=False)
	queries = []
	monkeypatch.setattr(managed.frappe, "get_all", lambda *a, **kw: queries.append(kw) or [{"name": "protected"}])
	before = Doc({"file_url": "/files/ordinary.txt", "is_private": 0})
	doc = Doc({"file_url": "https://site.invalid/private/files/private-recording.json.gz", "is_private": 0}, before=before)
	with pytest.raises(Denied):
		managed.protect_file(doc)
	assert "/private/files/private-recording.json.gz" in queries[0]["or_filters"]["file_url"][1]
