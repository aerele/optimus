# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Capture and report robustness: redaction never fails open on deep nesting or on any
error, a non-text ``cmd`` is cleaned, and times show in the site time zone."""

from __future__ import annotations

import json
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

import optimus  # noqa: F401 triggers _patch_recorder at import-time
from optimus.redaction import redact_sensitive

MARKER = "<REDACTED:nested too deep>"
CIRCULAR = "<REDACTED:circular>"
TOO_LARGE = "<REDACTED:too large>"
UNSUPPORTED = "<REDACTED:unsupported type>"
FAILED = {"_optimus": "<REDACTED:redaction failed>"}


def _nested(depth: int, leaf):
	value = leaf
	for _ in range(depth):
		value = {"a": value}
	return value


def _nested_json(depth: int, leaf_json: str) -> str:
	return '{"a":' * depth + leaf_json + "}" * depth


def _raise(error):
	def _fn(*args, **kwargs):
		raise error

	return _fn


class TestDepthBound:
	def test_deep_payload_never_keeps_the_secret(self):
		out = json.dumps(redact_sensitive(_nested(3000, {"x": "hunter2"})))
		assert "hunter2" not in out
		assert MARKER in out

	def test_deep_list_and_tuple_are_cut_too(self):
		listed = ["hunter2"]
		tupled = ("hunter2",)
		for _ in range(3000):
			listed = [listed]
			tupled = (tupled,)
		assert "hunter2" not in json.dumps(redact_sensitive(listed))
		assert "hunter2" not in json.dumps(redact_sensitive(tupled))

	def test_shallow_output_is_unchanged(self):
		leaf = {"password": "hunter2", "user": "alice", "rows": [{"token": "t"}, 1, None]}
		node = redact_sensitive(_nested(50, leaf))
		for _ in range(50):
			node = node["a"]
		assert node == {
			"password": "<REDACTED:password>",
			"user": "alice",
			"rows": [{"token": "<REDACTED:token>"}, 1, None],
		}

	def test_deep_json_string_is_cut_not_returned_raw(self):
		out = redact_sensitive({"doc": _nested_json(3000, '"hunter2"')})
		assert "hunter2" not in out["doc"]
		assert MARKER in out["doc"]

	def test_json_string_the_parser_cannot_read_becomes_the_marker(self):
		out = redact_sensitive({"doc": _nested_json(500_000, '"hunter2"')})
		assert out["doc"] == MARKER

	def test_json_string_within_range_is_still_redacted(self):
		out = redact_sensitive({"doc": _nested_json(20, '{"password":"hunter2"}')})
		assert "hunter2" not in out["doc"]

	def test_render_redaction_of_a_deep_recording_succeeds(self):
		from optimus.renderer import _internal

		out = _internal._redact_sensitive(_nested(3000, "hunter2"))
		assert "hunter2" not in json.dumps(out)


class TestSharedAndCyclicPayloads:
	def _timed(self, payload):
		start = time.monotonic()
		out = redact_sensitive(payload)
		assert time.monotonic() - start < 1.0
		return out

	def test_self_referencing_dict_ends_with_a_marker(self):
		cyclic = {"token": "S3CRET"}
		cyclic["a"] = cyclic
		cyclic["b"] = cyclic
		out = self._timed(cyclic)
		assert "S3CRET" not in repr(out)
		assert CIRCULAR in repr(out)

	def test_self_referencing_list_ends_with_a_marker(self):
		cyclic = ["S3CRET"]
		cyclic.append(cyclic)
		cyclic.append(cyclic)
		assert CIRCULAR in repr(self._timed(cyclic))

	def test_shared_subtrees_are_cut_by_the_container_budget(self):
		value = {"x": 1}
		for _ in range(80):
			value = {"l": value, "r": value}
		out = self._timed(value)
		assert TOO_LARGE in json.dumps(out)

	def test_the_same_object_twice_is_not_a_cycle(self):
		shared = {"user": "alice"}
		assert redact_sensitive({"a": shared, "b": shared}) == {"a": shared, "b": shared}

	def test_json_strings_spend_the_same_budget(self):
		text = "[" + ",".join(["{}"] * 2000) + "]"
		out = self._timed([text] * 70)
		assert TOO_LARGE in json.dumps(out)

	def test_budget_is_per_call(self):
		wide = [{"i": i} for i in range(60_000)]
		assert TOO_LARGE not in json.dumps(redact_sensitive(wide))
		assert TOO_LARGE not in json.dumps(redact_sensitive(wide))


class TestUnknownTypes:
	def test_dict_views_are_walked(self):
		out = redact_sensitive([{"password": "S3CRET"}.items(), {"token": "S3CRET"}.values()])
		assert out == [{"password": "<REDACTED:password>"}, UNSUPPORTED]

	def test_sets_and_key_views_are_walked_like_lists(self):
		out = redact_sensitive({"a": {"x", "y"}, "b": frozenset({"z"}), "c": {"k": 1}.keys()})
		assert sorted(out["a"]) == ["x", "y"]
		assert out["b"] == ["z"]
		assert out["c"] == ["k"]

	def test_secret_in_a_json_string_inside_a_set_is_redacted(self):
		out = redact_sensitive([{'{"password": "S3CRET"}'}])
		assert "S3CRET" not in repr(out)

	@pytest.mark.parametrize(
		"value", [b"S3CRET", bytearray(b"S3CRET"), object(), types.SimpleNamespace(p="S3CRET")]
	)
	def test_bytes_and_unknown_objects_become_a_marker(self, value):
		out = redact_sensitive({"a": value, "b": [value]})
		assert out == {"a": UNSUPPORTED, "b": [UNSUPPORTED]}

	def test_plain_scalars_pass_through(self):
		payload = {"i": 1, "f": 1.5, "t": True, "n": None, "s": "text"}
		assert redact_sensitive(payload) == payload


class TestRedundantCallsHashable:
	def test_deep_identifier_is_skipped_not_raised(self):
		from optimus.analyzers import redundant_calls

		deep = []
		for _ in range(5000):
			deep = [deep]
		assert redundant_calls._hashable_or_none(deep) is None
		assert redundant_calls._hashable_or_none([1, [2]]) == (1, (2,))
		assert redundant_calls._hashable_or_none([{}]) is None


class TestProfilerInit:
	@pytest.fixture(autouse=True)
	def _stub(self, monkeypatch):
		import frappe.recorder as rec

		if not hasattr(rec, "Recorder"):
			from optimus.tests.test_recorder_redaction_patch import _StubRecorder

			monkeypatch.setattr(rec, "Recorder", type("Recorder", (_StubRecorder,), {}), raising=False)
			optimus._patch_recorder()

	def _run(self, monkeypatch, form_dict, headers, cmd="x"):
		from frappe.recorder import Recorder

		obj = Recorder.__new__(Recorder)
		obj.form_dict, obj.headers, obj.cmd = form_dict, headers, cmd
		monkeypatch.setattr(Recorder.__init__, "__wrapped__", lambda self, *a, **k: None, raising=False)
		Recorder.__init__(obj)
		return obj

	def test_deep_form_dict_stored_without_the_secret(self, monkeypatch):
		obj = self._run(monkeypatch, _nested(3000, {"x": "hunter2"}), {"A": "b"})
		stored = json.dumps(obj.form_dict)
		assert "hunter2" not in stored
		assert MARKER in stored

	def test_any_redaction_error_stores_a_marker_not_raw_values(self, monkeypatch):
		import optimus.redaction as red

		monkeypatch.setattr(red, "redact_sensitive", _raise(ValueError("boom")))
		obj = self._run(monkeypatch, {"password": "hunter2"}, {"Authorization": "Bearer hunter2"})
		assert obj.form_dict == FAILED
		assert obj.headers == FAILED

	def test_failure_is_logged_at_error_level_without_values(self, monkeypatch):
		import optimus.redaction as red
		from optimus import safe_call

		lines = []
		monkeypatch.setattr(safe_call, "log_error_line", lambda message, **k: lines.append(message))
		monkeypatch.setattr(red, "redact_sensitive", _raise(ValueError("hunter2")))
		self._run(monkeypatch, {"password": "hunter2"}, {})
		assert len(lines) == 1
		assert "ValueError" in lines[0]
		assert "hunter2" not in lines[0]

	def test_a_failing_logger_never_breaks_the_request(self, monkeypatch):
		import optimus.redaction as red
		from optimus import safe_call

		monkeypatch.setattr(safe_call, "log_error_line", _raise(OSError()))
		monkeypatch.setattr(red, "redact_sensitive", _raise(ValueError("x")))
		obj = self._run(monkeypatch, {"password": "hunter2"}, {})
		assert obj.form_dict == FAILED

	def test_no_log_when_redaction_works(self, monkeypatch):
		from optimus import safe_call

		lines = []
		monkeypatch.setattr(safe_call, "log_error_line", lambda message, **k: lines.append(message))
		obj = self._run(monkeypatch, {"password": "hunter2", "a": "b"}, {})
		assert obj.form_dict == {"password": "<REDACTED:password>", "a": "b"}
		assert lines == []

	@pytest.mark.parametrize("cmd", [5, ["a"], {"a": 1}, 1.5, True])
	def test_non_text_cmd_is_cleaned_at_capture(self, cmd, monkeypatch):
		assert self._run(monkeypatch, {}, {}, cmd=cmd).cmd == ""

	def test_text_cmd_is_kept(self, monkeypatch):
		assert self._run(monkeypatch, {}, {}, cmd="frappe.client.get").cmd == "frappe.client.get"


class TestNonStringCmd:
	def _local(self, monkeypatch, form_dict, path=None):
		from optimus import hooks_callbacks as hc

		request = types.SimpleNamespace(path=path) if path else None
		local = types.SimpleNamespace(form_dict=form_dict, request=request)
		monkeypatch.setattr(hc.frappe, "local", local, raising=False)

	def test_should_skip_request_with_int_cmd_does_not_raise(self, monkeypatch):
		from optimus import hooks_callbacks as hc

		self._local(monkeypatch, {"cmd": 5})
		assert hc._extract_cmd_from_request() == ""
		assert hc._should_skip_request() is False

	def test_int_cmd_falls_back_to_the_path(self, monkeypatch):
		from optimus import hooks_callbacks as hc

		self._local(monkeypatch, {"cmd": 5}, path="/api/method/frappe.client.get")
		assert hc._extract_cmd_from_request() == "frappe.client.get"

	def test_load_recordings_light_gives_empty_cmd(self, monkeypatch):
		from optimus import analyze

		rec = {"uuid": "u1", "cmd": 5, "path": "/api/method/a.b", "calls": []}
		monkeypatch.setattr(
			analyze.frappe, "cache", types.SimpleNamespace(hget=lambda *a: rec), raising=False
		)
		doc = types.SimpleNamespace(name="s", session_uuid="s", recordings_file=None, actions=[])
		assert analyze.load_recordings_light(doc, ["u1"])[0]["cmd"] == ""

	def test_fetch_recordings_gives_empty_cmd(self, monkeypatch):
		from optimus import analyze

		rec = {"uuid": "u1", "cmd": 5, "path": "/api/method/a.b", "calls": []}
		cache = types.SimpleNamespace(hget=lambda *a: rec, get_value=lambda *a: None)
		monkeypatch.setattr(analyze.frappe, "cache", cache, raising=False)
		monkeypatch.setattr(analyze, "_allow_unsigned_pickles", lambda: False)
		assert next(analyze._fetch_recordings(["u1"]))["cmd"] == ""

	def test_per_action_labels_from_the_path(self):
		from optimus.analyzers.per_action import _label

		assert _label({"cmd": "", "path": "/api/method/a.b", "method": "GET"}) == "a.b"


class TestSiteTimeZone:
	@pytest.fixture(autouse=True)
	def _site_tz(self, monkeypatch):
		import frappe.utils as fu

		kolkata = timezone(timedelta(hours=5.5))

		def convert(value):
			assert value.tzinfo is not None, "a naive value is site time already"
			return value.astimezone(kolkata)

		monkeypatch.setattr(fu, "convert_utc_to_system_timezone", convert, raising=False)

		def format_datetime(value):
			if isinstance(value, str):
				value = datetime.fromisoformat(value)
			return value.strftime("%Y-%m-%d %H:%M")

		monkeypatch.setattr(fu, "format_datetime", format_datetime, raising=False)

	def test_aware_iso_string_is_shown_in_site_time(self):
		from optimus.renderer.time_format import _format_datetime_display

		assert _format_datetime_display("2026-11-07T04:30:00+00:00") == "2026-11-07 10:00"

	def test_aware_datetime_is_converted(self):
		from optimus.renderer.time_format import _format_datetime_display

		assert _format_datetime_display(datetime(2026, 11, 7, 4, 30, tzinfo=timezone.utc)) == "2026-11-07 10:00"

	def test_naive_value_is_unchanged(self):
		from optimus.renderer.time_format import _format_datetime_display

		assert _format_datetime_display(datetime(2026, 11, 7, 4, 30)) == "2026-11-07 04:30"
		assert _format_datetime_display("2026-11-07 04:30:00") == "2026-11-07 04:30"

	def test_unparseable_string_falls_back(self):
		from optimus.renderer.time_format import _format_datetime_display

		assert _format_datetime_display("not a date") == "not a date"

	def test_now_iso_uses_site_now(self, monkeypatch):
		import frappe.utils as fu

		from optimus.renderer._internal import _now_iso

		monkeypatch.setattr(fu, "now_datetime", lambda: datetime(2026, 1, 2, 3, 4, 5), raising=False)
		assert _now_iso() == "2026-01-02 03:04:05"

	def test_now_iso_falls_back_when_frappe_fails(self, monkeypatch):
		import frappe.utils as fu

		from optimus.renderer._internal import _now_iso

		monkeypatch.setattr(fu, "now_datetime", _raise(RuntimeError()), raising=False)
		assert len(_now_iso()) == 19
