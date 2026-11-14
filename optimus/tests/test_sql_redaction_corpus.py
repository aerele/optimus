# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Corpus and property tests for the SQL literal redactor in ``optimus/redaction.py``.

Every case runs against the MariaDB or PostgreSQL lexer and has to hold four
properties: no secret byte survives, the kept literals survive, the masked text
is token-preserving (so EXPLAIN still parses it), and a second pass changes
nothing. Pure, no Frappe.
"""

import time

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from optimus import redaction

R = "'<REDACTED>'"
PLACEHOLDERS = {R, "'%<REDACTED>'", "'_<REDACTED>'"}
S = "Zq9xK2vLm7Wp"
HASH = "$pbkdf2-sha256$29000$" + S
FERNET = "gAAAAABk" + S + "x" * 90
M, P = "mariadb", "postgres"


def run(sql, dialect=M, extras=()):
	return redaction.redact_sql_outcome(sql, dialect=dialect, extra_columns=extras)


def _sig(sql, dialect):
	return [(k, sql[a:b]) for k, a, b in redaction._lex(sql, dialect) if k != "cm"]


def _balanced(tokens):
	depth = 0
	for kind, text in tokens:
		if kind == "op" and text in ("(", ")", "[", "]"):
			depth += 1 if text in ("(", "[") else -1
			if depth < 0:
				return False
	return depth == 0


def _masked_run_end(src, start, following):
	"""Index where the masked run that begins at ``start`` stops matching ``following``."""
	depth = 0
	end = start
	while end < len(src):
		if (
			depth == 0
			and end > start
			and following is not None
			and src[end] == following
			and _balanced(src[start:end])
		):
			return end
		if src[end][0] == "op":
			depth += {"(": 1, ")": -1, "[": 1, "]": -1}.get(src[end][1], 0)
		end += 1
	return end


def assert_token_preserving(dialect, src, out):
	"""``out`` is ``src`` with some runs each replaced by exactly one placeholder token."""
	a, b = _sig(src, dialect), _sig(out, dialect)
	assert all(k != "open" for k, _ in b), "open token in output"
	i = j = 0
	while j < len(b):
		if b[j][1] in PLACEHOLDERS and (i >= len(a) or a[i] != b[j]):
			following = b[j + 1] if j + 1 < len(b) else None
			end = _masked_run_end(a, i, following)
			run_tokens = a[i:end]
			assert _balanced(run_tokens), f"unbalanced masked run {run_tokens}"
			assert not (i and run_tokens[0] == ("op", "(") and a[i - 1][1].lower() in ("in", "values")), (
				"IN/VALUES group collapsed"
			)
			depth = 0
			for kind, text in run_tokens:
				depth += (text in ("(", "[")) - (text in (")", "]")) if kind == "op" else 0
				assert not (kind == "op" and text == "," and depth == 0), "masked run crosses a comma"
			i, j = end, j + 1
			continue
		assert i < len(a) and a[i] == b[j], f"token mismatch at {j}: {b[j]}"
		i, j = i + 1, j + 1
	assert i == len(a), "trailing input tokens dropped"


def check(dialect, sql, absent, kept, extras=()):
	out, all_masked = run(sql, dialect, extras)
	assert not all_masked, f"unexpected doubt mode: {out}"
	for secret in absent:
		assert secret not in out, f"leak {secret!r} in {out}"
	for literal in kept:
		assert literal in out, f"lost {literal!r} in {out}"
	assert_token_preserving(dialect, sql, out)
	assert run(out, dialect, extras)[0] == out, "not idempotent"
	return out


HEX = S[:4].encode().hex()
CASES = [
	# operand shapes
	(M, f"SELECT 1 FROM t WHERE password = 0xDEAD{HEX} AND a='KEEP1'", ["0xDEAD"], ["KEEP1"]),
	(M, "SELECT 1 FROM t WHERE password = 0b101101 AND a='KEEP1'", ["0b101101"], ["KEEP1"]),
	(M, "SELECT 1 FROM t WHERE password = 1e10 AND a='KEEP1'", ["1e10"], ["KEEP1"]),
	(M, "SELECT 1 FROM t WHERE password = -5123 AND a='KEEP1'", ["5123"], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE password = admin{S} AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE password = 'ab' '{S}' AND a='KEEP1'", [S, "'ab'"], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE password = BINARY '{S}' AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE password = LOWER('{S}') AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE password = ('{S}') AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE password = CAST('{S}' AS CHAR) AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE password = /* c */ '{S}' AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE password = _utf8mb4'{S}' AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE password = N'{S}' AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE password = X'{HEX}' AND a='KEEP1'", [HEX], ["KEEP1"]),
	(P, f"SELECT 1 FROM t WHERE password = E'{S}\\'x' AND a='KEEP1'", [S], ["KEEP1"]),
	# operators
	(M, f"SELECT 1 FROM t WHERE password != '{S}' AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE password <> '{S}' AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE password <=> '{S}' AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE password NOT LIKE '%{S}' AND a='KEEP1'", [S], ["KEEP1", "'%<REDACTED>'"]),
	(M, f"SELECT 1 FROM t WHERE api_key BETWEEN '{S}' AND '{S}2' AND a='KEEP1'", [S], ["KEEP1"]),
	(
		M,
		f"SELECT 1 FROM t WHERE api_key IN ('{S}a)', '{S}b') AND a='KEEP1'",
		[S],
		["KEEP1", f"IN ({R}, {R})"],
	),
	(M, f"SELECT 1 FROM t WHERE api_key NOT IN ('{S}') AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE '{S}' = password AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE LOWER(password) = '{S}' AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE '{S}' NOT LIKE password AND a='KEEP1'", [S], ["KEEP1", "NOT LIKE password"]),
	(M, f"SELECT 1 FROM t WHERE `t`.`password` = '{S}' AND a='KEEP1'", [S], ["KEEP1"]),
	(P, f"SELECT 1 FROM t WHERE \"t\".\"password\" = '{S}' AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE password REGEXP '{S}' AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE CASE WHEN password = '{S}' THEN 1 END AND a='KEEP1'", [S], ["KEEP1"]),
	(P, f"SELECT 1 FROM t WHERE password SIMILAR TO '{S}%' AND a='KEEP1'", [S], ["KEEP1"]),
	(P, f"SELECT 1 FROM t WHERE password NOT SIMILAR TO '{S}%' AND a='KEEP1'", [S], ["KEEP1"]),
	(P, f"SELECT 1 FROM t WHERE password IS DISTINCT FROM '{S}' AND a='KEEP1'", [S], ["KEEP1"]),
	(P, f"SELECT 1 FROM t WHERE password IS NOT DISTINCT FROM '{S}' AND a='KEEP1'", [S], ["KEEP1"]),
	# same-side function arguments and JSON paths
	(M, f"SELECT 1 FROM t WHERE LOCATE('{S}', password) > 0 AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t WHERE IFNULL(api_key,'{S}') = 'x' AND a='KEEP1'", [S], ["KEEP1"]),
	(M, f"SELECT 1 FROM t ORDER BY FIELD(api_key,'{S}') LIMIT 5", [S], ["LIMIT 5"]),
	(M, f"SELECT 1 FROM t WHERE JSON_EXTRACT(data,'$.api_key') = '{S}' AND a='KEEP1'", [S], ["KEEP1"]),
	(
		M,
		f"UPDATE t SET data = JSON_SET(data,'$.api_key','{S}') WHERE a='KEEP1'",
		[S],
		["KEEP1", "'$.api_key'"],
	),
	# CASE results under a sensitive column (Frappe bulk_update)
	(
		M,
		f"UPDATE `tabEmployee` SET `bank_ac_no` = CASE WHEN `name`='E1' THEN '{S}' ELSE `bank_ac_no` END WHERE `name` IN ('E1')",
		[S],
		["WHEN `name`='E1'", "ELSE `bank_ac_no` END"],
	),
	(
		M,
		f"UPDATE `tabUser` SET `api_key` = CASE WHEN `name`='E1' THEN '{S}1' WHEN `name`='E2' THEN '{S}2' END WHERE `name` IN ('E1','E2')",
		[S],
		["WHEN `name`='E1'", "WHEN `name`='E2'"],
	),
	(M, f"SELECT 1 FROM t WHERE CASE WHEN a=1 THEN api_key END = '{S}' AND b='KEEP1'", [S], ["KEEP1"]),
	# row comparison keeps its arity
	(M, f"SELECT 1 FROM t WHERE (password, name) = ('{S}', 'KEEP1')", [S], []),
	(M, f"SELECT 1 FROM t WHERE (name, password) IN (('a', '{S}'), ('b', '{S}2'))", [S], []),
	# positional inserts
	(
		M,
		f"INSERT INTO `__Auth` (`doctype`,`name`,`fieldname`,`password`,`encrypted`) VALUES ('User','a@b.c','password','{HASH}',0) ON DUPLICATE KEY UPDATE `password`='{HASH}',`encrypted`=0",
		[S],
		["'a@b.c'", "'User'"],
	),
	(
		M,
		f"INSERT INTO `__Auth` (`doctype`,`name`,`fieldname`,`password`,`encrypted`) VALUES ('Email Account','x','password','{FERNET}',1) ON DUPLICATE KEY UPDATE `password`=VALUES(`password`)",
		[S],
		["'Email Account'", "VALUES(`password`)"],
	),
	(
		P,
		f'INSERT INTO "__Auth" ("doctype","name","fieldname","password","encrypted") VALUES (\'User\',\'a\',\'password\',\'{HASH}\',0) ON CONFLICT ("doctype","name","fieldname") DO UPDATE SET "password"=\'{HASH}\',"encrypted"=0',
		[S],
		["'User'"],
	),
	(
		P,
		f'INSERT INTO "__Auth" ("doctype","name","fieldname","password","encrypted") VALUES (\'E\',\'x\',\'password\',\'{FERNET}\',1) ON CONFLICT ("doctype","name","fieldname") DO UPDATE SET "password"=EXCLUDED."password"',
		[S],
		['EXCLUDED."password"'],
	),
	(
		M,
		f"INSERT INTO `tabUser` (`name`, `api_key`, `api_secret`, `full_name`) VALUES ('u1', '{S}k', '{S}s', 'KEEP1'), ('u2', '{S}k2', '{S}s2', 'KEEP2')",
		[S],
		["KEEP1", "KEEP2"],
	),
	(
		M,
		f"insert into `tabSingles` (doctype, field, value)\n\t\t\t\t\tvalues ('Push Notification Settings', 'api_key', '{S}')",
		[S],
		["'Push Notification Settings'"],
	),
	(
		M,
		f"INSERT INTO `tabSingles` (`doctype`,`field`,`value`) VALUES ('Google Settings','client_secret','{S}'),('Google Settings','enable','KEEP1')",
		[S],
		["KEEP1"],
	),
	(
		M,
		f"INSERT INTO `tabDefaultValue` (`name`,`parent`,`defkey`,`defvalue`) VALUES ('d1','__default','a@b.c_otpsecret','{FERNET}')",
		[S],
		["'d1'"],
	),
	(
		M,
		f"INSERT INTO `tabWebhook Header` (`name`,`key`,`value`) VALUES ('h1','Authorization','Bearer {S}'),('h2','Accept','KEEP1')",
		[S],
		["KEEP1", "'h1'"],
	),
	(
		M,
		f"INSERT INTO `tabSMS Parameter` (`name`,`parameter`,`value`) VALUES ('p1','api_key','{S}'),('p2','sender','KEEP1')",
		[S],
		["KEEP1"],
	),
	(
		M,
		f"INSERT INTO `tabCurrency Exchange Settings Details` (`name`,`key`,`value`) VALUES ('c1','api_key','{S}')",
		[S],
		["'c1'"],
	),
	(M, f"UPDATE `tabSingles` SET `value`='{S}' WHERE `doctype`='X' AND `field`='api_key'", [S], ["'X'"]),
	(
		M,
		f"INSERT INTO `tabVersion` (`name`,`ref_doctype`,`data`) VALUES ('v1','User','{{\\\"changed\\\":[[\\\"api_key\\\",null,\\\"{S}\\\"]]}}')",
		[S],
		["'v1'", "'User'"],
	),
	(
		M,
		f"INSERT INTO `tabSessions` (`sessiondata`, `user`, `lastupdate`, `sid`, `status`) VALUES ('{{\\\"csrf_token\\\": \\\"{S}c\\\"}}', 'a@b.c', NOW(), '{S}', 'Active')",
		[S],
		["'a@b.c'", "'Active'"],
	),
	(M, f"SELECT `sessiondata` FROM `tabSessions` WHERE `sid`='{S}'", [S], []),
	(
		M,
		f"INSERT INTO `tabOAuth Bearer Token` (`name`,`access_token`,`refresh_token`,`client`) VALUES ('{S}','{S}a','{S}r','KEEP1')",
		[S + "a", S + "r"],
		["KEEP1"],
	),
	(
		M,
		f"INSERT INTO `tabOAuth Authorization Code` (`name`,`authorization_code`,`client`) VALUES ('c','{S}','KEEP1')",
		[S],
		["KEEP1"],
	),
	(
		M,
		f"INSERT INTO `tabWebhook Request Log` (`name`,`headers`) VALUES ('l1','{{\\n \\\"Authorization\\\": \\\"Bearer {S}\\\"\\n}}')",
		[S],
		["'l1'"],
	),
	(
		P,
		f'INSERT INTO "tabIntegration Request" ("name","request_headers") VALUES (\'r1\',\'{{"Authorization": "Bearer {S}"}}\')',
		[S],
		["'r1'"],
	),
	(M, f"WITH x AS (SELECT 1) INSERT INTO t (`name`,`password`) VALUES ('KEEP1','{S}')", [S], ["KEEP1"]),
	(P, f"WITH x AS (SELECT 1) INSERT INTO t (\"name\",\"password\") VALUES ('KEEP1','{S}')", [S], ["KEEP1"]),
	(M, f"SELECT 1; INSERT INTO t (`name`,`password`) VALUES ('KEEP1','{S}')", [S], ["KEEP1"]),
	(M, f"INSERT INTO t (`name`,`password`) VALUES ('KEEP1',\"{S}\")", [S], ["KEEP1"]),
	(
		M,
		f"UPDATE `tabUser` SET `api_secret`='{S}', `full_name`='KEEP1' WHERE `name`='KEEP2'",
		[S],
		["KEEP1", "KEEP2"],
	),
	(M, f"UPDATE `tabEmployee` SET `bank_ac_no`='{S}' WHERE `name`='KEEP2'", [S], ["KEEP2"]),
	# the secret value shape alone
	(M, f"UPDATE `tabX` SET `blob`='{FERNET}' WHERE `name`='KEEP2'", [S], ["KEEP2"]),
	# string escapes per dialect
	(P, f"SELECT 1 FROM t WHERE a = 'C:\\dir\\' AND password = '{S}'", [S], ["'C:\\dir\\'"]),
	(M, f"SELECT 1 FROM t WHERE a = 'Pat O\\'Brien' AND password = '{S}'", [S], ["'Pat O\\'Brien'"]),
]

UNCHANGED = [
	(M, "SELECT 1 FROM t WHERE note = 'my password is abc'"),
	(
		M,
		"SELECT `password` FROM `__Auth` WHERE `doctype`='User' AND `name`='a@b.c' AND `fieldname`='password'",
	),
	(M, "SELECT 1 FROM t WHERE password IS NULL AND api_key IS NOT NULL"),
	(M, "SELECT 1 FROM t WHERE password = %s AND token = %(p)s"),
	(P, "SELECT 1 FROM t WHERE password = $1"),
	(M, "SELECT 1 FROM t WHERE password = ?"),
	(M, "UPDATE `tabGL Entry` SET `credit`=100 WHERE `credit` > 0 AND `bank_account`='HDFC'"),
	(M, "SELECT 1 FROM `tabJob Card` WHERE `job_card`='JC-1' AND credit_in_account_currency > 5"),
	(M, "SELECT * FROM `tabWebsite Settings` WHERE `show_sidebar`=1 AND inside_x='a'"),
	(M, "SELECT a.password FROM a JOIN b ON a.password = b.password WHERE a.x = 'KEEP'"),
	(M, "INSERT INTO `tabSingles` (doctype, field, value) VALUES ('System Settings', 'country', 'India')"),
	(M, "SELECT 1 FROM t WHERE name IN (SELECT parent FROM tabX WHERE x = 'KEEP')  AND password = password"),
	(M, "SELECT 1 FROM t WHERE name = password AND a = 'KEEP'"),
	(M, "SELECT 1 FROM t WHERE 5 = (SELECT x FROM tabUser WHERE api_key = %s)"),
	(M, "SELECT 1 FROM t WHERE 5 IN (SELECT x FROM tabUser WHERE api_key = %s)"),
]


@pytest.mark.parametrize(("dialect", "sql", "absent", "kept"), CASES)
def test_corpus_masks_secrets_and_keeps_the_rest(dialect, sql, absent, kept):
	check(dialect, sql, absent, kept)


@pytest.mark.parametrize(("dialect", "sql"), UNCHANGED)
def test_names_with_nothing_to_mask_stay_byte_identical(dialect, sql):
	assert run(sql, dialect) == (sql, False)


def test_masked_text_for_a_few_cases_is_exact():
	assert run(f"SELECT 1 FROM t WHERE password = LOWER('{S}') AND a='KEEP1'")[0] == (
		f"SELECT 1 FROM t WHERE password = {R} AND a='KEEP1'"
	)
	assert run(f"SELECT 1 FROM t WHERE api_key IN ('{S}', 'b')")[0] == (
		f"SELECT 1 FROM t WHERE api_key IN ({R}, {R})"
	)
	assert run(f"SELECT 1 FROM t WHERE (password, name) = ('{S}', 'k')")[0] == (
		f"SELECT 1 FROM t WHERE (password, name) = ({R}, {R})"
	)
	assert run(f"UPDATE t SET `bank_ac_no` = CASE WHEN a=1 THEN '{S}' ELSE `bank_ac_no` END")[0] == (
		f"UPDATE t SET `bank_ac_no` = CASE WHEN a=1 THEN {R} ELSE `bank_ac_no` END"
	)


def test_comments_become_one_space():
	out, _ = run(f"SELECT 1 /* c */ FROM t WHERE password = '{S}' -- tail\n")
	assert "/*" not in out
	assert "-- tail" not in out


def test_the_truncation_note_of_a_cut_query_survives():
	from optimus.analyzers.base import SQL_TRUNCATION_MARKER

	assert redaction.SQL_TRUNCATION_MARKER == SQL_TRUNCATION_MARKER
	note = f"{SQL_TRUNCATION_MARKER} at 10 of 99 characters]"
	assert run(f"SELECT 1 FROM t WHERE password = '{S}'\n{note}")[0].endswith(f"{R}\n{note}")
	assert run(f"INSERT INTO t VALUES ('{S}') /* c */ -- password\n{note}")[0].endswith(note)


def test_like_keeps_its_wildcard_prefix_for_the_index_analyzers():
	assert run(f"SELECT 1 FROM t WHERE password LIKE '%{S}'")[0].endswith("'%<REDACTED>'")
	assert run(f"SELECT 1 FROM t WHERE password LIKE '{S}%'")[0].endswith(R)


# --- the token-preserving helper must reject the known bad outputs -----------------------------------


@pytest.mark.parametrize(
	("src", "out"),
	[
		(
			"SELECT 1 FROM t WHERE password = N'secret'",
			"SELECT 1 FROM t WHERE password = '<REDACTED>''secret'",
		),
		(
			"INSERT INTO t (`password`) VALUES (`password`)",
			"INSERT INTO t (`password`) VALUES (`password`='<REDACTED>'(`password`))",
		),
		("INSERT INTO t (a,b) VALUES ('x', 'y')", "INSERT INTO t (a,b) VALUES ('<REDACTED>', '<REDACTED>'"),
		("SELECT 1 WHERE api_key IN ('a','b')", "SELECT 1 WHERE api_key IN '<REDACTED>'"),
	],
)
def test_token_preserving_helper_rejects_bad_output(src, out):
	with pytest.raises(AssertionError):
		assert_token_preserving(M, src, out)


# --- lexer -------------------------------------------------------------------------------------------


def kinds(sql, dialect):
	return [(k, sql[a:b]) for k, a, b in redaction._lex(sql, dialect) if k != "cm"]


def test_lexer_string_prefixes_and_placeholders():
	assert kinds("_utf8mb4'a' N'b' X'41' 'it''s'", M) == [
		("str", "_utf8mb4'a'"),
		("str", "N'b'"),
		("str", "X'41'"),
		("str", "'it''s'"),
	]
	assert kinds("a = $1 AND b = %(p)s AND c = %s AND d = ?", P)[2] == ("ph", "$1")
	assert [t for t in kinds("a = %(p)s AND c = %s AND d = ?", M) if t[0] == "ph"] == [
		("ph", "%(p)s"),
		("ph", "%s"),
		("ph", "?"),
	]


def test_lexer_dialect_differences():
	assert kinds(r"'a\'b'", M) == [("str", r"'a\'b'")]
	assert kinds(r"'a\'b'", P)[0] == ("str", r"'a\'")
	assert kinds('"x"', M) == [("dq", '"x"')]
	assert kinds('"x"', P) == [("id", '"x"')]
	assert kinds("E'a\\'b'", P) == [("str", "E'a\\'b'")]
	assert kinds("$tag$a'b$tag$", P) == [("str", "$tag$a'b$tag$")]
	assert redaction._lex("1 # c\n", M)[-1][0] == "cm"
	assert redaction._lex("1 # c\n", P)[-1][0] != "cm"


def test_lexer_comment_rules():
	assert [k for k, _, _ in redaction._lex("a --x\nb", P)].count("cm") == 1
	assert [k for k, _, _ in redaction._lex("a --x\nb", M)].count("cm") == 0
	assert [k for k, _, _ in redaction._lex("a -- x\nb", M)].count("cm") == 1
	assert kinds("a /*! b */", M)[1][0] == "cond"


def test_lexer_unterminated_input_is_an_open_token():
	assert redaction._lex("a = 'abc", M)[-1][0] == "open"
	assert redaction._lex("a = /* abc", M)[-1][0] == "open"
	assert redaction._lex("a = `abc", M)[-1][0] == "open"


# --- fail closed -------------------------------------------------------------------------------------


@pytest.mark.parametrize("dialect", [M, P, None])
def test_unterminated_quote_never_prints_the_secret(dialect):
	out, flagged = run(f"SELECT 1 FROM t WHERE x = 'abc AND password = '{S}'", dialect)
	assert S not in out
	assert flagged


def test_postgres_comment_rules_cannot_hide_a_secret_after_the_comment():
	for sql in (
		f"SELECT 1 FROM t WHERE x = 1 --'a\n AND password = '{S}'",
		f"SELECT 1 FROM t /* a /* b */ 'x */ WHERE password = '{S}'",
		f"SELECT 1 FROM t /* a /* b */ ' */ WHERE password = '{S}' --'",
		f"SELECT 1 FROM t WHERE x = 1 /* it's */ AND password = '{S}'",
	):
		out, flagged = run(sql, P)
		assert S not in out, out
		assert flagged


def test_a_dialect_none_statement_the_two_lexers_disagree_on_is_omitted():
	out, flagged = run(f"SELECT 1 FROM t WHERE a = 'x\\' AND password = '{S}'", None)
	assert out == redaction.OMITTED_SQL
	assert flagged


def test_dialect_none_masks_when_both_lexers_agree():
	out, flagged = run(f"SELECT 1 FROM t WHERE password = '{S}' AND a='KEEP1'", None)
	assert (out, flagged) == (f"SELECT 1 FROM t WHERE password = {R} AND a='KEEP1'", False)


STRUCTURAL = [
	f'INSERT INTO t VALUES ("{S}", 1) ON DUPLICATE KEY UPDATE password = 2',
	f"INSERT INTO t SELECT '{S}' FROM x WHERE password = 1",
	f"INSERT INTO `__Auth` VALUES ('User','password','a\\'1','{S}')",
	f"INSERT INTO t (`name`,`password`) VALUES ('{S}')",
	f"INSERT INTO t (`name`,`password`) SELECT 'a', '{S}'",
	f"SELECT 1 FROM t WHERE (password = '{S}'",
	f"SELECT 1 FROM t WHERE password = '{S}')",
]


@pytest.mark.parametrize("sql", STRUCTURAL)
def test_structural_doubt_masks_every_literal_and_keeps_the_sql(sql):
	out, flagged = run(sql, M)
	assert flagged
	assert S not in out
	assert "INSERT" in out or "SELECT" in out


def test_doubt_output_masks_numbers_and_keeps_names():
	out, flagged = run(f"INSERT INTO t VALUES (5, 'a', '{S}') ON DUPLICATE KEY UPDATE password = 7")
	assert flagged
	assert out == f"INSERT INTO t VALUES (0, {R}, {R}) ON DUPLICATE KEY UPDATE password = 0"


def test_doubt_output_does_not_turn_a_join_condition_into_a_string():
	out, flagged = run(f"INSERT INTO t SELECT 1 FROM a JOIN b ON u.x = v.y WHERE password = '{S}'")
	assert flagged
	assert "u.x = v.y" in out


def test_conditional_comment_does_not_swallow_the_statement():
	out, flagged = run(f"SELECT /*! STRAIGHT_JOIN */ a FROM t WHERE password = '{S}'")
	assert flagged
	assert S not in out
	assert "STRAIGHT_JOIN */ a FROM t WHERE password" in out


def test_redact_never_returns_the_input_after_an_exception(monkeypatch):
	def boom(*_args, **_kwargs):
		raise RuntimeError("boom")

	monkeypatch.setattr(redaction, "_lex", boom)
	assert redaction.redact_sql_literals(f"SELECT 1 WHERE password='{S}'", dialect=M) == redaction.OMITTED_SQL
	with pytest.raises(RuntimeError):
		redaction.redact_sql_literals(f"SELECT 1 WHERE password='{S}'", dialect=M, strict=True)


def test_dialect_is_keyword_only():
	with pytest.raises(TypeError):
		redaction.redact_sql_literals("SELECT 1", (), False, M)


def test_known_gap_a_positional_insert_with_no_hint_is_left_alone():
	sql = "INSERT INTO t VALUES ('plain-value')"
	assert run(sql) == (sql, False)


# --- names -------------------------------------------------------------------------------------------


def test_settings_extras_match_case_insensitively():
	out, flagged = run("SELECT 1 FROM t WHERE `aadhaar` = '1234'", M, ("Aadhaar",))
	assert not flagged
	assert "1234" not in out
	assert "1234" not in run("SELECT 1 FROM t WHERE `AADHAAR` = '1234'", M, (" aadhaar ",))[0]


def test_blank_extras_do_not_match_everything():
	sql = "SELECT 1 FROM t WHERE a = '1'"
	assert run(sql, M, ("", "  "))[0] == sql


def test_default_names_cover_secrets_cards_and_bank_numbers_not_money_columns():
	names = redaction.DEFAULT_SENSITIVE_SQL_COLUMNS
	for kept in ("credit", "card", "bank"):
		assert kept not in names
	for added in ("card_number", "bank_account_no", "iban", "cvv", "sid", "sessiondata", "authorization"):
		assert added in names


def test_otp_issuer_name_is_masked_as_a_compound_name():
	assert R in run("UPDATE `tabSystem Settings` SET `otp_issuer_name`='ERPNext'")[0]


def test_sql_names_hit_uses_word_boundaries_for_short_names():
	assert redaction.sql_names_hit("SELECT sid FROM tabSessions")
	assert not redaction.sql_names_hit("SELECT show_sidebar, inside FROM t WHERE consider=1")
	assert redaction.sql_names_hit("SELECT 1 WHERE api_key='x'")
	assert redaction.sql_names_hit("SELECT 1 WHERE note = 'x'", ("note",))


def test_value_shapes_alone_trip_the_fast_path():
	assert redaction.sql_names_hit(f"UPDATE t SET x = '{FERNET}'")
	assert redaction.sql_names_hit("UPDATE t SET x = '$2b$12$" + "a" * 53 + "'")


def test_a_user_value_that_only_starts_like_a_token_is_kept():
	sql = "UPDATE t SET `x` = 'gAAAAA', `note` = '$2b$ is a prefix'"
	assert run(sql)[0] == sql


# --- adversarial inputs finish in linear time --------------------------------------------------------

ADVERSARIAL = {
	"quote-escape": "SELECT password='" + "\\'" * 100000,
	"open-paren": "SELECT password=" + "(" * 200000,
	"comments": "SELECT password=" + "/*" * 100000,
	"repeat-cmp": "password=" * 22000,
	"backtick": "password `" + "a" * 199999,
	"hexprefix": "password=" + "x'" * 100000,
	"nested-sens": "SELECT 1 WHERE " + "(" * 100000 + "password" + ")" * 100000 + "='x'",
	"in-open": "SELECT 1 WHERE password" + " IN (" * 50000,
	"values-rows": "INSERT INTO t (`password`) VALUES " + ",".join(["('x')"] * 40000),
	"and-chain": "SELECT 1 WHERE " + " AND ".join(["password='x'"] * 20000),
	"case-open": "SELECT 1 WHERE password = " + "CASE WHEN a THEN b " * 20000,
	"dashes": "SELECT password=" + "--" * 100000,
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
@pytest.mark.parametrize("dialect", [M, P, None])
def test_adversarial_input_is_linear(name, dialect):
	sql = ADVERSARIAL[name]
	start = time.perf_counter()
	out, _ = run(sql, dialect)
	assert time.perf_counter() - start < 10.0
	assert isinstance(out, str)


# --- properties --------------------------------------------------------------------------------------

LEFT, RIGHT = "Qz7Kp", "Vw3Rx"
TEMPLATES = [
	"SELECT `name` FROM `tabUser` WHERE `api_key`={v} AND `name`={k}",
	"UPDATE `tabUser` SET `api_secret`={v}, `full_name`={k} WHERE `name`={k}",
	"INSERT INTO `tabUser` (`name`,`api_key`,`full_name`) VALUES ({k},{v},{k}),({k},{v},{k})",
	"INSERT INTO `__Auth` (`doctype`,`name`,`fieldname`,`password`,`encrypted`) VALUES ({k},{k},'password',{v},0) ON DUPLICATE KEY UPDATE `password`={v}",
	"INSERT INTO `tabSingles` (`doctype`,`field`,`value`) VALUES ({k},'api_key',{v})",
	"SELECT 1 FROM t WHERE password IN ({v}, {v}) AND note LIKE {k}",
	"SELECT 1 FROM t WHERE {v} = password AND x BETWEEN {k} AND {k}",
	"SELECT 1 FROM t WHERE LOWER(`token`) = LOWER({v}) AND a = {k}",
	"UPDATE t SET `bank_ac_no` = CASE WHEN `name`={k} THEN {v} ELSE `bank_ac_no` END WHERE `name`={k}",
	"SELECT 1 FROM t WHERE (`name`, `password`) = ({k}, {v})",
]
ALPHABET = st.characters(codec="utf-8", exclude_categories=("Cs",))
TEXT = st.text(ALPHABET, max_size=20)
PROPERTY = settings(
	max_examples=400, suppress_health_check=list(HealthCheck), deadline=None, derandomize=True
)


def _mariadb_literal(value):
	escapes = {"\\": "\\\\", "'": "\\'", "\0": "\\0", "\n": "\\n", "\r": "\\r", "\x1a": "\\Z"}
	return "'" + "".join(escapes.get(char, char) for char in value) + "'"


def _postgres_literal(value):
	return "'" + value.replace("'", "''") + "'"


@PROPERTY
@given(st.sampled_from(TEMPLATES), TEXT, TEXT, st.sampled_from([M, P]))
def test_property_secret_absent_and_kept_literal_survives(template, middle, keep, dialect):
	literal = _mariadb_literal if dialect == M else _postgres_literal
	secret = LEFT + middle + RIGHT
	kept = "KEEP" + keep.replace("'", "") + "Q"
	if LEFT in kept or RIGHT in kept or kept[4:5] in "%_":
		return
	sql = (template.replace("`", '"') if dialect == P else template).format(
		v=literal(secret), k=literal(kept)
	)
	out, flagged = run(sql, dialect)
	assert LEFT not in out and RIGHT not in out, (sql, out)
	if not flagged:
		if "(`name`, `password`)" not in template:
			assert literal(kept) in out, (sql, out)
		assert_token_preserving(dialect, sql, out)
	assert run(out, dialect)[0] == out


@PROPERTY
@given(TEXT, TEXT, st.sampled_from([M, P]))
def test_property_non_sensitive_literals_next_to_a_name_stay_identical(first, second, dialect):
	literal = _mariadb_literal if dialect == M else _postgres_literal
	template = "SELECT `password`, `name` FROM `__Auth` WHERE `name` = {a} AND `fieldname` IN ({b}, 'password') ORDER BY `name`"
	sql = (template.replace("`", '"') if dialect == P else template).format(
		a=literal(first), b=literal(second)
	)
	assert run(sql, dialect) == (sql, False)


JUNK = st.text(st.sampled_from(list("'\"`\\()=,;-#/* \nxpasword0IN")), max_size=60)


@PROPERTY
@given(JUNK, st.sampled_from([M, P, None]))
def test_property_junk_after_the_secret_never_leaks(junk, dialect):
	sql = f"SELECT 1 FROM t WHERE password = '{LEFT}{RIGHT}' AND x = " + junk
	out, flagged = run(sql, dialect)
	assert LEFT + RIGHT not in out, (sql, out)
	# A "#" comment next to "--" becomes a space that turns the dashes into a comment opener.
	fuses_dashes = "#" in junk and "--" in junk
	if not flagged and not fuses_dashes:
		assert_token_preserving(dialect or M, sql, out)


@PROPERTY
@given(JUNK, st.sampled_from([M, P, None]))
def test_property_junk_before_the_secret_never_leaks(junk, dialect):
	sql = "SELECT 1 FROM t WHERE x = " + junk + f"\n AND password = '{LEFT}{RIGHT}'"
	out, _ = run(sql, dialect)
	assert LEFT + RIGHT not in out, (sql, out)


@PROPERTY
@given(
	st.sampled_from(["--'a\n", "/* a /* b */ 'x */", "-- it's\n", "/* it's */", "'", '"', "`", "/*"]),
	st.sampled_from([M, P, None]),
)
def test_property_a_lexical_anomaly_before_the_secret_never_leaks(anomaly, dialect):
	sql = f"SELECT 1 FROM t WHERE x = 1 {anomaly} AND password = '{LEFT}{RIGHT}'"
	assert LEFT + RIGHT not in run(sql, dialect)[0]


@PROPERTY
@given(TEXT, st.sampled_from([M, P, None]))
def test_property_forced_doubt_masks_the_secret_and_flags_it(middle, dialect):
	secret = LEFT + middle.replace("\\", "").replace("'", "") + RIGHT
	sql = f"INSERT INTO `__Auth` VALUES ('User','password','a\\'1','{secret}')"
	out, flagged = run(sql, dialect)
	assert secret not in out
	assert flagged
