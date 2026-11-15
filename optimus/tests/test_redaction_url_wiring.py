# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Where the URL and header masks are applied: the capture-time Recorder patch, the
frontend metrics endpoint, the frontend analyzer and report context, the render-time
redaction and the AI Steps recording. Frappe-free."""

import json
import types

import pytest

import optimus  # noqa: F401 triggers _patch_recorder at import-time
from optimus import redaction
from optimus.tests.test_recorder_redaction_patch import _Recorder, _stub_recorder  # noqa: F401

REDACTED = "<REDACTED>"


def _captured(path, cmd, form_dict, headers):
	Recorder = _Recorder()
	rec = Recorder.__new__(Recorder)
	rec.path, rec.cmd, rec.form_dict, rec.headers = path, cmd, form_dict, headers
	Recorder.__init__(rec)
	return rec


class TestCapture:
	def test_key_bearing_endpoint_masks_the_bare_key_in_form_dict(self):
		rec = _captured("/update-password", "", {"key": "reset123", "new_password": "p"}, {})
		assert rec.form_dict["key"] == "<REDACTED:key>"
		assert rec.form_dict["new_password"] == "<REDACTED:new_password>"

	def test_printview_masks_the_share_key(self):
		rec = _captured("/printview", "", {"doctype": "Invoice", "key": "share1"}, {})
		assert rec.form_dict == {"doctype": "Invoice", "key": "<REDACTED:key>"}

	def test_other_endpoints_keep_the_bare_key(self):
		rec = _captured(
			"/api/method/frappe.client.get_value", "frappe.client.get_value", {"key": "country"}, {}
		)
		assert rec.form_dict == {"key": "country"}

	def test_referer_query_and_secret_header_names_are_masked(self):
		headers = {
			"Referer": "https://s/app/x?api_key=abc&sid=9&doctype=User",
			"X-Api-Key": "v1",
			"Content-Type": "application/json",
			"Origin": "https://s",
			"X-Frappe-CMD": "a.b",
			"X-Frappe-CSRF-Token": "tok",
		}
		out = _captured("/api/method/x", "x", {}, headers).headers
		assert out["Referer"] == f"https://s/app/x?api_key={REDACTED}&sid={REDACTED}&doctype=User"
		assert out["X-Api-Key"] == "<REDACTED:X-Api-Key>"
		assert out["X-Frappe-CSRF-Token"] == "<REDACTED:X-Frappe-CSRF-Token>"
		assert (out["Content-Type"], out["Origin"], out["X-Frappe-CMD"]) == (
			"application/json",
			"https://s",
			"a.b",
		)

	def test_a_hostile_path_or_cmd_does_not_break_the_request(self):
		rec = _captured(object(), 5, {"key": "k"}, {"Referer": "/a?token=1"})
		assert rec.form_dict == {"key": "k"}
		assert rec.headers["Referer"] == f"/a?token={REDACTED}"


class TestRenderTimeRedaction:
	def test_recording_copy_is_masked_for_a_key_bearing_request(self):
		from optimus.renderer import _internal

		rec = {
			"path": "/update-password",
			"cmd": "",
			"form_dict": {"key": "reset123", "doctype": "User"},
			"headers": {"Referer": "/a?sid=1"},
		}
		_internal._redact_request_fields(rec)
		assert rec["form_dict"] == {"key": "<REDACTED:key>", "doctype": "User"}
		assert rec["headers"] == {"Referer": f"/a?sid={REDACTED}"}

	def test_render_masks_every_recording_it_keeps(self, monkeypatch):
		from optimus import renderer
		from optimus.renderer import _internal
		from optimus.tests.test_background_jobs_section import _action, _doc

		seen = []
		monkeypatch.setattr(_internal, "_redact_request_fields", seen.append)
		recs = [{"uuid": "r0", "headers": {"Referer": "/a?token=1"}}, {"no_uuid": True}]
		renderer.render_raw(_doc([_action(recording_uuid="r0")]), recordings=recs)
		assert [r["uuid"] for r in seen] == ["r0"]
		assert recs[0]["headers"] == {"Referer": "/a?token=1"}

	def test_recording_copy_keeps_the_key_of_an_ordinary_request(self):
		from optimus.renderer import _internal

		rec = {"path": "/api/method/x", "cmd": "x", "form_dict": {"key": "country"}, "headers": None}
		_internal._redact_request_fields(rec)
		assert rec["form_dict"] == {"key": "country"}

	def test_old_session_headers_and_form_dict_are_masked_at_render(self):
		from optimus.renderer import _internal

		headers = _internal._redact_sensitive({"Referer": "/a?token=1", "X-Session-Id": "s"})
		assert headers == {"Referer": f"/a?token={REDACTED}", "X-Session-Id": "<REDACTED:X-Session-Id>"}
		form = _internal._redact_sensitive({"key": "k"}, mask_key=True)
		assert form == {"key": "<REDACTED:key>"}
		assert _internal._redact_sensitive({"key": "k"}) == {"key": "k"}


@pytest.fixture
def frontend_store(monkeypatch):
	import frappe

	monkeypatch.setattr("optimus.ratelimit.enforce_user_rate_limit", lambda *a, **kw: None)
	lists = {}

	class Cache:
		def get_value(self, key):
			return {"user": "alice@example.com"}

		def rpush(self, key, value):
			lists.setdefault(key, []).append(value)

		def ltrim(self, *a):
			pass

		def llen(self, key):
			return len(lists.get(key, []))

	monkeypatch.setattr(frappe, "cache", Cache(), raising=False)
	monkeypatch.setattr(frappe, "session", types.SimpleNamespace(user="alice@example.com"), raising=False)
	monkeypatch.setattr(frappe, "parse_json", json.loads, raising=False)
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: ["System Manager"], raising=False)
	monkeypatch.setattr(frappe, "log_error", lambda *a, **k: None, raising=False)
	monkeypatch.setattr("optimus.session.get_session_meta", lambda uuid: {"user": "alice@example.com"})
	monkeypatch.setattr("optimus.session.expire_key", lambda *a, **k: None)
	return lists


class TestFrontendMetricsEndpoint:
	def test_secret_query_values_never_reach_redis(self, frontend_store):
		from optimus import api

		payload = {
			"session_uuid": "S1",
			"xhr": [
				{"recording_id": "r", "url": "/api/method/x?api_key=SECRETVAL&doctype=User"},
				{"recording_id": "r2", "url": "/api/method/y"},
			],
			"vitals": [{"name": "lcp", "page_url": "/app/x?token=SECRETVAL&a=1#sid=SECRETVAL"}],
		}
		assert api.submit_frontend_metrics(payload=json.dumps(payload))["accepted"] is True
		stored = json.dumps(frontend_store)
		assert "SECRETVAL" not in stored
		assert f"/api/method/x?api_key={REDACTED}&doctype=User" in stored
		assert "/api/method/y" in stored

	def test_a_row_without_a_url_is_stored_as_sent(self, frontend_store):
		from optimus import api

		payload = {
			"session_uuid": "S1",
			"xhr": [{"recording_id": "r", "url": None}],
			"vitals": [{"name": "x"}],
		}
		assert api.submit_frontend_metrics(payload=json.dumps(payload))["accepted"] is True


class TestAnalyzerAndReportContext:
	def test_analyzer_masks_stored_raw_urls(self):
		from optimus.analyzers import frontend_timings
		from optimus.analyzers.base import AnalyzeContext

		ctx = AnalyzeContext(session_uuid="t", docname="t")
		ctx.frontend_data = {
			"xhr": [{"recording_id": "gone", "url": "/a?sid=SECRETVAL", "duration_ms": 5}],
			"vitals": [{"name": "fcp", "value_ms": 10, "page_url": "/p?token=SECRETVAL", "timestamp": 1}],
		}
		result = frontend_timings.analyze([], ctx)
		assert "SECRETVAL" not in json.dumps(result.aggregate, default=str)

	def test_report_context_masks_a_session_analyzed_before_the_fix(self):
		from optimus import report_context

		ctx = {
			"frontend_vitals_by_page": {"/p?token=SECRETVAL": {"fcp_ms": 10}},
			"frontend_xhr_matched": [{"action_label": "a", "url": "/a?key=SECRETVAL", "status": 200}],
			"frontend_orphans": [{"url": "/o?sid=SECRETVAL"}],
		}
		built = report_context._build_frontend(ctx)
		assert "SECRETVAL" not in json.dumps(built, default=str)
		assert built["xhr_matched"][0]["url"] == f"/a?key={REDACTED}"
		assert ctx["frontend_xhr_matched"][0]["url"] == "/a?key=SECRETVAL"


class TestAiSteps:
	def test_recording_text_fields_have_secret_query_values_masked(self):
		from optimus import ai_privacy

		rec = ai_privacy.action_recording(
			{
				"cmd": "x",
				"path": "/a?api_key=SECRETVAL",
				"method": "GET",
				"form_dict": {"name": "/p?sid=SECRETVAL"},
			}
		)
		assert "SECRETVAL" not in json.dumps(rec)
		assert rec["path"] == f"/a?api_key={REDACTED}"


def test_one_parameter_rule_serves_urls_and_scrub():
	for name in ("api_key", "sid", "_signature", "key", "x-api-key"):
		assert redaction.redact_url(f"/a?{name}=v1") == f"/a?{name}={REDACTED}"
		assert redaction.scrub_secrets(f"https://h/a?{name}=v1") == f"https://h/a?{name}=********"
