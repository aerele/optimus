"""Persisted recordings are bounded JSON from a private, session-bound File."""

import gzip
import json
import sys
from types import SimpleNamespace

import pytest

from optimus import ai_fix, analyze

pytestmark = pytest.mark.rq


@pytest.fixture
def bundle(monkeypatch, tmp_path):
	root = tmp_path / "private" / "files"
	root.mkdir(parents=True)
	path = root / "fake.json.gz"
	doc = SimpleNamespace(name="fake-doc", session_uuid="fake-session", recordings_file="/private/files/fake.json.gz")
	file = SimpleNamespace(name="fake-file", file_url=doc.recordings_file, is_private=1,
		attached_to_doctype="Optimus Session", attached_to_name=doc.name, attached_to_field="recordings_file",
		get_full_path=lambda: str(path))
	state = SimpleNamespace(doc=doc, file=file, path=path, root=root, reads=[], logs=[], matches=["fake-file"])
	def get_all(doctype, *, filters, **kw):
		assert doctype == "File"
		assert filters == {"file_url": doc.recordings_file, "is_private": 1,
			"attached_to_doctype": "Optimus Session", "attached_to_name": doc.name, "attached_to_field": "recordings_file"}
		return state.matches
	def get_doc(*args):
		state.reads.append(args)
		return file
	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(get_all=get_all, get_doc=get_doc,
		get_site_path=lambda *parts: str(root.parent.parent.joinpath(*parts))))
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **kw: state.logs.append((sys.exc_info()[0], kw)))
	state.payload = {"schema": 1, "session_uuid": doc.session_uuid, "recordings": {
		"fake-recording": {"rec": {"uuid": "fake-recording", "calls": [{"query": "SELECT 1"}]},
			"tree_b64": "must-never-be-unpickled", "sidecar": [{"private": "fake-value"}]},
	}}
	def write(payload=None, *, raw=None):
		path.write_bytes(raw if raw is not None else gzip.compress(json.dumps(state.payload if payload is None else payload).encode()))
	state.write = write
	write()
	return state


def test_old_bundle_loads_json_without_pickle_or_sidecar(bundle, monkeypatch):
	import pickle

	def forbidden(*a, **kw):
		pytest.fail("persisted data must never reach pickle")
	monkeypatch.setattr(pickle, "loads", forbidden)
	out = analyze._load_recordings_bundle(bundle.doc)
	assert out["recordings"]["fake-recording"]["rec"]["calls"] == [{"query": "SELECT 1"}]
	assert bundle.reads == [("File", "fake-file")]
	assert not bundle.logs


@pytest.mark.parametrize("names", [[], ["fake-file", "duplicate-file"]])
def test_unbound_or_ambiguous_file_is_never_opened(bundle, names):
	bundle.matches = names
	assert analyze._load_recordings_bundle(bundle.doc) is None
	assert not bundle.reads
	assert len(bundle.logs) == 1 and bundle.logs[0][0] is None


@pytest.mark.parametrize("field,value", [
	("is_private", 0), ("attached_to_name", "other-doc"), ("attached_to_doctype", "Other"),
	("attached_to_field", "raw_report_file"), ("file_url", "/private/files/another.json.gz"),
])
def test_file_binding_rechecked_after_lookup(bundle, field, value):
	setattr(bundle.file, field, value)
	bundle.file.get_full_path = lambda: pytest.fail("rebound file must not be read")
	assert analyze._load_recordings_bundle(bundle.doc) is None
	assert bundle.logs


@pytest.mark.parametrize("change", ["session", "schema", "recordings", "entry", "uuid"])
def test_wrong_or_malformed_bundle_is_rejected(bundle, change):
	if change == "session":
		bundle.payload["session_uuid"] = "other-session"
	elif change == "schema":
		bundle.payload["schema"] = 999
	elif change == "recordings":
		bundle.payload["recordings"] = []
	elif change == "entry":
		bundle.payload["recordings"]["fake-recording"] = []
	else:
		bundle.payload["recordings"]["fake-recording"]["rec"]["uuid"] = "other-recording"
	bundle.write()
	assert analyze._load_recordings_bundle(bundle.doc) is None
	assert len(bundle.logs) == 1 and bundle.logs[0][0] is None


@pytest.mark.parametrize("kind", ["outside", "symlink", "fifo", "directory", "missing"])
def test_nonregular_or_escaping_paths_are_not_read(bundle, tmp_path, kind):
	outside = tmp_path / "other.json.gz"
	outside.write_bytes(bundle.path.read_bytes())
	if kind == "outside":
		bundle.file.get_full_path = lambda: str(outside)
	else:
		bundle.path.unlink()
		if kind == "symlink":
			bundle.path.symlink_to(outside)
		elif kind == "directory":
			bundle.path.mkdir()
		elif kind == "fifo":
			import os
			os.mkfifo(bundle.path)
	assert analyze._load_recordings_bundle(bundle.doc) is None
	assert bundle.logs


@pytest.mark.parametrize("raw", [b"not gzip", gzip.compress(b'{"schema":1,"schema":1,"session_uuid":"fake-session","recordings":{}}'),
	gzip.compress(b'{"schema":1,"session_uuid":"fake-session","recordings":{},"x":NaN}'),
	gzip.compress(b"[" * 1200 + b"]" * 1200)])
