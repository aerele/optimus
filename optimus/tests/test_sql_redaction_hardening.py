# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Hardening of the SQL redactor: size and time bounds, name spellings, fail-closed paths,
logging, the capture and render wiring, and the rarer SQL shapes. Pure, no Frappe."""

import logging
import time
import tracemalloc

import pytest

from optimus import redaction
from optimus.tests.test_recorder_redaction_patch import (  # noqa: F401
	_FakeRecorderInstance,
	_Recorder,
	_stub_recorder,
)
from optimus.tests.test_sql_redaction_corpus import FERNET, HASH, M, P, R, S, check, run

OMIT = redaction.OMITTED_SQL
FLAG = redaction.LITERALS_MASKED_KEY


def test_omitted_text_says_it_could_not_be_read_safely():
	assert OMIT == "[SQL omitted: could not be read safely]"


# --- bounds ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("unit", ["password_", "a_password_"])
@pytest.mark.parametrize("dialect", [M, P, None])
def test_long_underscore_identifier_is_fast_and_sensitive(unit, dialect):
	sql = "SELECT 1 FROM t WHERE " + unit * (200_000 // len(unit)) + f"x = '{S}'"
	start = time.perf_counter()
	out, _ = run(sql, dialect)
	assert time.perf_counter() - start < 10.0
	assert S not in out


@pytest.mark.parametrize("dialect", [M, P, None])
def test_statement_over_the_size_cap_is_omitted_without_a_memory_spike(dialect):
	big = [
		"INSERT INTO t (`password`) VALUES " + ",".join(["('x')"] * 800_000),
		"SELECT password=" + "(" * 4_000_000,
	]
	for sql in big:
		assert len(sql) > 3_000_000
		tracemalloc.start()
		start = time.perf_counter()
		out, flagged = run(sql, dialect)
		elapsed = time.perf_counter() - start
		_, peak = tracemalloc.get_traced_memory()
		tracemalloc.stop()
		assert (out, flagged) == (OMIT, True)
		assert elapsed < 5.0
		assert peak < 100_000_000


def test_statement_with_too_many_tokens_is_omitted():
	sql = "SELECT 1 FROM t WHERE password = 'x' AND " + " AND ".join(["a=1"] * 30_000)
	assert len(sql) < redaction.MAX_REDACTED_SQL_CHARS
	assert run(sql, M) == (OMIT, True)


@pytest.mark.parametrize("dialect", [P, None])
def test_distinct_dollar_tags_are_not_quadratic(dialect):
	sql = "SELECT password = " + "".join(f"$t{i}$" for i in range(24_000))
	assert 150_000 < len(sql) < redaction.MAX_REDACTED_SQL_CHARS
	start = time.perf_counter()
	out, flagged = run(sql, dialect)
	assert time.perf_counter() - start < 10.0
	assert (out, flagged) == (OMIT, True)


def test_ordinary_dollar_quote_and_numbered_placeholders_still_work():
	sql = f"SELECT $f$a'b$f$ FROM t WHERE password = '{S}' AND x = $1"
	assert run(sql, P)[0] == f"SELECT $f$a'b$f$ FROM t WHERE password = {R} AND x = $1"


def test_non_text_input_does_not_raise():
	assert redaction.redact_sql_literals(10**5000) == 10**5000
	assert redaction.redact_sql_literals(None) == ""


# --- camelCase names and JSON keys -------------------------------------------------------------------


@pytest.mark.parametrize("key", ["accessToken", "clientSecret", "refreshToken", "apiKey", "X_Api_Key"])
def test_camel_case_json_keys_are_masked(key):
	sql = f"INSERT INTO `tabIntegration Request` (`name`,`data`) VALUES ('r1','{{\"{key}\": \"{S}\"}}')"
	check(M, sql, [S], ["'r1'"])


@pytest.mark.parametrize("column", ["secretKey", "authToken", "accessToken", "bankAcNo"])
def test_camel_case_columns_are_sensitive(column):
	check(M, f"SELECT 1 FROM t WHERE `{column}` = '{S}' AND a='KEEP1'", [S], ["KEEP1"])


def test_camel_case_extras_match_camel_case_columns():
	assert S not in run(f"SELECT 1 FROM t WHERE aadhaarNo = '{S}'", M, ("aadhaarNo",))[0]


def test_unlisted_spellings_are_not_default_names():
	sql = "SELECT 1 FROM t WHERE secrets = 'a' AND mypassword = 'b' AND accesstoken = 'c' AND pin = 'd' AND pan = 'e'"
	assert run(sql, M) == (sql, False)
	assert "'a'" not in run(sql, M, ("secrets", "mypassword", "accesstoken", "pin", "pan"))[0]


# --- name and shape coverage -------------------------------------------------------------------------


@pytest.mark.parametrize("column", ["hmac", "csrf", "cookie", "hash", "recovery", "sid", "sessiondata"])
def test_every_short_default_name_masks_its_value(column):
	check(M, f"SELECT 1 FROM t WHERE `{column}` = '{S}' AND a='KEEP1'", [S], ["KEEP1"])


def test_a_real_bcrypt_hash_is_masked_by_shape_alone():
	bcrypt = "$2b$12$" + "a" * 53
	assert len(bcrypt) == 60
	assert bcrypt not in run(f"UPDATE t SET `x` = '{bcrypt}' WHERE a='KEEP1'", M)[0]
	short = "$2b$12$" + "a" * 52
	assert run(f"UPDATE t SET `x` = '{short}'", M)[0].endswith(f"'{short}'")


def test_an_overlong_stored_key_is_treated_as_sensitive():
	key = "a" * 300 + "_ok"
	sql = f"UPDATE `tabSingles` SET `value`='{S}' WHERE `field`='{key}' AND `password` IS NULL"
	assert S not in run(sql, M)[0]


def test_dialect_none_omits_when_only_the_postgres_reading_differs():
	sql = f"SELECT --a WHERE password = '{S}'"
	assert run(sql, None) == (OMIT, True)
	assert run(sql, M)[0] == f"SELECT --a WHERE password = {R}"


def test_returning_ends_the_operand():
	check(M, f"DELETE FROM t WHERE password = '{S}' RETURNING id", [S], ["RETURNING id"])
	check(P, f"UPDATE t SET a = 1 WHERE password = '{S}' RETURNING id", [S], ["RETURNING id"])


def test_ai_gate_names_are_exactly_the_old_list():
	from optimus import ai_privacy

	old = {
		"password", "pwd", "api_key", "apikey", "token", "secret", "csrf", "authorization", "cookie",
		"encryption_key", "private_key", "session_id", "access_key", "salt", "hash", "otp", "ssn",
		"recovery", "credit", "card", "bank",
	}  # fmt: skip
	assert set(ai_privacy._AI_RAW_GATE_NAMES) == old


# --- truncation note ---------------------------------------------------------------------------------


def test_the_truncation_marker_is_defined_once():
	from optimus.analyzers import base

	assert base.SQL_TRUNCATION_MARKER is redaction.SQL_TRUNCATION_MARKER


def test_only_the_exact_last_truncation_note_survives():
	note = f"{redaction.SQL_TRUNCATION_MARKER} at 10 of 99 characters]"
	assert run(f"SELECT 1 WHERE password = '{S}'\n{note}")[0].endswith(f"\n{note}")
	for bad in (
		f"SELECT 1 WHERE password = '{S}'\n{note} AND password is hunter2",
		f"SELECT 1 WHERE password = '{S}'\n{redaction.SQL_TRUNCATION_MARKER} password: hunter2",
	):
		assert "hunter2" not in run(bad)[0]
	early = run(f"SELECT 1 WHERE password = '{S}'\n{note}\nAND a = 1")[0]
	assert "[Optimus" not in early


# --- fail-closed logging -----------------------------------------------------------------------------


def test_failures_reach_the_hook_and_log_once_per_type_without_sql(monkeypatch, caplog):
	from optimus import redaction_log

	def boom(*_args, **_kwargs):
		raise ValueError(f"leaky {S}")

	monkeypatch.setattr(redaction, "_lex", boom)
	monkeypatch.setattr(redaction_log, "_logged", set())
	with caplog.at_level(logging.WARNING):
		for _ in range(3):
			text, flagged = redaction.redact_sql_fail_closed(
				f"SELECT 1 WHERE password='{S}'",
				dialect=M,
				on_error=redaction_log.log_redaction_failure,
			)
			assert (text, flagged) == (OMIT, True)
	messages = [r.getMessage() for r in caplog.records]
	assert messages == ["Optimus SQL redaction failed, statement omitted: ValueError"]
	assert S not in caplog.text


def test_call_queries_report_failures_to_the_hook(monkeypatch):
	seen = []
	monkeypatch.setattr(redaction, "_lex", lambda *a: 1 / 0)
	calls = [{"query": f"SELECT 1 WHERE password='{S}'"}]
	redaction.redact_call_queries(calls, dialect=M, on_error=seen.append)
	assert calls[0]["query"] == OMIT
	assert [type(e) for e in seen] == [ZeroDivisionError]


# --- capture and render wiring -----------------------------------------------------------------------


def _register(data):
	fake = _FakeRecorderInstance()
	_Recorder().register(fake, data)
	return fake.calls[0]


def test_register_omits_a_query_that_is_not_text():
	for raw in (b"SELECT 1 WHERE password='x'", 5, ["x"]):
		call = _register({"query": raw})
		assert call["query"] == OMIT
		assert call[FLAG] is True
	assert _register({"query": None})["query"] is None


def test_register_failure_path_uses_the_shared_constants(monkeypatch):
	monkeypatch.setattr(redaction, "redact_sql_fail_closed", lambda *a, **k: 1 / 0)
	call = _register({"query": "SELECT 1"})
	assert (call["query"], call[FLAG]) == (OMIT, True)


@pytest.mark.parametrize("dialect", ["postgres", "mariadb"])
def test_render_passes_the_site_dialect_to_the_call_redactor(monkeypatch, dialect):
	from optimus.renderer import _internal

	seen = []
	monkeypatch.setattr(_internal, "redaction_dialect", lambda: dialect)
	monkeypatch.setattr(_internal, "_settings_extras", lambda: ((), ()))
	monkeypatch.setattr(
		_internal, "_redact_call_queries_base", lambda calls, **kw: seen.append(kw["dialect"])
	)
	_internal._redact_call_queries([{"query": "SELECT 1"}])
	assert seen == [dialect]


def test_redact_finding_sql_reads_the_dialect_once(monkeypatch):
	from optimus.renderer import _internal

	reads = []
	monkeypatch.setattr(_internal, "redaction_dialect", lambda: reads.append(1) or M)
	monkeypatch.setattr(_internal, "_settings_extras", lambda: ((), ()))
	findings = [
		{"finding_type": "Slow Query", "technical_detail": {"normalized_query": f"SELECT {i}"}}
		for i in range(5)
	]
	_internal.redact_finding_sql(findings)
	assert len(reads) == 1


# --- rarer shapes ------------------------------------------------------------------------------------

SHAPES = [
	(P, f"SELECT 1 FROM t WHERE password = ANY(ARRAY['a', '{S}']) AND a='KEEP1'", [S], ["KEEP1"]),
	(P, f"SELECT 1 FROM t WHERE password = ARRAY['a', '{S}'] AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"UPDATE t SET password=REPLACE(password,'a','{S}') WHERE a='KEEP1'", [S], ["KEEP1"]),
	(M, f"UPDATE t SET api_key=INSERT(api_key,1,2,'{S}') WHERE a='KEEP1'", [S], ["KEEP1"]),
	(M, f"CREATE USER 'bob'@'%' IDENTIFIED BY '{S}'", [S], ["'bob'@'%'"]),
	(M, f"ALTER USER bob IDENTIFIED BY PASSWORD '{S}'", [S], ["bob"]),
	(M, f"CREATE USER bob IDENTIFIED WITH mysql_native_password BY '{S}'", [S], ["bob"]),
	(M, f"SELECT PASSWORD('{S}') FROM t WHERE a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT AES_DECRYPT(x, '{S}') FROM t WHERE a='KEEP1'", [S], ["KEEP1"]),
	(P, f"SELECT 1 FROM t WHERE password && '{S}' AND a='KEEP1'", [S], ["KEEP1"]),
	(P, f"SELECT 1 FROM t WHERE password @@ '{S}' AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE '{S}' MEMBER OF (password) AND a='KEEP1'", [S], ["KEEP1"]),
	(P, f"SELECT 1 FROM t WHERE data->>'password' = '{S}' AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE data->>'$.api_key' = '{S}' AND a='KEEP1'", [S], ["KEEP1"]),
	(P, f"SELECT 1 FROM t WHERE '{S}' = data->>'password' AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE CONCAT(token, '{S}') = 'abc' AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE CONCAT_WS('-', token, '{S}') != 'x'", [S], []),
	(
		M,
		f"UPDATE `tabSingles` SET `value`='{S}' WHERE `doctype`='X' AND `field` IN ('a', 'api_key')",
		[S],
		["'X'"],
	),
	(
		M,
		f"INSERT INTO `tabSingles` (`doctype`,`field`,`value`) SELECT 'X', 'k', '{S}' FROM y WHERE `field`='api_key'",
		[S],
		[],
	),
]


@pytest.mark.parametrize(("dialect", "sql", "absent", "kept"), SHAPES)
def test_rarer_shapes_never_leak(dialect, sql, absent, kept):
	out, _ = run(sql, dialect)
	for secret in absent:
		assert secret not in out, out
	for text in kept:
		assert text in out, out


def test_a_conditional_comment_masks_idempotently_with_no_dialect():
	sql = f"SELECT /*! STRAIGHT_JOIN */ a FROM t WHERE password = '{S}' AND b = 7"
	for dialect in (M, None):
		first = run(sql, dialect)
		assert run(first[0], dialect) == first


def test_hash_and_fernet_values_still_masked():
	check(M, f"UPDATE t SET x='{HASH}', y='{FERNET}' WHERE a='KEEP1'", [S], ["KEEP1"])


# --- second pass: quadratic dollar quotes, unary tilde, subquery literals ----------------------------


def _lex_kinds(sql, dialect):
	return [(k, sql[a:b]) for k, a, b in redaction._lex(sql, dialect)]


@pytest.mark.parametrize("dialect", [P, None])
@pytest.mark.parametrize("unit", ["$$ ", "$$ x $$ "])
def test_untagged_dollar_quotes_are_linear(unit, dialect):
	sql = "SELECT password=" + unit * (180_000 // len(unit))
	start = time.perf_counter()
	run(sql, dialect)
	assert time.perf_counter() - start < 2.0


def test_dollar_quote_forms_still_lex_as_strings():
	tokens = _lex_kinds("SELECT $$S$$, $t$S$t$, $$ a $$", P)
	assert [text for kind, text in tokens if kind == "str"] == ["$$S$$", "$t$S$t$", "$$ a $$"]


OPENERS = [
	"'", '"', "`", "$$", "$a$", "$a$ x ", "$$ x ", "$ ", "$1", "/*", "/* ", "--", "-- ", "#", "E'", "U&'",
	"N'", "_utf8'", "X'", "b'", "%(", "%s", "0x", "0b", "1e", "1.", ".", "\\", "''", "\\'", "'\\", "<",
	"->", "->>", "&", "@", "!~", "~*", "|", "w ", "''x", "$a$ $b$ ", "/*!", "(", "[", "?", ":",
]  # fmt: skip


@pytest.mark.parametrize("dialect", [M, P])
@pytest.mark.parametrize("opener", OPENERS)
def test_every_lexer_opener_repeated_is_linear(opener, dialect):
	sql = opener * (200_000 // len(opener))
	start = time.perf_counter()
	redaction._lex(sql, dialect)
	assert time.perf_counter() - start < 2.0


@pytest.mark.parametrize(
	"sql",
	[
		f"SELECT 1 FROM t WHERE password = ~ '{S}' AND a='KEEP1'",
		"SELECT 1 FROM t WHERE password = ~ 7391827 AND a='KEEP1'",
		f"SELECT 1 FROM t WHERE password = ~ $${S}$$ AND a='KEEP1'",
		f"SELECT 1 FROM t WHERE password IN (SELECT '{S}') AND a='KEEP1'",
		"SELECT 1 FROM t WHERE password IN (SELECT 7391827) AND a='KEEP1'",
		f"CREATE USER bob IDENTIFIED BY '{S}' '{S}'",
		f"SELECT 1 FROM t WHERE password = ~ '{S}' '{S}' AND a='KEEP1'",
		f"SELECT 1 FROM t WHERE REPLACE(password,'{S}','b') != 'x' AND a='KEEP1'",
		f"SELECT 1 FROM t WHERE POSITION('{S}' IN password) > 0 AND a='KEEP1'",
	],
)
def test_second_pass_leaks_are_masked(sql):
	out, _ = run(sql, P)
	assert S not in out and "7391827" not in out, out


def test_a_regex_match_tilde_is_still_a_comparison():
	assert run(f"SELECT 1 FROM t WHERE password ~ '{S}'", P)[0].endswith(R)


def test_a_subquery_keeps_its_own_literals_and_limit():
	sql = "SELECT 1 FROM t WHERE password IN (SELECT x FROM u WHERE y = 'keep' LIMIT 5)"
	assert run(sql, M) == (sql, False)


def test_a_long_double_quoted_string_is_not_a_name_with_no_dialect():
	sql = f"SELECT 1 FROM t WHERE \"{'a' * 130}\" = 'KEEP1' AND password IS NULL"
	assert run(sql, None) == (sql, False)
