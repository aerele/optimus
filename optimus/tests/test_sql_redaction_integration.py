# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Where the SQL redactor meets the rest of the app: capture, render, the analyzer's EXPLAIN
and duplicate steps, index suggestions, the AI raw-value gate and the dialect lookup."""

import json
import types

import pytest

from optimus import analyze, redaction
from optimus.analyzers import index_suggestions
from optimus.tests.test_analyze_runtime import FakeCache, enrich_env  # noqa: F401
from optimus.tests.test_index_suggestions import _install_fake_recorder_module
from optimus.tests.test_recorder_redaction_patch import (  # noqa: F401
	_FakeRecorderInstance,
	_Recorder,
	_stub_recorder,
)

S = "Zq9xK2vLm7Wp"
R = redaction.PLACEHOLDER
FLAG = redaction.LITERALS_MASKED_KEY
NO_COLUMNS = "INSERT INTO `__Auth` VALUES ('User','password','a\\'1','" + S + "')"


# --- capture -----------------------------------------------------------------------------------------


def _register(data):
	fake = _FakeRecorderInstance()
	_Recorder().register(fake, data)
	return fake.calls[0]


@pytest.fixture
def site_dialect(monkeypatch):
	def set_dialect(value):
		monkeypatch.setattr("optimus.dbdialect.redaction_dialect", lambda: value)

	return set_dialect


def test_register_flags_a_statement_it_masked_whole(site_dialect):
	site_dialect("mariadb")
	call = _register({"query": NO_COLUMNS, "duration": 1.0})
	assert S not in call["query"]
	assert call[FLAG] is True


def test_register_does_not_flag_an_ordinary_redaction(site_dialect):
	site_dialect("mariadb")
	call = _register({"query": f"UPDATE `tabUser` SET `password`='{S}' WHERE `name`='a'", "duration": 1.0})
	assert FLAG not in call
	assert S not in call["query"]


def test_register_reads_the_statement_in_the_site_dialect(site_dialect):
	sql = f"SELECT 1 FROM t WHERE a = 'C:\\dir\\' AND password = '{S}'"
	site_dialect("postgres")
	assert _register({"query": sql})["query"] == f"SELECT 1 FROM t WHERE a = 'C:\\dir\\' AND password = {R}"
	site_dialect("mariadb")
	assert _register({"query": sql})["query"] == redaction.OMITTED_SQL


def test_register_stores_no_raw_sql_when_redaction_raises(monkeypatch):
	def boom(*_args, **_kwargs):
		raise RuntimeError("boom")

	monkeypatch.setattr(redaction, "redact_sql_fail_closed", boom)
	call = _register({"query": f"SELECT 1 WHERE password='{S}'"})
	assert call["query"] == redaction.OMITTED_SQL
	assert call[FLAG] is True


# --- render ------------------------------------------------------------------------------------------


def test_render_helpers_pass_the_site_dialect(monkeypatch):
	from optimus.renderer import _internal

	seen = []
	monkeypatch.setattr(_internal, "redaction_dialect", lambda: "postgres")
	monkeypatch.setattr(_internal, "_settings_extras", lambda: ((), ()))
	monkeypatch.setattr(
		_internal, "_redact_sql_literals_base", lambda sql, **kw: seen.append(kw["dialect"]) or sql
	)
	_internal._redact_sql_literals("SELECT 1")
	assert seen == ["postgres"]


def test_redact_call_queries_flags_and_is_idempotent():
	calls = [
		{"query": NO_COLUMNS, "normalized_query": NO_COLUMNS},
		{"query": "SELECT 1"},
		"junk",
		{"query": 5},
	]
	redaction.redact_call_queries(calls, dialect="mariadb")
	assert S not in calls[0]["query"] and S not in calls[0]["normalized_query"]
	assert calls[0][FLAG] is True
	assert FLAG not in calls[1]
	assert calls[3] == {"query": 5}
	before = json.dumps(calls)
	redaction.redact_call_queries(calls, dialect="mariadb")
	assert json.dumps(calls) == before


def test_redact_finding_sql_omits_nothing_for_a_readable_statement(monkeypatch):
	from optimus.renderer import _internal

	monkeypatch.setattr(_internal, "_settings_extras", lambda: ((), ()))
	monkeypatch.setattr(_internal, "redaction_dialect", lambda: "mariadb")
	findings = [
		{
			"finding_type": "Slow Query",
			"technical_detail": {"normalized_query": f"UPDATE t SET `api_key`='{S}' WHERE a=1"},
		}
	]
	_internal.redact_finding_sql(findings)
	assert findings[0]["technical_detail"]["normalized_query"] == f"UPDATE t SET `api_key`={R} WHERE a=1"


# --- analyzer ----------------------------------------------------------------------------------------

MASKED_SELECT = "SELECT * FROM tabFoo WHERE x = '<REDACTED>' AND password = '<REDACTED>'"


def _flagged(query=MASKED_SELECT, **extra):
	return {"query": query, "duration": 1, FLAG: True, **extra}


def test_flagged_call_skips_explain_even_with_a_stored_result(enrich_env):  # noqa: F811
	recording = {"uuid": "r", "calls": [_flagged(explain_result=[{"id": 1, "table": "tabFoo"}])]}
	analyze._enrich_recordings([recording])
	call = recording["calls"][0]
	assert call["explain_result"] == []
	assert call["explain_warning"] == analyze.EXPLAIN_SKIPPED_MASKED
	assert enrich_env.explain_queries == []


def test_flagged_call_never_reads_or_writes_the_explain_caches(enrich_env, monkeypatch):  # noqa: F811
	enrich_env.conf["optimus_explain_cache_ttl_seconds"] = 60
	recording = {
		"uuid": "r",
		"calls": [
			{"query": "SELECT * FROM tabFoo WHERE x = 1", "duration": 1},
			_flagged("SELECT * FROM tabFoo WHERE x = 1"),
		],
	}
	analyze._enrich_recordings([recording])
	plain, flagged = recording["calls"]
	assert plain["explain_result"]
	assert flagged["explain_result"] == []
	assert len(enrich_env.explain_queries) == 1


def test_flagged_call_leaves_the_caches_empty(enrich_env):  # noqa: F811
	enrich_env.conf["optimus_explain_cache_ttl_seconds"] = 60
	analyze._enrich_recordings([{"uuid": "r", "calls": [_flagged()]}])
	assert analyze.frappe.cache.store == {}


def test_masked_calls_are_not_exact_duplicates_and_omitted_ones_do_not_group(enrich_env, monkeypatch):  # noqa: F811
	def mark(recording):
		for call in recording["calls"]:
			call.update(exact_copies=3, normalized_copies=3, normalized_query=call["query"])

	monkeypatch.setattr(analyze, "mark_duplicates", mark)
	omitted = redaction.OMITTED_SQL
	recording = {
		"uuid": "r",
		"calls": [_flagged(), _flagged(omitted), _flagged(omitted), {"query": "SELECT 1", "duration": 1}],
	}
	analyze._enrich_recordings([recording])
	masked, first_omitted, second_omitted, plain = recording["calls"]
	assert masked["exact_copies"] == first_omitted["exact_copies"] == 1
	assert plain["exact_copies"] == 3
	assert first_omitted["normalized_query"] != second_omitted["normalized_query"]
	assert first_omitted["normalized_copies"] == second_omitted["normalized_copies"] == 1


def test_literal_shape_covers_unterminated_hex_binary_and_exponent():
	shape = analyze._literal_shape
	assert shape("SELECT 'abc") == shape("SELECT 'xyz")
	assert shape("SELECT 0xDEAD, 0b101, 1e10") == "select ?, ?, ?"


def test_index_suggestions_skips_a_statement_masked_whole(monkeypatch, empty_context):
	_install_fake_recorder_module(monkeypatch, lambda query: pytest.fail("optimizer called"))
	calls = [
		_flagged("SELECT * FROM tabLead WHERE status = '<REDACTED>'", normalized_query="SELECT 1", stack=[])
	]
	result = index_suggestions.analyze([{"uuid": "i", "calls": calls}], empty_context)
	assert result.findings == []


def test_index_suggestions_reads_the_masked_key_from_redaction(monkeypatch, empty_context):
	assert index_suggestions.LITERALS_MASKED_KEY == redaction.LITERALS_MASKED_KEY
	monkeypatch.setattr(index_suggestions, "LITERALS_MASKED_KEY", "renamed_masked_key")
	_install_fake_recorder_module(monkeypatch, lambda query: pytest.fail("optimizer called"))
	query = "SELECT * FROM tabLead WHERE status = '<REDACTED>'"
	calls = [
		{
			"query": query,
			"duration": 1,
			"renamed_masked_key": True,
			"normalized_query": "SELECT 1",
			"stack": [],
		}
	]
	result = index_suggestions.analyze([{"uuid": "i", "calls": calls}], empty_context)
	assert result.findings == []


# --- AI raw-value gate -------------------------------------------------------------------------------


@pytest.fixture
def raw_gate(monkeypatch):
	from optimus import ai_privacy

	monkeypatch.setattr(ai_privacy, "_sensitive_columns", lambda: ())
	return lambda sql: ai_privacy.query_text(sql, send_raw=True)


def test_raw_gate_still_strips_queries_the_old_gate_stripped(raw_gate):
	for sql in (
		"SELECT name FROM `tabGL Entry` WHERE credit > 100",
		"SELECT name FROM tabBank WHERE bank_name = 'HDFC'",
		"SELECT name FROM tabX WHERE card_no = 'x'",
	):
		assert raw_gate(sql) != sql.strip()


def test_raw_gate_strips_a_new_name_and_leaves_sidebar_raw(raw_gate):
	assert (
		raw_gate("SELECT name FROM tabX WHERE iban = 'DE89'") != "SELECT name FROM tabX WHERE iban = 'DE89'"
	)
	sidebar = "SELECT * FROM `tabWebsite Settings` WHERE show_sidebar = 1"
	assert raw_gate(sidebar) == sidebar


def test_the_ai_gate_names_are_frozen_to_the_old_list():
	from optimus import ai_privacy

	old = {
		"password", "pwd", "api_key", "apikey", "token", "secret", "csrf", "authorization", "cookie",
		"encryption_key", "private_key", "session_id", "access_key", "salt", "hash", "otp", "ssn",
		"recovery", "credit", "card", "bank",
	}  # fmt: skip
	assert len(ai_privacy._AI_RAW_GATE_NAMES) == len(old)
	assert set(ai_privacy._AI_RAW_GATE_NAMES) == old


# --- dialect lookup ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
	("configured", "expected"),
	[("MariaDB", "mariadb"), ("postgres", "postgres"), ("sqlite", None), (None, None), ("", None)],
)
def test_redaction_dialect_never_guesses(monkeypatch, configured, expected):
	import frappe

	from optimus import dbdialect

	monkeypatch.setattr(frappe, "db", types.SimpleNamespace(db_type=configured), raising=False)
	monkeypatch.setattr(frappe, "conf", types.SimpleNamespace(get=lambda *_: None), raising=False)
	assert dbdialect.redaction_dialect() == expected
