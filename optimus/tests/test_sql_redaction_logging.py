# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""What the SQL redactor logs: settings that cannot be read, and a report whose statements it
mostly could not read. Counts and exception classes only, never SQL. Pure, no Frappe."""

import pytest

S = "Zq9xK2vLm7Wp"


@pytest.fixture
def fresh_log(monkeypatch):
	from optimus import redaction_log

	monkeypatch.setattr(redaction_log, "_logged", set())
	return redaction_log


def test_unreadable_settings_are_logged_once_and_defaults_still_mask(monkeypatch, caplog, fresh_log):
	from optimus import settings as _settings
	from optimus.tests.test_recorder_redaction_patch import _FakeRecorderInstance, _Recorder

	def broken():
		raise TimeoutError("cache down")

	monkeypatch.setattr(_settings, "get_config", broken, raising=False)
	fake = _FakeRecorderInstance()
	with caplog.at_level("WARNING"):
		for _ in range(3):
			_Recorder().register(fake, {"query": f"SELECT 1 WHERE password = '{S}'", "duration": 0.1})
	assert all(S not in call["query"] for call in fake.calls)
	warned = [r for r in caplog.records if "sensitive-name settings" in r.getMessage()]
	assert len(warned) == 1 and "TimeoutError" in warned[0].getMessage()
	assert S not in caplog.text


def test_drift_is_logged_with_counts_only(caplog, fresh_log):
	counts = {"statements": 20, "masked_whole": 4, "omitted": 8}
	with caplog.at_level("WARNING"):
		fresh_log.log_redaction_drift(counts)
		fresh_log.log_redaction_drift(counts)
		fresh_log.log_redaction_drift({"statements": 3, "masked_whole": 3, "omitted": 0})
	assert [r.getMessage() for r in caplog.records] == [
		"Optimus SQL redaction could not read 12 of 20 statements (8 omitted)"
	]


def test_ordinary_reports_do_not_log_drift(caplog, fresh_log):
	with caplog.at_level("WARNING"):
		fresh_log.log_redaction_drift({"statements": 20, "masked_whole": 2, "omitted": 1})
	assert caplog.records == []


def test_the_render_helper_reports_drift(monkeypatch, fresh_log):
	from optimus.renderer import _internal

	seen = []
	monkeypatch.setattr(_internal, "_settings_extras", lambda: ((), ()))
	monkeypatch.setattr(_internal, "redaction_dialect", lambda: "mariadb")
	monkeypatch.setattr(_internal, "log_redaction_drift", seen.append)
	_internal._redact_call_queries([{"query": "SELECT 1 WHERE password = 'x"}])
	assert seen == [{"statements": 1, "masked_whole": 0, "omitted": 1}]
