# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""A failing settings read or a failing logger must never break capture, the metrics endpoint, the
AI Steps label or the frontend analyzer, and masking never fails open. Frappe-free."""

import logging

import pytest

import optimus  # noqa: F401 triggers _patch_recorder at import-time
from optimus import redaction, redaction_extras, redaction_log, settings
from optimus.tests.test_recorder_redaction_patch import _Recorder, _stub_recorder  # noqa: F401
from optimus.tests.test_redaction_url_wiring import _captured


@pytest.fixture(autouse=True)
def _fresh_failure_logs(monkeypatch):
	monkeypatch.setattr(redaction_extras, "_logged", set())
	monkeypatch.setattr(redaction_log, "_logged_capture", set())


@pytest.fixture
def broken_settings(monkeypatch):
	def boom():
		raise RuntimeError("db gone")

	monkeypatch.setattr(settings, "get_config", boom)


@pytest.fixture
def broken_logger(monkeypatch):
	def boom(self, *args, **kwargs):
		raise OSError("log disk full")

	monkeypatch.setattr(logging.Logger, "error", boom)
	monkeypatch.setattr(logging.Logger, "warning", boom)


class TestReadExtras:
	def test_register_still_masks_when_settings_and_logger_both_fail(self, broken_settings, broken_logger):
		Recorder = _Recorder()
		rec = Recorder.__new__(Recorder)
		Recorder.__init__(rec)
		rec.register({"query": "select name from tabUser where password = 'RAWPW'"})
		query = rec.calls[-1]["query"]
		assert "RAWPW" not in query
		assert "OMITTED" not in query.upper()


class TestCaptureLogging:
	def test_a_failing_logger_never_breaks_the_request(self, monkeypatch, broken_logger):
		def boom(payload, **kwargs):
			raise ValueError("RAWSECRET")

		monkeypatch.setattr(redaction, "redact_sensitive", boom)
		rec = _captured("/x", "x", {"a": "RAWSECRET"}, {"b": "RAWSECRET"})
		assert "RAWSECRET" not in str(rec.form_dict) + str(rec.headers)

	def test_a_failing_settings_read_with_a_failing_logger_still_masks(self, broken_settings, broken_logger):
		rec = _captured("/x", "x", {"password": "RAWSECRET"}, {"Referer": "/a?token=HEADSECRET"})
		assert "RAWSECRET" not in str(rec.form_dict)
		assert "HEADSECRET" not in str(rec.headers)

	def test_the_logger_function_itself_raising_is_contained(self, monkeypatch):
		def boom(field, error):
			raise OSError("x")

		monkeypatch.setattr(redaction_log, "log_capture_failure", boom)
		monkeypatch.setattr(redaction, "redact_sensitive", lambda *a, **k: 1 / 0)
		rec = _captured("/x", "x", {"a": "RAWSECRET"}, {"b": "RAWSECRET"})
		assert "RAWSECRET" not in str(rec.form_dict) + str(rec.headers)


class TestFormKeyExtras:
	def test_a_failing_read_and_a_failing_logger_fall_back_to_no_extras(self, broken_settings, broken_logger):
		assert redaction_extras.form_key_extras() == ()

	def test_ai_steps_label_masks_with_the_defaults(self, broken_settings, broken_logger):
		from optimus import ai_privacy

		rec = ai_privacy.action_recording({"cmd": "x", "path": "/a?token=SECRETVAL&b=1", "method": "GET"})
		assert "SECRETVAL" not in rec["path"] and "b=1" in rec["path"]

	def test_the_frontend_analyzer_masks_with_the_defaults(self, broken_settings, broken_logger):
		from optimus.analyzers import frontend_timings
		from optimus.analyzers.base import AnalyzeContext

		ctx = AnalyzeContext(session_uuid="t", docname="t")
		ctx.frontend_data = {
			"xhr": [{"recording_id": "g", "url": "/a?token=SECRETVAL", "duration_ms": 5}],
			"vitals": [],
		}
		assert "SECRETVAL" not in str(frontend_timings.analyze([], ctx).aggregate)

	def test_the_metrics_endpoint_masks_with_the_defaults(self, broken_settings, broken_logger):
		from optimus import api

		rows = api._mask_frontend_urls([{"url": "/a?token=SECRETVAL"}], "url")
		assert "SECRETVAL" not in str(rows)
