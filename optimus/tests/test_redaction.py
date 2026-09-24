# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Pure-function tests for ``optimus/redaction.py``.

These cover the relocated render-time helpers (now also called from
the recorder patch at capture time). The tests don't depend on Frappe;
they exercise the regex + substring patterns directly so the same code
is provably equivalent in both call paths.
"""

import pytest

from optimus import redaction

# ---------------------------------------------------------------------------
# is_sensitive_key + redact_sensitive
# ---------------------------------------------------------------------------


class TestIsSensitiveKey:
	def test_default_patterns_match_case_insensitively(self):
		for key in (
			"password", "Password", "PWD", "api_key", "apiKey", "TOKEN",
			"secret_key", "CSRF", "Authorization", "Cookie",
			"encryption_key", "PRIVATE_KEY", "session_id",
		):
			assert redaction.is_sensitive_key(key), f"{key!r} should be flagged sensitive"

	def test_non_string_input_is_not_sensitive(self):
		for key in (None, 0, [], {}, b"password"):
			assert redaction.is_sensitive_key(key) is False

	def test_extra_keys_extend_defaults(self):
		assert redaction.is_sensitive_key("recovery_code", extra=("recovery_code",))
		# Default still matches even when extras supplied.
		assert redaction.is_sensitive_key("password", extra=("recovery_code",))

	def test_benign_keys_pass_through(self):
		# Note: substring match means "csrf_disabled_flag" WOULD match
		# ("csrf" substring); the only safe benign keys are those whose
		# names share NO substring with any sensitive pattern.
		for key in ("name", "email", "title", "user", "enabled", "created_at"):
			assert redaction.is_sensitive_key(key) is False, f"{key!r} unexpectedly flagged"


class TestRedactSensitive:
	def test_flat_dict_redacts_default_keys(self):
		out = redaction.redact_sensitive({"password": "hunter2", "name": "Alice"})
		assert out == {"password": "<REDACTED:password>", "name": "Alice"}

	def test_nested_dict_and_list_are_walked(self):
		payload = {
			"form": {"username": "alice", "password": "hunter2"},
			"headers": [{"Authorization": "Bearer xyz"}, {"X-Custom": "ok"}],
		}
		out = redaction.redact_sensitive(payload)
		assert out["form"]["password"] == "<REDACTED:password>"
		assert out["form"]["username"] == "alice"
		assert out["headers"][0]["Authorization"] == "<REDACTED:Authorization>"
		assert out["headers"][1]["X-Custom"] == "ok"

	def test_extra_keys_extend_redaction(self):
		out = redaction.redact_sensitive(
			{"recovery_code": "abc123", "name": "Alice"},
			extra_keys=("recovery_code",),
		)
		assert out["recovery_code"] == "<REDACTED:recovery_code>"
		assert out["name"] == "Alice"

	def test_extra_keys_are_additive_not_replacement(self):
		"""A customer config that adds ``recovery_code`` must not
		accidentally stop redacting ``password``."""
		out = redaction.redact_sensitive(
			{"password": "x", "recovery_code": "y"},
			extra_keys=("recovery_code",),
		)
		assert out["password"] == "<REDACTED:password>"
		assert out["recovery_code"] == "<REDACTED:recovery_code>"

	def test_scalar_passes_through(self):
		assert redaction.redact_sensitive("hello") == "hello"
		assert redaction.redact_sensitive(42) == 42
		assert redaction.redact_sensitive(None) is None

	def test_input_is_not_mutated(self):
		original = {"password": "x", "name": "Alice"}
		out = redaction.redact_sensitive(original)
		assert original == {"password": "x", "name": "Alice"}, "input must be unchanged"
		assert out is not original


# ---------------------------------------------------------------------------
# redact_sql_literals + redact_call_queries
# ---------------------------------------------------------------------------


class TestRedactSqlLiterals:
	def test_default_columns_redact_equality_literal(self):
		out = redaction.redact_sql_literals(
			"SELECT * FROM `tabUser` WHERE password = 'hunter2'"
		)
		assert "hunter2" not in out
		assert "'<REDACTED>'" in out

	def test_redacts_like_and_in(self):
		assert "'<REDACTED>'" in redaction.redact_sql_literals(
			"SELECT 1 WHERE api_key LIKE 'sk-%'"
		)
		assert "'<REDACTED>'" in redaction.redact_sql_literals(
			"SELECT 1 WHERE token IN ('a', 'b', 'c')"
		)

	def test_redacts_double_quoted_literal(self):
		out = redaction.redact_sql_literals('UPDATE x SET secret = "shh" WHERE id = 1')
		assert "shh" not in out

	def test_non_sensitive_query_passes_through(self):
		query = "SELECT name, email FROM `tabUser` WHERE enabled = 1 ORDER BY name"
		assert redaction.redact_sql_literals(query) == query

	def test_empty_or_invalid_input(self):
		assert redaction.redact_sql_literals("") == ""
		assert redaction.redact_sql_literals(None) == ""
		# Non-string passes through too (we type-check defensively).
		assert redaction.redact_sql_literals(42) == 42 or redaction.redact_sql_literals(42) == ""

	def test_extra_columns_extend_redaction(self):
		query = "SELECT * FROM acc WHERE bank_account = '1234567890'"
		# Default: bank_account NOT in defaults → no redaction.
		assert "1234567890" in redaction.redact_sql_literals(query)
		# With extra: redacted.
		out = redaction.redact_sql_literals(query, extra_columns=("bank_account",))
		assert "1234567890" not in out
		assert "'<REDACTED>'" in out

	def test_extra_columns_are_additive(self):
		"""Custom config can't accidentally turn off the default password redaction."""
		query = "SELECT * FROM u WHERE password='x' AND bank_account='y'"
		out = redaction.redact_sql_literals(query, extra_columns=("bank_account",))
		assert "'x'" not in out
		assert "'y'" not in out


