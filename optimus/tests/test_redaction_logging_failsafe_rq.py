# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""An RQ job timeout raised while reading the sensitive names or logging a redaction failure still
stops the job, and the failure is never swallowed. Needs rq."""

import logging

import pytest

pytest.importorskip("rq")

import optimus  # noqa: F401 triggers _patch_recorder at import-time
from optimus import redaction, redaction_extras, redaction_log, settings
from optimus.tests.test_recorder_redaction_patch import _Recorder, _stub_recorder  # noqa: F401
from optimus.tests.test_redaction_url_wiring import _captured

pytestmark = pytest.mark.rq


@pytest.fixture(autouse=True)
def _fresh_failure_logs(monkeypatch):
	monkeypatch.setattr(redaction_extras, "_logged", set())
	monkeypatch.setattr(redaction_log, "_logged_capture", set())


class TestJobTimeouts:
	def test_a_job_timeout_in_the_logger_still_stops_the_job(self, monkeypatch):
		from rq.timeouts import JobTimeoutException

		def boom(self, *args, **kwargs):
			raise JobTimeoutException("slow")

		monkeypatch.setattr(logging.Logger, "error", boom)
		monkeypatch.setattr(redaction, "redact_sensitive", lambda *a, **k: 1 / 0)
		with pytest.raises(JobTimeoutException):
			_captured("/x", "x", {"a": "v"}, {"b": "v"})

	def test_a_job_timeout_in_the_read_still_stops_the_job(self, monkeypatch):
		from rq.timeouts import JobTimeoutException

		def boom():
			raise JobTimeoutException("slow")

		monkeypatch.setattr(settings, "get_config", boom)
		with pytest.raises(JobTimeoutException):
			redaction_extras.form_key_extras()


class TestRecorderPathJobTimeouts:
	def test_a_timeout_in_the_settings_read_stops_init(self, monkeypatch):
		from rq.timeouts import JobTimeoutException

		def boom():
			raise JobTimeoutException("slow")

		monkeypatch.setattr(settings, "get_config", boom)
		with pytest.raises(JobTimeoutException):
			_captured("/x", "x", {"a": "v"}, {"b": "v"})

	def test_a_timeout_in_the_settings_read_stops_register(self, monkeypatch):
		from rq.timeouts import JobTimeoutException

		def boom():
			raise JobTimeoutException("slow")

		Recorder = _Recorder()
		rec = Recorder.__new__(Recorder)
		Recorder.__init__(rec)
		monkeypatch.setattr(settings, "get_config", boom)
		with pytest.raises(JobTimeoutException):
			rec.register({"query": "select 1"})
		assert rec.calls == []

	def test_a_timeout_in_the_redaction_stops_init_and_leaves_no_raw_field(self, monkeypatch):
		from rq.timeouts import JobTimeoutException

		def boom(payload, **kwargs):
			raise JobTimeoutException("slow")

		monkeypatch.setattr(redaction, "redact_sensitive", boom)
		Recorder = _Recorder()
		rec = Recorder.__new__(Recorder)
		rec.path, rec.cmd, rec.form_dict, rec.headers = "/x", "x", {"p": "RAWSECRET"}, {"h": "RAWSECRET"}
		with pytest.raises(JobTimeoutException):
			Recorder.__init__(rec)
		assert "RAWSECRET" not in str(rec.form_dict) + str(rec.headers)