def test_invalid_gzip_json_or_ambiguous_keys_are_rejected(bundle, raw):
	bundle.write(raw=raw)
	assert analyze._load_recordings_bundle(bundle.doc) is None
	assert bundle.logs


@pytest.mark.parametrize("bound", ["compressed", "expanded", "nodes", "depth", "recordings"])
def test_bundle_resource_limits_are_enforced(bundle, monkeypatch, bound):
	from optimus import recording_bundle

	limits = {"compressed": "MAX_COMPRESSED_BYTES", "expanded": "MAX_JSON_BYTES", "nodes": "MAX_JSON_NODES",
		"depth": "MAX_JSON_DEPTH", "recordings": "MAX_RECORDINGS"}
	monkeypatch.setattr(recording_bundle, limits[bound], 0 if bound == "recordings" else 8)
	if bound == "depth":
		value = {}
		for _ in range(10):
			value = {"nested": value}
		bundle.payload["nested"] = value
		bundle.write()
	assert analyze._load_recordings_bundle(bundle.doc) is None
	assert bundle.logs


def test_interrupt_escapes_fresh_without_becoming_a_missing_bundle(bundle, monkeypatch):
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = Timeout("fake database timeout")
	def failed(*a, **kw):
		raise original
	monkeypatch.setattr(analyze.frappe, "get_all", failed)
	with pytest.raises(Timeout) as caught:
		analyze._load_recordings_bundle(bundle.doc)
	assert caught.value is not original and caught.value.__context__ is None
	assert not bundle.logs


@pytest.mark.parametrize("calls", ["bad", {"query": "SELECT 1"}, ["not a query record"]])
def test_malformed_query_arrays_cannot_escape_to_consumers(bundle, calls):
	bundle.payload["recordings"]["fake-recording"]["rec"]["calls"] = calls
	bundle.write()
	assert analyze._load_recordings_bundle(bundle.doc) is None
	assert bundle.logs


def test_new_snapshot_respects_the_same_size_limit_as_its_reader(monkeypatch):
	from optimus import recording_bundle, session

	stored, logs = [], []
	monkeypatch.setattr(recording_bundle, "MAX_JSON_BYTES", 64)
	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(cache=SimpleNamespace(
		hget=lambda *a: {"uuid": "fake-recording", "calls": []}, get_value=lambda *a: None)))
	monkeypatch.setattr(session, "get_session_meta", lambda *a: {})
	monkeypatch.setattr(analyze, "_save_report_file", lambda **kw: stored.append(True))
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **kw: logs.append((sys.exc_info()[0], kw)))
	analyze._persist_recordings_file("fake-doc", "fake-session", ["fake-recording"])
	assert stored == [], "never persist a snapshot the bounded reader must refuse"
	assert len(logs) == 1 and logs[0][0] is None


def test_reads_are_bounded_before_allocating_untrusted_content(bundle, monkeypatch):
	from optimus import recording_bundle

	file_reads, gzip_reads = [], []
	fdopen, gzip_read = recording_bundle.os.fdopen, gzip.GzipFile.read
	class WatchedFile:
		def __init__(self, *args, **kwargs):
			self.stream = fdopen(*args, **kwargs)
		def __enter__(self):
			return self
		def __exit__(self, *args):
			self.stream.close()
		def fileno(self):
			return self.stream.fileno()
		def read(self, size=-1):
			file_reads.append(size)
			return self.stream.read(size)
	def expanded(stream, size=-1):
		gzip_reads.append(size)
		return gzip_read(stream, size)
	monkeypatch.setattr(recording_bundle.os, "fdopen", WatchedFile)
	monkeypatch.setattr(gzip.GzipFile, "read", expanded)
	assert analyze._load_recordings_bundle(bundle.doc)
	assert file_reads and all(0 < size <= recording_bundle.MAX_COMPRESSED_BYTES + 1 for size in file_reads)
	assert gzip_reads and all(0 < size <= recording_bundle.MAX_JSON_BYTES + 1 for size in gzip_reads)


def test_empty_snapshot_is_a_valid_empty_dataset(bundle):
	bundle.payload["recordings"] = {}
	bundle.write()
	assert analyze._load_recordings_bundle(bundle.doc)["recordings"] == {}
	assert not bundle.logs


def test_bundle_wrapper_failure_closes_the_opened_file(bundle, monkeypatch):
	import os

	from optimus import recording_bundle
	opened = []
	original = os.open
	def open_file(*a, **kw):
		fd = original(*a, **kw)
		opened.append(fd)
		return fd
	def fail(*a, **kw):
		raise OSError("fake stream wrapper failure")
	monkeypatch.setattr(recording_bundle.os, "open", open_file)
	monkeypatch.setattr(recording_bundle.os, "fdopen", fail)
	assert analyze._load_recordings_bundle(bundle.doc) is None
	assert len(opened) == 2
	try:
		for fd in opened:
			with pytest.raises(OSError):
				os.fstat(fd)
	finally:
		for fd in opened:
			try:
				os.close(fd)
			except OSError:
				pass
