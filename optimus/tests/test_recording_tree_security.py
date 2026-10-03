"""Only the live Redis path may unpickle, with a verified site signature by default."""

import inspect
import pickle
import sys
from types import SimpleNamespace

import pytest

from optimus import ai_fix, analyze, session

pytestmark = pytest.mark.rq


@pytest.fixture
def tree(monkeypatch):
	conf = {"encryption_key": "fake-signing-secret"}
	logs = []
	fake = SimpleNamespace(conf=conf)
	monkeypatch.setattr(analyze, "frappe", fake)
	monkeypatch.setattr(session, "frappe", fake)
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **kw: logs.append((sys.exc_info()[0], kw)))
	return SimpleNamespace(conf=conf, logs=logs)


def test_stable_signing_site_refuses_unsigned_by_default(tree):
	assert analyze._allow_unsigned_pickles() is False


@pytest.mark.parametrize("value,expected", [(False, False), (0, False), ("0", False), ("false", False),
	(True, True), (1, True), ("1", True), ("true", True), ("garbage", False), ([], False)])
def test_explicit_legacy_opt_in_is_strict(tree, value, expected):
	tree.conf["optimus_allow_unsigned_pickles"] = value
	assert analyze._allow_unsigned_pickles() is expected


def test_unsigned_capture_compatibility_only_when_site_has_no_signing_secret(tree):
	tree.conf.clear()
	assert analyze._allow_unsigned_pickles() is True
	tree.conf["optimus_allow_unsigned_pickles"] = False
	assert analyze._allow_unsigned_pickles() is False


def test_unreadable_signing_config_fails_closed(tree, monkeypatch):
	class Broken:
		def get(self, *a, **kw):
			raise RuntimeError("fake config outage")
	monkeypatch.setattr(analyze.frappe, "conf", Broken())
	assert analyze._allow_unsigned_pickles() is False


def test_valid_signature_round_trips_live_tree(tree):
	blob = session.sign_blob(pickle.dumps({"fake": "tree"}))
	assert analyze._deserialize_tree("fake-recording", blob, allow_unsigned=False) == {"fake": "tree"}
	assert not tree.logs


@pytest.mark.parametrize("kind", ["raw", "tampered", "stripped_signature"])
def test_unverified_bytes_never_reach_pickle_on_signed_site(tree, monkeypatch, kind):
	payload = pickle.dumps({"fake": "tree"})
	blob = session.sign_blob(payload)
	if kind == "raw":
		blob = payload
	elif kind == "stripped_signature":
		blob = b"x" * 32 + payload
	else:
		blob = blob[:blob.index(b"fake")] + b"F" + blob[blob.index(b"fake") + 1:]
	calls = []
	monkeypatch.setattr(pickle, "loads", lambda value: calls.append(value))
	assert analyze._deserialize_tree("fake-recording", blob, allow_unsigned=analyze._allow_unsigned_pickles()) is None
	assert not calls
	assert len(tree.logs) == 1 and tree.logs[0][0] is None


@pytest.mark.parametrize("prefix", [b"", b"x" * 32])
def test_explicit_legacy_override_keeps_old_redis_formats(tree, prefix):
	assert analyze._deserialize_tree("fake-recording", prefix + pickle.dumps({"fake": 1}), allow_unsigned=True) == {"fake": 1}


def test_persisted_bundle_rehydrate_interface_is_removed():
	assert not hasattr(analyze, "_rehydrate_from_bundle")
	assert "recordings_bundle" not in inspect.signature(analyze._fetch_recordings).parameters


def test_tree_interrupt_is_fresh_and_not_logged_as_missing(tree, monkeypatch):
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = Timeout("fake verification timeout")
	def interrupted(*a):
		raise original
	monkeypatch.setattr(session, "unsign_blob", interrupted)
	with pytest.raises(Timeout) as caught:
		analyze._deserialize_tree("fake-recording", b"fake", allow_unsigned=False)
	assert caught.value is not original and caught.value.__context__ is None
	assert not tree.logs


def test_new_snapshot_never_reads_or_embeds_tree_and_sidecar(monkeypatch):
	import gzip
	import json

	from optimus import redis_keys

	rec = {"uuid": "fake-recording", "calls": [], "pyi_session": object(),
		"sidecar": [{"fake": "private-arguments"}], "tree_b64": "opaque"}
	captured = []
	def value(key):
		assert key not in {redis_keys.tree("fake-recording"), redis_keys.sidecar("fake-recording")}
		return None
	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(cache=SimpleNamespace(
		hget=lambda key, *a: rec if key == analyze.RECORDER_REQUEST_HASH else None, get_value=value)))
	monkeypatch.setattr(session, "get_session_meta", lambda *a: {})
	monkeypatch.setattr(analyze, "_save_report_file", lambda **kw: captured.append(kw["content"]))
	analyze._persist_recordings_file("fake-doc", "fake-session", ["fake-recording"])
	out = json.loads(gzip.decompress(captured[0]))
	assert out["recordings"]["fake-recording"] == {"rec": {"uuid": "fake-recording", "calls": []}}
	assert "sidecar" in rec and "pyi_session" in rec, "snapshotting must not mutate the live recorder cache"


@pytest.mark.parametrize("boundary", ["cache", "file"])
def test_snapshot_interrupt_is_not_swallowed(monkeypatch, boundary):
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = Timeout("fake snapshot timeout")
	def fail(*a, **kw):
		raise original
	cache = SimpleNamespace(hget=fail if boundary == "cache" else lambda *a: {"uuid": "fake-recording"}, get_value=lambda *a: None)
	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(cache=cache, log_error=lambda **kw: None))
	monkeypatch.setattr(session, "get_session_meta", lambda *a: {})
	monkeypatch.setattr(analyze, "_save_report_file", fail)
	with pytest.raises(Timeout) as caught:
		analyze._persist_recordings_file("fake-doc", "fake-session", ["fake-recording"])
	assert caught.value is not original and caught.value.__context__ is None


def test_snapshot_file_insert_timeout_restores_request_and_escapes_fresh(monkeypatch):
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = Timeout("fake insert timeout")
	request = object()
	local = SimpleNamespace(request=request)
	def insert(**kw):
		assert local.request is None
		raise original
	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(local=local,
		get_doc=lambda *a: SimpleNamespace(insert=insert), log_error=lambda **kw: None))
	with pytest.raises(Timeout) as caught:
		analyze._save_report_file(docname="fake-doc", filename="fake.json.gz", attached_to_field="recordings_file", content=b"fake")
	assert local.request is request
	assert caught.value is not original and caught.value.__context__ is None
