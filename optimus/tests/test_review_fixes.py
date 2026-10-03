# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Regression guards for the post-review fixes (analyze + api need a live
bench for true integration, so the contract is pinned by source-inspection,
matching test_suggest_fix_api.py)."""

import os
import re

_HERE = os.path.dirname(__file__)
_API_PATH = os.path.join(_HERE, "..", "api.py")
_ANALYZE_PATH = os.path.join(_HERE, "..", "analyze.py")


def _read(path: str) -> str:
	with open(path, encoding="utf-8") as f:
		return f.read()


def _fn_body(src: str, name: str) -> str:
	start = src.index(f"def {name}(")
	search_from = src.find("\n", start) + 1
	nxt = re.search(r"\n(?:def |@frappe\.whitelist)", src[search_from:])
	end = search_from + (nxt.start() if nxt else len(src) - search_from)
	return src[start:end]


# --- HIGH-1: steps tokens captured on the auto-analyze path -----------------




# --- HIGH-2: drain phase stays on "Capturing Background Jobs" ----------------


class TestDrainKeepsCapturingStatus:
	def test_bg_wait_does_not_flip_to_analyzing(self):
		body = _fn_body(_read(_ANALYZE_PATH), "_bg_wait_for_pending_jobs")
		assert '"status", "Capturing Background Jobs"' in body
		assert '"status", "Analyzing"' not in body  # the real Analyzing is in run()


# --- MEDIUM-6 + the stop-time status set ------------------------------------


class TestStopSetsCapturingStatus:
	def test_stop_session_sets_status_and_commits(self):
		body = _fn_body(_read(_API_PATH), "_stop_session")
		assert '"status", "Capturing Background Jobs"' in body
		assert "safe_commit()" in body


# --- drain_progress endpoint contract ---------------------------------------


class TestDrainProgressEndpoint:
	def test_permission_gated_and_window_math(self):
		src = _read(_API_PATH)
		assert re.search(r"@frappe\.whitelist\(\)\s*\ndef drain_progress", src)
		body = _fn_body(src, "drain_progress")
		assert "_require_session_permission(session_uuid)" in body
		assert "session.get_pending_jobs(session_uuid)" in body
		assert "- 60" in body  # window remaining = draining_until - now - 60-grace
		assert '"remaining_seconds"' in body
		assert '"window_seconds"' in body


# --- test_ai_connection must not bill the probe to a prior session -----------




# --- export_session parity --------------------------------------------------


class TestExportSessionTokenParity:
	def test_export_includes_token_fields(self):
		body = _fn_body(_read(_API_PATH), "export_session")
		assert '"ai_tokens_spent"' in body
		assert '"ai_refresh_count"' in body
		assert '"ai_steps_tokens"' in body


# --- behavioral: corrupt bundle degrades to None ----------------------------


class TestLoadRecordingsBundleCorrupt:
	def test_corrupt_gzip_returns_none(self, monkeypatch, tmp_path):
		from types import SimpleNamespace

		from optimus import ai_fix, analyze

		root = tmp_path / "private" / "files"
		root.mkdir(parents=True)
		bad = root / "bad.json.gz"
		bad.write_bytes(b"this is definitely not gzip")
		doc = SimpleNamespace(name="fake-doc", session_uuid="fake-session", recordings_file="/private/files/bad.json.gz")
		opened, failures = [], []
		def path():
			opened.append(True)
			return str(bad)
		file = SimpleNamespace(file_url=doc.recordings_file, is_private=1,
			attached_to_doctype="Optimus Session", attached_to_name=doc.name, attached_to_field="recordings_file",
			get_full_path=path)
		monkeypatch.setattr(analyze, "frappe", SimpleNamespace(get_all=lambda *a, **kw: ["fake-file"],
			get_doc=lambda *a: file, get_site_path=lambda *a: str(root)))
		monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **kw: failures.append(kw))
		assert analyze._load_recordings_bundle(doc) is None
		assert opened == [True], "the test must reach corrupt gzip, not fail an unrelated binding check"
		assert failures[0]["reason"] == "bundle_read_failed"
