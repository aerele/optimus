# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""What the SQL redactor logs: settings that cannot be read, and a report whose statements it
mostly could not read. Counts and exception classes only, never SQL. Pure, no Frappe."""

import pytest

from optimus.tests.test_recorder_redaction_patch import (  # noqa: F401
	_FakeRecorderInstance,
	_Recorder,
	_stub_recorder,
)

S = "Zq9xK2vLm7Wp"


@pytest.fixture
def fresh_log(monkeypatch):
	from optimus import redaction_log

	monkeypatch.setattr(redaction_log, "_logged", set())
	return redaction_log


def test_unreadable_settings_omit_the_query_and_log_only_the_exception_class(monkeypatch):
	from optimus import safe_call
	from optimus import settings as _settings
	from optimus.redaction import OMITTED_SQL

	def broken():
		raise TimeoutError("cache down")

	lines = []
	monkeypatch.setattr(_settings, "get_config", broken, raising=False)
	monkeypatch.setattr(safe_call, "log_error_line", lines.append)
	fake = _FakeRecorderInstance()
	for _ in range(3):
		_Recorder().register(fake, {"query": f"SELECT 1 WHERE password = '{S}'", "duration": 0.1})
	# Without the customer's own sensitive names the defaults alone are not trusted: no SQL is kept.
	assert [call["query"] for call in fake.calls] == [OMITTED_SQL] * 3
	assert set(lines) == {"optimus: SQL redaction could not run (TimeoutError); query omitted"}
	assert S not in str(lines) and "cache down" not in str(lines)


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
