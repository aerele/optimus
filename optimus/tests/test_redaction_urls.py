# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Header-name and URL-query secrets: ``is_sensitive_key``'s whole-name rule,
``redact_url``, the URL pass inside ``redact_sensitive``, the bare ``key``
parameter on key-bearing endpoints and the query-parameter rule of
``scrub_secrets``. Pure functions, no Frappe."""

import json

import pytest

from optimus import redaction

REDACTED = "<REDACTED>"


class TestHeaderNames:
	@pytest.mark.parametrize(
		"name",
		[
			"X-Api-Key",
			"api-key",
			"x-api-key",
			"X-Access-Key",
			"x-private-key",
			"X-Session-Id",
			"sessionId",
			"accessKey",
			"privateKey",
			"sid",
			"SID",
			"X-Frappe-CSRF-Token",
			"X-Auth-Token",
		],
	)
	def test_secret_header_and_key_names_are_sensitive(self, name):
		assert redaction.is_sensitive_key(name)

	@pytest.mark.parametrize(
		"name",
		[
			"Content-Type",
			"Accept",
			"Origin",
			"Referer",
			"X-Frappe-CMD",
			"Host",
			"User-Agent",
			"key",
			"inside",
			"consider",
			"doctype",
			"X-Requested-With",
			"signature",
		],
	)
	def test_ordinary_names_stay_visible(self, name):
		assert not redaction.is_sensitive_key(name)

	def test_user_extras_still_apply(self):
		assert redaction.is_sensitive_key("X-Custom-Thing", extra=("x-custom-thing",))

	def test_headers_are_masked_by_name(self):
		headers = {"X-Api-Key": "v1", "Content-Type": "text/html", "sid": "s", "X-Frappe-CMD": "a.b"}
		out = redaction.redact_sensitive(headers)
		assert out["X-Api-Key"] == "<REDACTED:X-Api-Key>"
		assert out["sid"] == "<REDACTED:sid>"
		assert out["Content-Type"] == "text/html"
		assert out["X-Frappe-CMD"] == "a.b"

	def test_hyphenated_json_key_is_masked(self):
		out = redaction.redact_sensitive({"args": json.dumps({"api-key": "v", "doctype": "User"})})
		assert "v" not in json.loads(out["args"]).get("api-key", "")
		assert json.loads(out["args"])["doctype"] == "User"

	def test_overlong_key_does_not_slow_or_raise(self):
		assert redaction.is_sensitive_key("a" * 100_000) is False


class TestRedactUrl:
	def test_sensitive_values_are_replaced_in_place(self):
		url = "https://s/app?api_key=abc&doctype=User&token=xyz"
		assert redaction.redact_url(url) == f"https://s/app?api_key={REDACTED}&doctype=User&token={REDACTED}"

	@pytest.mark.parametrize(
		"name", ["sid", "x-api-key", "accessKey", "key", "_signature", "signature", "password", "Token"]
	)
	def test_each_shared_parameter_is_masked(self, name):
		assert redaction.redact_url(f"/a?{name}=secret1") == f"/a?{name}={REDACTED}"

	def test_encoded_names_and_values(self):
		assert redaction.redact_url("/a?api%5Fkey=ab%20c&x=1") == f"/a?api%5Fkey={REDACTED}&x=1"
		assert redaction.redact_url("/a?access%2Dkey=v") == f"/a?access%2Dkey={REDACTED}"

	def test_repeated_keys_are_all_masked(self):
		assert redaction.redact_url("/a?token=1&x=2&token=3") == f"/a?token={REDACTED}&x=2&token={REDACTED}"

	def test_fragment_params_and_route_fragments(self):
		assert redaction.redact_url("/a#access_token=abc&x=1") == f"/a#access_token={REDACTED}&x=1"
		assert redaction.redact_url("/a#/route?sid=abc") == f"/a#/route?sid={REDACTED}"
		assert redaction.redact_url("/a#section-2") == "/a#section-2"

	def test_non_sensitive_urls_come_back_byte_for_byte(self):
		for url in ("/a?b", "/a?", "/a?x=a+b%20c&y=", "https://h/p?x=1&&y=2#top", "/a?x=1;y=2", "/plain"):
			assert redaction.redact_url(url) == url

	def test_relative_and_malformed_urls(self):
		assert redaction.redact_url("?token=abc") == f"?token={REDACTED}"
		assert redaction.redact_url("http://[::1/p?token=abc") == f"http://[::1/p?token={REDACTED}"
		assert redaction.redact_url("/a?=x&&=&token") == "/a?=x&&=&token"

	def test_json_valued_parameter_is_masked_inside(self):
		url = "/api?filters=%7B%22api_key%22%3A%22abc%22%2C%22doctype%22%3A%22User%22%7D"
		out = redaction.redact_url(url)
		assert "abc" not in out
		assert "doctype" in out

	def test_idempotent(self):
		url = "https://s/app?api_key=abc&token=x#sid=1&a=b"
		once = redaction.redact_url(url)
		assert redaction.redact_url(once) == once
		scrub = redaction.redact_url(url, placeholder="********")
		assert redaction.redact_url(scrub, placeholder="********") == scrub

	def test_very_long_query_is_omitted_not_scanned(self):
		url = "/a?" + "x=1&" * 5000 + "token=abc"
		out = redaction.redact_url(url)
		assert out == "/a?<query omitted>"

	def test_very_long_url_without_a_query_is_untouched(self):
		url = "/" + "p" * 20_000
		assert redaction.redact_url(url) == url

	def test_hostile_input_never_raises(self):
		for value in (None, 7, b"x", ["a"], "", "?", "#", "=", "?=", "&&&", "%", "?a=%zz&token=%"):
			redaction.redact_url(value)

	def test_extra_keys_apply(self):
		assert redaction.redact_url("/a?my_pin=1", ("my_pin",)) == f"/a?my_pin={REDACTED}"

	def test_name_that_is_absurdly_long_is_masked(self):
		assert redaction.redact_url("/a?" + "n" * 300 + "=v") == f"/a?{'n' * 300}={REDACTED}"

	def test_a_name_cannot_hide_a_secret_behind_surrounding_spaces(self):
		assert redaction.redact_url("/a?%20token%20=abc") == f"/a?%20token%20={REDACTED}"


class TestRedactSensitiveUrlPass:
	def test_referer_and_origin_values_are_masked(self):
		out = redaction.redact_sensitive(
			{"Referer": "https://s/app?api_key=abc&token=x", "Origin": "https://s"}
		)
		assert out["Referer"] == f"https://s/app?api_key={REDACTED}&token={REDACTED}"
		assert out["Origin"] == "https://s"

	def test_form_dict_url_values_and_nested_values(self):
		out = redaction.redact_sensitive({"redirect_to": "/app?sid=1", "x": [{"u": "/a?token=1"}]})
		assert out["redirect_to"] == f"/app?sid={REDACTED}"
		assert out["x"][0]["u"] == f"/a?token={REDACTED}"

	def test_prose_and_sql_strings_are_left_alone(self):
		for text in (
			"select * from t where a = 1?b=2",
			"what is this? a=b or not",
			"key=value",
			"a#b=c d",
		):
			assert redaction.redact_sensitive({"v": text})["v"] == text

	def test_json_strings_keep_their_own_path(self):
		text = json.dumps({"url": "/a?x=1"})
		assert redaction.redact_sensitive({"doc": text})["doc"] == text

	def test_non_string_scalars_pass_through(self):
		assert redaction.redact_sensitive({"a": 1, "b": None, "c": 1.5}) == {"a": 1, "b": None, "c": 1.5}

	def test_input_is_not_mutated(self):
		src = {"Referer": "/a?token=1"}
		redaction.redact_sensitive(src)
		assert src == {"Referer": "/a?token=1"}


class TestBareKeyOnKeyBearingEndpoints:
	@pytest.mark.parametrize(
		("path", "cmd"),
		[
			("/update-password", ""),
			("/printview", ""),
			(
				"/api/method/frappe.core.doctype.user.user.update_password",
				"frappe.core.doctype.user.user.update_password",
			),
			("/api/method/frappe.utils.print_format.download_pdf", "frappe.utils.print_format.download_pdf"),
			("/", "frappe.core.doctype.user.user.test_password_strength"),
		],
	)
	def test_endpoints_are_key_bearing(self, path, cmd):
		assert redaction.is_key_bearing_endpoint(path, cmd)

	@pytest.mark.parametrize(
		("path", "cmd"),
		[
			("/api/method/frappe.client.get_list", "frappe.client.get_list"),
			("/app/user", ""),
			(None, None),
			(7, []),
		],
	)
	def test_other_endpoints_are_not(self, path, cmd):
		assert not redaction.is_key_bearing_endpoint(path, cmd)

	def test_bare_key_is_masked_only_when_asked(self):
		form = {"key": "reset123", "new_password": "p", "doctype": "User"}
		masked = redaction.redact_sensitive(form, mask_key=True)
		assert masked["key"] == "<REDACTED:key>"
		assert masked["doctype"] == "User"
		assert redaction.redact_sensitive(form)["key"] == "reset123"

	def test_only_the_top_level_key_is_masked(self):
		out = redaction.redact_sensitive({"filters": {"key": "v"}}, mask_key=True)
		assert out["filters"]["key"] == "v"

	def test_bare_key_in_a_url_is_masked_everywhere(self):
		assert (
			redaction.redact_url("/printview?doctype=X&key=share1") == f"/printview?doctype=X&key={REDACTED}"
		)
		assert redaction.redact_sensitive({"Referer": "/a?key=k1"})["Referer"] == f"/a?key={REDACTED}"


class TestScrubSecretsQueryParameters:
	def test_url_query_values_are_masked_with_the_scrub_placeholder(self):
		text = "GET https://h/p?api_key=abcdef&x=1&token=t0k failed"
		assert redaction.scrub_secrets(text) == "GET https://h/p?api_key=********&x=1&token=******** failed"

	def test_one_rule_for_every_parameter(self):
		for name in ("key", "sid", "_signature", "access_token", "x-api-key", "secret"):
			assert (
				redaction.scrub_secrets(f"see https://h/p?{name}=value1")
				== f"see https://h/p?{name}=********"
			)

	def test_idempotent(self):
		text = "https://h/p?key=abc&sid=1#token=x"
		once = redaction.scrub_secrets(text)
		assert redaction.scrub_secrets(once) == once

	def test_prose_without_a_url_is_left_alone(self):
		for text in ("pass ?key=value in the query", "set key=value and sid=1", "a?token=1"):
			assert redaction.scrub_secrets(text) == text

	def test_non_secret_parameters_stay(self):
		text = "https://h/p?doctype=User&name=x"
		assert redaction.scrub_secrets(text) == text

	def test_credentials_in_the_authority_are_still_masked(self):
		assert redaction.scrub_secrets("https://u:pw@h/p?token=abc") == "https://********@h/p?token=********"

	def test_long_url_is_bounded(self):
		text = "https://h/p?" + "a=1&" * 100_000
		assert len(redaction.scrub_secrets(text[: redaction.SCRUB_TEXT_CAP])) < redaction.SCRUB_TEXT_CAP + 100


class TestUrlOnlyNames:
	@pytest.mark.parametrize(
		"name",
		[
			"X-Amz-Signature",
			"X-Goog-Signature",
			"X-Amz-Credential",
			"X-Amz-Security-Token",
			"sig",
			"code",
			"jwt",
			"session",
			"reset_key",
			"auth",
			"bearer",
			"pin",
			"refresh",
			"oauth_signature",
			"x_sig",
		],
	)
	def test_masked_in_urls(self, name):
		assert redaction.redact_url(f"/a?{name}=S") == f"/a?{name}={REDACTED}"

	@pytest.mark.parametrize(
		"name", ["item_code", "code_name", "session_user", "doctype", "pincode", "authority"]
	)
	def test_lookalikes_stay_visible_in_urls(self, name):
		assert redaction.redact_url(f"/a?{name}=x") == f"/a?{name}=x"

	@pytest.mark.parametrize("name", ["code", "session", "sig", "pin", "auth", "refresh", "reset_key"])
	def test_not_sensitive_as_form_or_header_names(self, name):
		assert not redaction.is_sensitive_key(name)


class TestMoreHeaderNames:
	@pytest.mark.parametrize(
		"name",
		[
			"Cf-Access-Jwt-Assertion",
			"jwt",
			"X-Session",
			"X-Signature",
			"X-Hub-Signature-256",
			"Stripe-Signature",
			"X-Slack-Signature",
			"WWW-Authenticate",
			"Proxy-Authenticate",
			"Authentication",
			"Sec-WebSocket-Key",
		],
	)
	def test_masked(self, name):
		assert redaction.is_sensitive_key(name)

	@pytest.mark.parametrize(
		"name",
		[
			"Host",
			"Keep-Alive",
			"Sec-Fetch-Site",
			"Content-Type",
			"Accept",
			"Origin",
			"Referer",
			"X-Frappe-CMD",
			"signature",
		],
	)
	def test_visible(self, name):
		assert not redaction.is_sensitive_key(name)


class TestTokenisedUrlPass:
	def test_urls_inside_text_are_masked(self):
		out = redaction.redact_sensitive({"v": "see /a?x=1&token=S end", "r": "Referer: /a?token=S"})
		assert out == {"v": f"see /a?x=1&token={REDACTED} end", "r": f"Referer: /a?token={REDACTED}"}

	def test_whitespace_is_preserved(self):
		text = "a  /p?token=S\n\tb"
		assert redaction.redact_sensitive({"v": text})["v"] == f"a  /p?token={REDACTED}\n\tb"

	def test_prose_without_secret_params_is_untouched(self):
		for text in (
			"what is this? a=b or not",
			"x = 1?b=2 and y",
			"see /a?doctype=X for details",
			"a#b=c d",
		):
			assert redaction.redact_sensitive({"v": text})["v"] == text

	def test_malformed_json_looking_text_falls_through(self):
		out = redaction.redact_sensitive({"v": "{broken /a?token=S"})["v"]
		assert "S" not in out.replace("<REDACTED>", "").replace("{broken", "").replace("/a?token=", "")
		assert out == f"{{broken /a?token={REDACTED}"

	def test_valid_json_unchanged_stays_unchanged(self):
		text = json.dumps({"u": "/a?x=1", "n": 2})
		assert redaction.redact_sensitive({"v": text})["v"] == text

	def test_idempotent(self):
		once = redaction.redact_sensitive({"v": "see /a?x=1&token=S end"})
		assert redaction.redact_sensitive(once) == once

	def test_oversize_text_is_still_masked(self):
		text = "a /p?token=S " + "word " * 300_000
		out = redaction.redact_sensitive({"v": text})["v"]
		assert out == f"a /p?token={REDACTED} " + "word " * 300_000

	def test_prose_skips_the_word_walk(self, monkeypatch):
		calls = []
		real = redaction._may_hold_url
		monkeypatch.setattr(redaction, "_may_hold_url", lambda t: calls.append(1) or real(t))
		text = "what is this? " + "plain words here " * 1000 + " a = b"
		assert redaction.redact_sensitive({"v": text})["v"] == text
		assert len(calls) < 10


class TestParamSeparatorsAndEncodings:
	def test_semicolon_separates_parameters(self):
		assert redaction.redact_url("/a?x=1;token=S;y=2") == f"/a?x=1;token={REDACTED};y=2"

	def test_encoded_equals_sign_is_checked(self):
		assert redaction.redact_url("/a?token%3DS&x=1") == f"/a?token={REDACTED}&x=1"
		assert redaction.redact_url("/a?doctype%3DUser&x=1") == "/a?doctype%3DUser&x=1"

	def test_double_encoded_name(self):
		assert redaction.redact_url("/a?api%255Fkey=S") == f"/a?api%255Fkey={REDACTED}"

	def test_json_value_with_lowercase_hex_and_plus(self):
		for value in (
			"%7b%22api_key%22%3a%22abc%22%7d",
			"%7B%22api_key%22%3A+%22abc%22%7D",
			"{%22api_key%22:%22abc%22}",
		):
			out = redaction.redact_url(f"/a?filters={value}")
			assert "abc" not in out, out
			assert redaction.redact_url(out) == out


class TestUserinfo:
	def test_credentials_in_the_authority_are_masked(self):
		assert redaction.redact_url("https://user:pass@host/p?x=1") == f"https://{REDACTED}@host/p?x=1"
		assert redaction.redact_url("//user:pass@host/p") == f"//{REDACTED}@host/p"
		assert redaction.redact_url("https://u:p@h") == f"https://{REDACTED}@h"

	def test_address_in_a_query_is_not_userinfo(self):
		for url in ("/a?to=a@b.com", "https://h/p?to=a@b.com", "https://h/p@q", "mailto:a@b.com"):
			assert redaction.redact_url(url) == url

	def test_idempotent(self):
		once = redaction.redact_url("https://u:p@h/p?token=1")
		assert redaction.redact_url(once) == once

	def test_a_password_with_an_at_sign(self):
		assert redaction.redact_url("https://u:p@ss@h/x") == f"https://{REDACTED}@h/x"


class TestBareKeyIsExact:
	def test_lookalike_names_stay_visible_on_a_key_bearing_endpoint(self):
		form = {"keyword": "a", "key_name": "b", "Key": "c", "monkey": "d"}
		out = redaction.redact_sensitive(form, mask_key=True)
		assert out["keyword"] == "a"
		assert out["key_name"] == "b"
		assert out["monkey"] == "d"
		assert out["Key"] == "<REDACTED:Key>"


class TestStringBranchEdges:
	def test_userinfo_without_a_query_is_masked_in_a_value(self):
		assert redaction.redact_sensitive({"v": "https://u:p@h/x"})["v"] == f"https://{REDACTED}@h/x"
		assert (
			redaction.redact_sensitive({"v": "go to https://u:p@h/x now"})["v"]
			== f"go to https://{REDACTED}@h/x now"
		)

	def test_a_huge_url_without_whitespace_has_its_query_omitted(self):
		text = "/p?token=S" + "a" * 1_100_000
		assert redaction.redact_sensitive({"v": text})["v"] == "/p?<query omitted>"


class TestFilterRows:
	ROW4 = '[["User","password","=","S8"]]'

	def test_form_json_filter_row_value_is_masked(self):
		out = redaction.redact_sensitive({"filters": self.ROW4})["filters"]
		assert "S8" not in out
		assert json.loads(out) == [["User", "password", "=", "<REDACTED:password>"]]

	def test_three_item_row_and_nested_lists(self):
		out = json.loads(redaction.redact_sensitive({"f": '[["api_key","=","S8"]]'})["f"])
		assert out == [["api_key", "=", "<REDACTED:api_key>"]]
		out = json.loads(
			redaction.redact_sensitive({"f": '{"filters": [["User","token","like","S8"]]}'})["f"]
		)
		assert out["filters"] == [["User", "token", "like", "<REDACTED:token>"]]

	def test_url_filter_row_value_is_masked(self):
		url = "/a?filters=%5B%5B%22User%22%2C%22password%22%2C%22%3D%22%2C%22S8%22%5D%5D"
		out = redaction.redact_url(url)
		assert "S8" not in out
		assert redaction.redact_url(out) == out

	def test_dict_form_filters_are_masked_by_key(self):
		out = redaction.redact_sensitive({"filters": {"password": ["=", "S8"], "name": ["=", "x"]}})
		assert out["filters"]["password"] == "<REDACTED:password>"
		assert out["filters"]["name"] == ["=", "x"]

	def test_non_sensitive_filters_are_byte_identical(self):
		text = '[["User","enabled","=",1],["User","name","like","%a%"],["name","=","x"]]'
		assert redaction.redact_sensitive({"f": text})["f"] == text
		url = "/a?filters=%5B%5B%22User%22%2C%22name%22%2C%22%3D%22%2C%22x%22%5D%5D"
		assert redaction.redact_url(url) == url

	def test_idempotent(self):
		once = redaction.redact_sensitive({"filters": self.ROW4})
		assert redaction.redact_sensitive(once) == once

	def test_other_list_lengths_are_left_alone(self):
		assert redaction.redact_sensitive({"f": ["password", "x"]}) == {"f": ["password", "x"]}
		assert redaction.redact_sensitive({"f": ["a", "password", "=", "x", "y"]})["f"][-1] == "y"

	def test_non_string_field_item(self):
		assert redaction.redact_sensitive({"f": [1, "=", "x"]}) == {"f": [1, "=", "x"]}


class TestTrailingPunctuation:
	@pytest.mark.parametrize(
		("text", "expected"),
		[
			('see "https://h/a?code=S3".', f'see "https://h/a?code={REDACTED}".'),
			("(https://h/a?code=S3)", f"(https://h/a?code={REDACTED})"),
			("go to https://h/a?code=S3, then", f"go to https://h/a?code={REDACTED}, then"),
			("<a href='https://h/a?code=S3'>x</a>", f"<a href='https://h/a?code={REDACTED}'>x</a>"),
			("[l](https://h/a?code=S3) ok", f"[l](https://h/a?code={REDACTED}) ok"),
			("end https://h/a?code=S3;", f"end https://h/a?code={REDACTED};"),
			("x https://h/a?code=S3>", f"x https://h/a?code={REDACTED}>"),
			("x https://h/a?a[0]=1&code=S3 y", f"x https://h/a?a[0]=1&code={REDACTED} y"),
		],
	)
	def test_the_tail_is_kept(self, text, expected):
		assert redaction.redact_sensitive({"v": text})["v"] == expected

	def test_idempotent_with_the_angle_bracket_placeholder(self):
		once = redaction.redact_sensitive({"v": "x https://h/a?code=S3> y"})
		assert redaction.redact_sensitive(once) == once

	def test_scrub_keeps_the_tail(self):
		assert redaction.scrub_secrets("see https://h/a?code=S3).") == "see https://h/a?code=********)."

	def test_a_run_of_closing_brackets_is_linear(self):
		redaction.redact_sensitive({"v": "a /p?x=1 " + "]" * 200_000})


class TestEmptyValues:
	def test_an_empty_secret_value_has_nothing_to_mask(self):
		for url in ("/a?token=", "/a?x=1&sid=&y=2", "/a?code=#frag"):
			assert redaction.redact_url(url) == url


class TestClosingBrackets:
	def test_an_unmatched_bracket_is_not_part_of_the_url(self):
		assert (
			redaction.redact_sensitive({"v": "x https://h/p?code=S3] y"})["v"]
			== f"x https://h/p?code={REDACTED}] y"
		)

	def test_a_matched_bracket_belongs_to_the_value(self):
		assert (
			redaction.redact_sensitive({"v": "x https://h/p?token=S[1] y"})["v"]
			== f"x https://h/p?token={REDACTED} y"
		)

	def test_only_the_unmatched_closers_are_stripped(self):
		out = redaction.redact_sensitive({"v": "x https://h/p?a[=1&code=S3]] y"})["v"]
		assert out == f"x https://h/p?a[=1&code={REDACTED}] y"


class TestLongRunsStayLinear:
	def test_a_long_run_without_a_candidate_is_not_rescanned(self):
		import time

		text = "go to /p?x=1 then " + "z" * 80_000 + " end"
		start = time.perf_counter()
		assert redaction.redact_sensitive({"v": text})["v"] == text
		assert time.perf_counter() - start < 1.0