class TestRedactCallQueries:
	def test_mutates_calls_list_in_place(self):
		calls = [
			{"query": "SELECT 1 WHERE password='x'", "duration": 1.0},
			{"normalized_query": "WHERE token=?", "duration": 2.0},
		]
		redaction.redact_call_queries(calls)
		assert "'x'" not in calls[0]["query"]
		# Non-sensitive normalized_query (with placeholder, not literal) passes through.
		assert calls[1]["normalized_query"] == "WHERE token=?"

	def test_ignores_non_dict_entries(self):
		# Must not crash on malformed calls (e.g. mid-deploy garbage in Redis).
		redaction.redact_call_queries([None, "not a dict", {"query": "ok"}])

	def test_ignores_non_list_input(self):
		# Must not crash on dict-as-calls (defensive).
		redaction.redact_call_queries({"not": "a list"})
		redaction.redact_call_queries(None)


# ---------------------------------------------------------------------------
# scrub_secrets (PR-0a): API keys in log text
# ---------------------------------------------------------------------------

_TOK = "sk-live-0123456789abcdefXYZ"


class TestScrubSecrets:
	def test_masks_the_three_leak_shapes_seen_in_real_rows(self):
		cases = [
			# headers dict from _call_openai_chat / _http_post frames
			f"headers = {{'content-type': 'application/json', 'authorization': 'Bearer {_TOK}'}}",
			# provider dict (api_key is not the exact name Frappe's printer redacts)
			f"provider = {{'name': 'OpenAI', 'api_key': '{_TOK}', 'model': 'm'}}",
			# anthropic header
			f"headers = {{'x-api-key': '{_TOK}', 'anthropic-version': '2023-06-01'}}",
			# JSON-style double quotes
			f'{{"Authorization": "Bearer {_TOK}"}}',
			# bare Bearer in an exception message
			f"401 for Bearer {_TOK} at /v1/chat/completions",
			f"'authorization': 'Basic {_TOK}'",
		]
		for text in cases:
			out = redaction.scrub_secrets(text)
			assert _TOK not in out, text
			assert "********" in out

	def test_keeps_surrounding_structure(self):
		out = redaction.scrub_secrets(f"provider = {{'api_key': '{_TOK}', 'model': 'm'}}")
		assert out == "provider = {'api_key': '********', 'model': 'm'}"

	def test_leaves_empty_key_fields_and_plain_text_alone(self):
		for text in ("provider = {'api_key': ''}", "no secrets here", "Bearer short"):
			assert redaction.scrub_secrets(text) == text

	def test_literal_replacement(self):
		assert redaction.scrub_secrets(f"echoed {_TOK} back", literals=(_TOK,)) == "echoed ******** back"

	def test_short_or_empty_literals_are_ignored(self):
		# A test/misconfigured key like "k" must not shred ordinary words.
		assert redaction.scrub_secrets("keep kittens", literals=("k", "", None)) == "keep kittens"

	def test_a_literal_of_exactly_the_minimum_length_is_masked(self):
		# The boundary: an 8-character literal is replaced, a 7-character one is not.
		assert redaction.scrub_secrets("key=abcdefgh end", literals=("abcdefgh",)) == "key=******** end"
		assert redaction.scrub_secrets("key=abcdefg end", literals=("abcdefg",)) == "key=abcdefg end"

	def test_an_x_goog_api_key_entry_is_masked(self):
		text = "headers = {'content-type': 'application/json', 'x-goog-api-key': 'AIzaSyD-0123456789abcdef'}"
		assert redaction.scrub_secrets(text) == (
			"headers = {'content-type': 'application/json', 'x-goog-api-key': '********'}"
		)

	def test_idempotent(self):
		text = (
			f"headers = {{'authorization': 'Bearer {_TOK}'}}\n"
			f"provider = {{'api_key': '{_TOK}'}}\nBearer {_TOK}"
		)
		once = redaction.scrub_secrets(text, literals=(_TOK,))
		assert redaction.scrub_secrets(once, literals=(_TOK,)) == once

	def test_non_string_passthrough(self):
		assert redaction.scrub_secrets(None) is None
		assert redaction.scrub_secrets("") == ""

	def test_a_smart_quote_in_or_before_the_key_is_masked_whole(self):
		# Frame-local lines Frappe prints for the requests / http.client frames
		# when a pasted smart quote made the header unencodable (observed).
		mid = "sk-live-0123\u2019456789abcdef"
		lead = "\u2019sk-proj-0123456789abcdef\u2019"
		for text in (f"      value = 'Bearer {mid}'", f"      one_value = 'Bearer {lead}'"):
			out = redaction.scrub_secrets(text)
			assert out.endswith("= 'Bearer ********'"), out
			assert "456789abcdef" not in out

	def test_url_userinfo_is_masked(self):
		text = "POST http://user:s3cret-pass@10.0.0.5:11434/v1/chat/completions failed"
		out = redaction.scrub_secrets(text)
		assert out == "POST http://********@10.0.0.5:11434/v1/chat/completions failed"
		assert redaction.scrub_secrets(out) == out
		# a raw "@" in the password: masked up to the last "@" before the path
		raw_at = redaction.scrub_secrets("http://user:p@ss@w0rd@10.0.0.5/v1")
		assert raw_at == "http://********@10.0.0.5/v1"
		assert redaction.scrub_secrets(raw_at) == raw_at
		for plain in (
			"https://api.openai.com/v1", "https://github.com/frappe@v16", "mailto:a@b.example",
			'{"url":"http://x","owner":"a@b.example"}', "url = 'http://host',owner@example.com",
		):
			assert redaction.scrub_secrets(plain) == plain

	def test_json_escaped_text_stays_valid_json(self):
		# Deleted Document data is JSON: the smart quote is escaped as \u2019
		# and a JSON-style header dump escapes its double quotes.
		import json

		doc = {
			"error": "      value = 'Bearer sk-live-0123\u2019456789abcdef'\n next line",
			"method": 'dump {"Authorization": "Bearer sk-live-0123456789abcdefXYZ"} end',
		}
		out = redaction.scrub_secrets(json.dumps(doc))
		assert json.loads(out) == {
			"error": "      value = 'Bearer ********'\n next line",
			"method": 'dump {"Authorization": "Bearer ********"} end',
		}

	def test_the_key_sits_only_in_locals_the_log_formatters_redact(self, monkeypatch):
		# Frappe's traceback sanitizer redacts local names containing "secret"
		# or "key"; Sentry's denylist has "secret" and "api_key". If a pattern
		# ever raised, the with-context traceback of this frame must not print
		# the key under any other name (``literals`` is deleted first).
		class _Boom:
			def sub(self, *a, **k):
				raise RuntimeError("boom")

		monkeypatch.setattr(redaction, "_SECRET_PATTERNS", (_Boom(),))
		with pytest.raises(RuntimeError) as ei:
			redaction.scrub_secrets("text without it", literals=(_TOK,))
		tb = ei.value.__traceback__
		while tb.tb_frame.f_code.co_name != "scrub_secrets":
			tb = tb.tb_next
		holders = {name for name, value in tb.tb_frame.f_locals.items() if _TOK in repr(value)}
		assert holders == {"secret", "api_key"}
