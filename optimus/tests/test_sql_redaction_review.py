# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Review follow-ups of the SQL redactor: hyphenated names, ``name=value`` text inside string
literals, cut dollar quotes, stored-secret shapes, key-matching cost and the statement counters.
Pure, no Frappe."""

import time

import pytest

from optimus import redaction

M, P = "mariadb", "postgres"
S = "Zq9xK2vLm7Wp"


def out_of(sql, dialect=M, extras=()):
	return redaction.redact_sql_outcome(sql, dialect=dialect, extra_columns=extras)


def assert_masked(sql, secrets, dialect=M, extras=(), kept=()):
	out, _ = out_of(sql, dialect, extras)
	for secret in secrets:
		assert secret not in out, out
	for text in kept:
		assert text in out, out
	assert out_of(out, dialect, extras)[0] == out, "not idempotent"
	return out


# --- hyphenated and spaced names ---------------------------------------------------------------------


@pytest.mark.parametrize(
	"key", ["X-API-Key", "x-api-key", "api-key", "X-Auth-Token", "X-Secret", "Client-SecretKey"]
)
@pytest.mark.parametrize("dialect", [M, P])
def test_hyphenated_json_keys_are_masked(key, dialect):
	sql = f"""INSERT INTO t (name, headers) VALUES ('1', '{{"{key}": "{S}", "Accept": "KEEP1"}}')"""
	assert_masked(sql, [S], dialect)


@pytest.mark.parametrize("key", ["X-Secret", "X-API-Key", "api-key", "X-Auth-Token", "x secret"])
def test_hyphenated_settings_keys_mask_their_value(key):
	sql = f"INSERT INTO `tabWebhook Header` (name, `key`, `value`) VALUES ('1', '{key}', '{S}')"
	assert_masked(sql, [S])


@pytest.mark.parametrize("column", ["api-key", "Client-SecretKey"])
def test_hyphenated_backtick_column_is_a_name(column):
	assert_masked(f"SELECT 1 FROM t WHERE `{column}` = '{S}' AND a = 'KEEP1'", [S], kept=["KEEP1"])


def test_hyphenated_extra_name_matches_hyphen_and_underscore_forms():
	assert_masked(f"SELECT 1 FROM t WHERE my_custom_thing = '{S}'", [S], extras=("my-custom-thing",))


@pytest.mark.parametrize("column", ["PassWord", "PASSWORD", "AccessToken", "ApiKey", "Api_Key"])
def test_odd_capitalisation_of_a_default_name_is_still_the_name(column):
	assert_masked(f"SELECT 1 FROM t WHERE {column}='{S}' AND a='KEEP1'", [S], kept=["KEEP1"])


# --- name=value text inside string literals ----------------------------------------------------------

TEXTS = [
	f"password={S}&client_secret={S}b&user=KEEP1",
	f"https://x.example/cb?api_key={S}&state=KEEP1",
	f"https://x.example/cb?token={S}",
	f"PassWord={S} user=KEEP1",
	f"card_number={S} cvv={S}b ok=KEEP1",
	f"Api-Key={S}; Path=/",
	f"X-API-Key: ignored KEEP1 accessToken={S}",
	f'password="{S} x" user=KEEP1',
]


@pytest.mark.parametrize("text", TEXTS)
@pytest.mark.parametrize("dialect", [M, P])
def test_name_value_text_in_a_string_literal_is_masked(text, dialect):
	sql = f"INSERT INTO `tabEmail Queue` (name, message) VALUES ('a', '{text}')"
	out = assert_masked(sql, [S, S + "b"], dialect)
	if "KEEP1" in text:
		assert "KEEP1" in out


def test_name_value_text_in_an_update_and_a_like_filter():
	assert_masked(
		f"UPDATE `tabEmail Queue` SET message='go?token={S}&a=KEEP1' WHERE name='1'", [S], kept=["KEEP1"]
	)
	assert_masked(f"SELECT 1 FROM t WHERE data LIKE '%PassWord={S}%'", [S])


def test_plain_text_without_a_name_is_untouched():
	sql = "UPDATE t SET message='order=KEEP1&page=2' WHERE a = 3 AND token IS NULL"
	assert out_of(sql)[0] == sql


# --- cut dollar quotes -------------------------------------------------------------------------------


@pytest.mark.parametrize("quote", ["$$", "$tag$"])
def test_a_cut_dollar_quote_is_omitted_not_returned_raw(quote):
	sql = f"SELECT 1 WHERE password = {quote}{S}"
	out, masked = out_of(sql, P)
	assert S not in out and masked


def test_closed_dollar_quote_and_numbered_placeholder_still_read():
	sql = f"SELECT 1 WHERE password = $$ {S} $$ AND a = $1"
	out, masked = out_of(sql, P)
	assert S not in out and not masked and "$1" in out


# --- stored-secret shapes anywhere in a literal ------------------------------------------------------

HASHES = [
	"$pbkdf2-sha256$29000$" + S + "abcdef",
	"pbkdf2:sha256:600000$" + S + "$abcdef0123456789",
	"$argon2id$v=19$m=65536,t=3,p=4$" + S + "$abcdef",
	"$2b$12$" + "a" * 53,
	"gAAAAAB" + "x" * 95,
]


@pytest.mark.parametrize("hash_text", HASHES)
def test_a_stored_secret_shape_is_masked_inside_a_longer_literal(hash_text):
	sql = f"SELECT 1 FROM t WHERE note = 'copied {hash_text} done' AND a = 'KEEP1'"
	out, _ = out_of(sql, M)
	assert hash_text not in out and "KEEP1" in out
	assert out_of(out, M)[0] == out


# --- join keyword after a masked value ---------------------------------------------------------------


@pytest.mark.parametrize("join", ["LEFT JOIN", "RIGHT JOIN", "INNER JOIN", "LEFT OUTER JOIN", "CROSS JOIN"])
def test_a_masked_value_keeps_the_following_join_keyword(join):
	sql = f"SELECT 1 FROM a LEFT JOIN b ON b.token = '{S}' {join} c ON c.x = 1"
	out = assert_masked(sql, [S])
	assert f"'<REDACTED>' {join} c" in out


def test_left_function_is_not_a_join():
	sql = f"SELECT LEFT(password, 2) FROM t WHERE token = '{S}' AND LEFT(a, 1) = 'KEEP1'"
	assert_masked(sql, [S], kept=["LEFT(a, 1)", "KEEP1"])


# --- key matching cost -------------------------------------------------------------------------------


def test_distinct_long_keys_do_not_stall():
	rows = ",".join(f"('{'a_' * 120}{i}', 'x')" for i in range(900))
	sql = f"INSERT INTO `tabSingles` (`field`, `value`) VALUES {rows}"
	start = time.perf_counter()
	out_of(sql)
	assert time.perf_counter() - start < 0.5


def test_distinct_keys_with_a_name_among_many_parts_do_not_stall():
	rows = ",".join(f"('{'a_' * 30}password_{i}', 'x')" for i in range(1800))
	sql = f"INSERT INTO `tabSingles` (`field`, `value`) VALUES {rows}"
	start = time.perf_counter()
	out_of(sql)
	assert time.perf_counter() - start < 1.0


def test_is_key_is_memoised_for_a_repeated_key():
	names = redaction._names(())
	names.is_key("a_b_c")
	assert "a_b_c" in names._key_memo


@pytest.mark.parametrize(
	("text", "expected"),
	[
		("a@b.c_otpsecret", True),
		("user@x.com_password", True),
		("a_" * 40 + "password", True),
		("password_a_a_a_a_a", True),
		("show_sidebar", False),
		("X-Secret", True),
		("title", False),
	],
)
def test_is_key_keeps_its_meaning(text, expected):
	assert redaction._names(()).is_key(text) is expected


# --- counters ----------------------------------------------------------------------------------------


def test_counter_counts_masked_and_omitted_statements():
	calls = [
		{"query": f"SELECT 1 WHERE password = '{S}' AND (a"},
		{"query": "SELECT 1 WHERE password = 'x"},
		{"query": f"SELECT 1 WHERE password = '{S}'"},
		{"query": "SELECT 1"},
	]
	counts = redaction.redact_call_queries(calls)
	assert counts == {"statements": 4, "masked_whole": 1, "omitted": 1}
