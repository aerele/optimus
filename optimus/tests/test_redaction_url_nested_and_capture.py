# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Nested and encoded URL values, credential header names, the fail-closed capture hook, the one source
of Settings extras, and the smaller redaction fixes. Frappe-free."""

import time

import pytest

import optimus  # noqa: F401 triggers _patch_recorder at import-time
from optimus import redaction
from optimus.tests.test_recorder_redaction_patch import _Recorder, _stub_recorder  # noqa: F401
from optimus.tests.test_redaction_url_wiring import _captured

REDACTED = "<REDACTED>"


@pytest.fixture(autouse=True)
def _fresh_failure_logs(monkeypatch):
	"""Each failure class is logged once per process; start every test with nothing logged yet."""
	from optimus import redaction_extras, redaction_log

	monkeypatch.setattr(redaction_extras, "_logged", set())
	monkeypatch.setattr(redaction_log, "_logged_capture", set())


def _seconds(fn, *args):
	start = time.perf_counter()
	fn(*args)
	return time.perf_counter() - start


class TestNestedUrlValues:
	@pytest.mark.parametrize(
		("url", "leak"),
		[
			("/login?redirect-to=%2Fupdate-password%3Fkey%3DRESETSECRET", "RESETSECRET"),
			("/login?redirect-to=%252Fupdate-password%253Fkey%253DRESETSECRET", "RESETSECRET"),
			("/login?next=/a?token=NESTEDSECRET&x=1", "NESTEDSECRET"),
			("/x?a=1?token=NESTEDSECRET", "NESTEDSECRET"),
			("/x?cb=https%3A%2F%2Fh%2Fp%3Fapi_key%3DNESTEDSECRET%26k%3D1", "NESTEDSECRET"),
			("/x?cb=https%3A%2F%2Fu%3Apw%40h%2Fp", "u%3Apw"),
			("/x?go=/a%3Fnext%3D/b%253Ftoken%253DDEEPSECRET", "DEEPSECRET"),
		],
	)
	def test_a_secret_inside_a_url_valued_parameter_is_masked(self, url, leak):
		assert leak not in redaction.redact_url(url)

	def test_the_rest_of_the_nested_url_stays_readable(self):
		out = redaction.redact_url("/login?next=/a?token=S&x=1&page=2")
		assert out == f"/login?next=/a?token={REDACTED}&x=1&page=2"
		out = redaction.redact_url("/login?redirect-to=%2Fapp%2Fuser%3Fdoctype%3DUser%26token%3DSECRETVAL")
		assert "doctype" in out and "SECRETVAL" not in out

	def test_a_url_without_a_nested_secret_is_unchanged(self):
		url = "/login?redirect-to=%2Fapp%2Fuser%3Fdoctype%3DUser&next=/a?page=2"
		assert redaction.redact_url(url) == url

	def test_deep_nesting_is_masked_not_skipped(self):
		url = "/x?" + "a=/b?" * 40 + "token=DEEPSECRET"
		assert "DEEPSECRET" not in redaction.redact_url(url)

	def test_nested_masking_is_bounded(self):
		url = "/x?" + "a=/b?" * 1500 + "token=DEEPSECRET"
		assert _seconds(redaction.redact_url, url) < 1.5
		assert "DEEPSECRET" not in redaction.redact_url(url)


class TestCredentialHeaderNames:
	@pytest.mark.parametrize(
		"name",
		[
			"Ocp-Apim-Subscription-Key",
			"X-Master-Key",
			"X-Key",
			"X-Auth",
			"Auth",
			"X-Bearer",
			"Bearer",
			"x-vault-auth",
		],
	)
	def test_header_names_are_masked(self, name):
		out = redaction.redact_sensitive({name: "v", "Accept": "a"}, header=True)
		assert out[name] == f"<REDACTED:{name}>"
		assert out["Accept"] == "a"

	@pytest.mark.parametrize(
		"name", ["Idempotency-Key", "X-Idempotency-Key", "Content-Type", "Author", "X-Request-Id"]
	)
	def test_allow_listed_and_ordinary_headers_stay_visible(self, name):
		assert redaction.redact_sensitive({name: "v"}, header=True) == {name: "v"}

	def test_the_key_suffix_rule_is_for_headers_only(self):
		# A form field named sort_key or auth is not a credential.
		assert redaction.redact_sensitive({"sort_key": "idx", "auth": "x"}) == {
			"sort_key": "idx",
			"auth": "x",
		}

	def test_header_mode_applies_to_the_top_level_only(self):
		out = redaction.redact_sensitive({"X-Hook": {"sort_key": "idx"}}, header=True)
		assert out == {"X-Hook": {"sort_key": "idx"}}


class TestCaptureHook:
	def test_a_failing_form_dict_pass_still_masks_the_headers(self, monkeypatch, caplog):
		real = redaction.redact_sensitive

		def flaky(payload, **kwargs):
			if "boom" in payload:
				raise RuntimeError("boom")
			return real(payload, **kwargs)

		monkeypatch.setattr(redaction, "redact_sensitive", flaky)
		with caplog.at_level("ERROR"):
			rec = _captured("/x", "x", {"boom": "RAWSECRET"}, {"Referer": "/a?token=HEADSECRET"})
		assert "RAWSECRET" not in str(rec.form_dict)
		assert "HEADSECRET" not in str(rec.headers)
		assert any(r.levelname == "ERROR" and "form_dict" in r.getMessage() for r in caplog.records)

	def test_a_failing_headers_pass_still_masks_the_form_dict(self, monkeypatch, caplog):
		real = redaction.redact_sensitive

		def flaky(payload, **kwargs):
			if "X-Boom" in payload:
				raise RuntimeError("boom")
			return real(payload, **kwargs)

		monkeypatch.setattr(redaction, "redact_sensitive", flaky)
		with caplog.at_level("ERROR"):
			rec = _captured("/x", "x", {"password": "RAWSECRET"}, {"X-Boom": "HEADSECRET"})
		assert "RAWSECRET" not in str(rec.form_dict)
		assert "HEADSECRET" not in str(rec.headers)
		assert any(r.levelname == "ERROR" and "headers" in r.getMessage() for r in caplog.records)

	def test_the_failure_log_names_the_error_class_and_never_the_value(self, monkeypatch, caplog):
		def boom(payload, **kwargs):
			raise ValueError("RAWSECRET")

		monkeypatch.setattr(redaction, "redact_sensitive", boom)
		with caplog.at_level("ERROR"):
			rec = _captured("/x", "x", {"a": "RAWSECRET"}, {"b": "RAWSECRET"})
		assert "RAWSECRET" not in str(rec.form_dict) + str(rec.headers)
		assert "RAWSECRET" not in caplog.text and "ValueError" in caplog.text

	def test_the_capture_masks_credential_header_names_and_oauth_code(self):
		rec = _captured(
			"/api/method/frappe.integrations.oauth2_logins.login_via_google/google",
			"frappe.integrations.oauth2_logins.login_via_google",
			{"code": "OAUTHCODE", "state": "s"},
			{"X-Master-Key": "MK", "Idempotency-Key": "ok"},
		)
		assert rec.form_dict["code"] == "<REDACTED:code>" and rec.form_dict["state"] == "s"
		assert rec.headers == {"X-Master-Key": "<REDACTED:X-Master-Key>", "Idempotency-Key": "ok"}

	def test_code_is_kept_on_an_ordinary_request(self):
		rec = _captured("/api/method/frappe.client.get", "frappe.client.get", {"code": "ABC"}, {})
		assert rec.form_dict == {"code": "ABC"}


class TestFormKeyExtras:
	@pytest.fixture
	def custom(self, monkeypatch):
		from optimus import redaction_extras

		monkeypatch.setattr(redaction_extras, "form_key_extras", lambda: ("my_field",))

	def test_returns_the_settings_extras(self, monkeypatch):
		from optimus import redaction_extras, settings

		monkeypatch.setattr(
			settings, "get_config", lambda: type("C", (), {"sensitive_form_keys": ["a_field", 7]})()
		)
		assert redaction_extras.form_key_extras() == ("a_field", 7)

	def test_a_failing_config_read_is_logged_at_error(self, monkeypatch, caplog):
		from optimus import redaction_extras, settings

		def broken():
			raise RuntimeError("db gone")

		monkeypatch.setattr(settings, "get_config", broken)
		monkeypatch.setattr(redaction_extras, "_logged", set())
		with caplog.at_level("ERROR"):
			assert redaction_extras.form_key_extras() == ()
		assert any(r.levelname == "ERROR" and "RuntimeError" in r.getMessage() for r in caplog.records)

	def test_scrub_secrets_uses_it(self, custom):
		out = redaction.scrub_secrets("see https://h/p?my_field=SECRETVAL&a=1")
		assert "SECRETVAL" not in out

	def test_ai_steps_use_it(self, custom):
		from optimus import ai_privacy

		rec = ai_privacy.action_recording({"cmd": "x", "path": "/a?my_field=SECRETVAL", "method": "GET"})
		assert "SECRETVAL" not in rec["path"]

	def test_the_frontend_analyzer_uses_it(self, custom):
		from optimus.analyzers import frontend_timings
		from optimus.analyzers.base import AnalyzeContext

		ctx = AnalyzeContext(session_uuid="t", docname="t")
		ctx.frontend_data = {
			"xhr": [{"recording_id": "g", "url": "/a?my_field=SECRETVAL", "duration_ms": 5}],
			"vitals": [],
		}
		assert "SECRETVAL" not in str(frontend_timings.analyze([], ctx).aggregate)

	def test_report_context_uses_it(self, custom):
		from optimus import report_context

		_, xhrs, _ = report_context._masked_frontend_urls(
			{"frontend_xhr_matched": [{"url": "/a?my_field=SECRETVAL"}]}
		)
		assert "SECRETVAL" not in str(xhrs)

	def test_the_metrics_endpoint_uses_it(self, custom):
		from optimus import api

		rows = api._mask_frontend_urls([{"url": "/a?my_field=SECRETVAL"}], "url")
		assert "SECRETVAL" not in str(rows)

	def test_the_render_time_pass_uses_it(self, custom):
		from optimus.renderer import _internal

		assert _internal._settings_extras()[0] == ("my_field",)


class TestMinors:
	def test_a_short_list_that_is_not_a_filter_row_is_kept(self):
		assert redaction.redact_sensitive({"v": ["password", "hint", "weak"]}) == {
			"v": ["password", "hint", "weak"]
		}
		assert (
			redaction.redact_sensitive({"v": ["User", "token", "like", "x"]})["v"][-1] == "<REDACTED:token>"
		)
		assert redaction.redact_sensitive({"v": ["token", "=", "x"]})["v"][-1] == "<REDACTED:token>"

	def test_truncated_json_with_a_secret_is_masked(self):
		text = '{"doc": {"name": "a", "password": "TRUNCSECRET", "x": "' + "y" * 50
		assert "TRUNCSECRET" not in redaction.redact_sensitive({"doc": text})["doc"]

	def test_deeply_nested_json_with_a_secret_is_masked(self):
		text = '{"a":' * 5000 + '{"password": "DEEPSECRET"}' + "}" * 5000
		assert "DEEPSECRET" not in redaction.redact_sensitive({"doc": text})["doc"]

	def test_truncated_json_without_a_secret_is_unchanged(self):
		text = '{"doc": {"name": "a", "x": "' + "y" * 50
		assert redaction.redact_sensitive({"doc": text}) == {"doc": text}

	def test_the_string_helper_is_public(self):
		assert redaction.redact_string_value("go /a?token=S now") == f"go /a?token={REDACTED} now"
