# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Capture and report robustness: redaction never fails open on deep nesting or on any
error, a non-text ``cmd`` is cleaned, and times show in the site time zone."""

from __future__ import annotations

import json
import types
from datetime import datetime, timedelta, timezone

import pytest

import optimus  # noqa: F401 triggers _patch_recorder at import-time
from optimus.redaction import _MAX_REDACT_CONTAINERS, redact_sensitive, redact_sensitive_walk

MARKER = "<REDACTED:nested too deep>"
CIRCULAR = "<REDACTED:circular>"
TOO_LARGE = "<REDACTED:too large>"
UNSUPPORTED = "<REDACTED:unsupported type>"
FAILED = {"_optimus": "<REDACTED:redaction failed>", "_optimus_omitted": 1}
UNREADABLE = "<REDACTED:unreadable json>"


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
		"""The redacted payload, after checking the walk spent no more than its budget (a
		visit count, not a wall clock, so a slow runner cannot fail it)."""
		out, walk = redact_sensitive_walk(payload)
		assert walk.visited <= _MAX_REDACT_CONTAINERS
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


def _recorder_with_calls(recorder_class):
	obj = recorder_class.__new__(recorder_class)
	obj.calls = []
	return obj


class _InitHarness:
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


class TestProfilerInit(_InitHarness):

	def test_deep_form_dict_stored_without_the_secret(self, monkeypatch):
		obj = self._run(monkeypatch, _nested(3000, {"x": "hunter2"}), {"A": "b"})
		stored = json.dumps(obj.form_dict)
		assert "hunter2" not in stored
		assert MARKER in stored

	def test_any_redaction_error_stores_a_marker_not_raw_values(self, monkeypatch):
		import optimus.redaction as red

		monkeypatch.setattr(red, "redact_sensitive_walk", _raise(ValueError("boom")))
		obj = self._run(monkeypatch, {"password": "hunter2"}, {"Authorization": "Bearer hunter2"})
		assert obj.form_dict == FAILED
		assert obj.headers == FAILED

	def test_failure_is_logged_at_error_level_without_values(self, monkeypatch):
		import optimus.redaction as red
		from optimus import safe_call

		lines = []
		monkeypatch.setattr(safe_call, "log_error_line", lambda message, **k: lines.append(message))
		monkeypatch.setattr(red, "redact_sensitive_walk", _raise(ValueError("hunter2")))
		self._run(monkeypatch, {"password": "hunter2"}, {})
		assert len(lines) == 1
		assert "ValueError" in lines[0]
		assert "hunter2" not in lines[0]

	def test_a_failing_logger_never_breaks_the_request(self, monkeypatch):
		import optimus.redaction as red
		from optimus import safe_call

		monkeypatch.setattr(safe_call, "log_error_line", _raise(OSError()))
		monkeypatch.setattr(red, "redact_sensitive_walk", _raise(ValueError("x")))
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


class TestOmittedCount:
	def test_each_fail_closed_marker_is_counted(self):
		cyclic = {}
		cyclic["a"] = cyclic
		payload = {"deep": _nested(3000, 1), "loop": cyclic, "raw": b"x", "obj": object()}
		out, walk = redact_sensitive_walk(payload)
		assert walk.omitted == 4
		assert MARKER in json.dumps(out)

	def test_a_clean_payload_counts_nothing(self):
		_, walk = redact_sensitive_walk({"password": "x", "a": [1, {"b": "c"}]})
		assert walk.omitted == 0

	def test_a_sensitive_key_is_redacted_not_omitted(self):
		out, walk = redact_sensitive_walk({"token": "t"})
		assert out == {"token": "<REDACTED:token>"}
		assert walk.omitted == 0

	def test_visits_count_the_containers_walked(self):
		_, walk = redact_sensitive_walk([{"i": i} for i in range(10)])
		assert walk.visited == 11

	def test_count_omitted_reads_form_dict_and_headers(self):
		from optimus.redaction import OMITTED_COUNT_KEY, count_omitted

		rec = {"form_dict": {OMITTED_COUNT_KEY: 2}, "headers": {OMITTED_COUNT_KEY: 1}}
		assert count_omitted(rec) == 3
		assert count_omitted({"form_dict": {OMITTED_COUNT_KEY: "x"}, "headers": None}) == 0
		assert count_omitted({"form_dict": {OMITTED_COUNT_KEY: True}}) == 0
		assert count_omitted(None) == 0

	def test_the_literal_used_by_the_failure_path_matches(self):
		from optimus.redaction import OMITTED_COUNT_KEY

		assert OMITTED_COUNT_KEY in FAILED


class TestJsonStringBudget:
	def test_each_parse_attempt_spends_the_budget(self):
		out, walk = redact_sensitive_walk(["{}"] * 150_000)
		assert walk.visited == _MAX_REDACT_CONTAINERS
		assert TOO_LARGE in out

	def test_each_failed_parse_attempt_spends_the_budget(self):
		out, walk = redact_sensitive_walk(["{x}"] * 150_000)
		assert walk.visited == _MAX_REDACT_CONTAINERS
		assert TOO_LARGE in out

	def test_text_that_cannot_be_json_is_not_parsed(self):
		out, walk = redact_sensitive_walk(["{x"] * 150_000)
		assert walk.visited == 1
		assert TOO_LARGE not in out
		assert out[0] == "{x"

	def test_a_closed_object_is_still_parsed(self):
		out = redact_sensitive({"doc": '{"password": "S3CRET"}'})
		assert out["doc"] == '{"password": "<REDACTED:password>"}'


class TestUnreadableJson:
	def test_cut_off_text_naming_a_secret_key_is_omitted(self):
		out, walk = redact_sensitive_walk({"doc": '{"password":"S3CRET",'})
		assert out["doc"] == UNREADABLE
		assert walk.omitted == 1

	def test_malformed_closed_text_naming_a_secret_key_is_omitted(self):
		assert redact_sensitive({"doc": '{"password":"S3CRET",}'})["doc"] == UNREADABLE

	def test_a_custom_key_counts(self):
		out = redact_sensitive({"doc": '{"nickname":"S3CRET",'}, extra_keys=("nickname",))
		assert out["doc"] == UNREADABLE

	def test_text_without_a_secret_key_is_kept(self):
		assert redact_sensitive({"doc": '{"a":1,'})["doc"] == '{"a":1,'
		assert redact_sensitive({"doc": "[Draft] password reset"})["doc"] == "[Draft] password reset"

	def test_an_empty_extra_key_does_not_omit_everything(self):
		from optimus.redaction import _redact_json_string

		assert _redact_json_string('{"a":1,', ("",)) == '{"a":1,'

	def test_repeated_key_with_a_masked_last_value_does_not_return_the_text(self):
		text = '{"password":"S3CRET","password":"<REDACTED:password>"}'
		assert "S3CRET" not in redact_sensitive({"doc": text})["doc"]

	def test_repeated_key_in_a_nested_object(self):
		text = '{"a":{"x":"S3CRET","x":"ok"},"b":1}'
		assert "S3CRET" not in redact_sensitive({"doc": text})["doc"]

	def test_text_without_repeated_keys_is_returned_unchanged(self):
		text = '{ "a" : 1 }'
		assert redact_sensitive({"doc": text})["doc"] == text


class TestProfilerInitCounts(_InitHarness):
	def test_omitted_values_are_counted_in_the_stored_form_dict(self, monkeypatch):
		obj = self._run(monkeypatch, {"a": b"x", "b": _nested(3000, 1)}, {"A": "b"})
		assert obj.form_dict["_optimus_omitted"] == 2

	def test_headers_count_when_there_is_no_form_dict(self, monkeypatch):
		obj = self._run(monkeypatch, None, {"A": object()})
		assert obj.headers["_optimus_omitted"] == 1

	def test_nothing_is_added_when_nothing_was_omitted(self, monkeypatch):
		obj = self._run(monkeypatch, {"a": "b"}, {"A": "b"})
		assert obj.form_dict == {"a": "b"}
		assert obj.headers == {"A": "b"}

	def test_a_form_dict_that_is_not_a_dict_does_not_stay_raw(self, monkeypatch):
		obj = self._run(monkeypatch, types.SimpleNamespace(password="S3CRET"), {})
		assert obj.form_dict == UNSUPPORTED


class TestSettingsReadFailure(_InitHarness):
	@pytest.fixture(autouse=True)
	def _settings_down(self, monkeypatch):
		import frappe

		import optimus.settings as settings

		monkeypatch.setattr(settings, "get_config", _raise(RuntimeError("db down")), raising=False)
		if getattr(frappe, "local", None) is None:
			monkeypatch.setattr(frappe, "local", types.SimpleNamespace(), raising=False)
		monkeypatch.setattr(frappe.local, "optimus_reading_extras", False, raising=False)

	def test_unreadable_settings_omit_the_request_values_and_log(self, monkeypatch):
		from optimus import safe_call

		lines = []
		monkeypatch.setattr(safe_call, "log_error_line", lambda message, **k: lines.append(message))
		obj = self._run(monkeypatch, {"nickname": "S3CRET"}, {"A": "b"})
		assert obj.form_dict == FAILED
		assert obj.headers == FAILED
		assert len(lines) == 1
		assert "RuntimeError" in lines[0]

	def test_unreadable_settings_omit_the_query_and_log(self, monkeypatch):
		from frappe.recorder import Recorder

		from optimus import safe_call
		from optimus.redaction import OMITTED_SQL

		lines = []
		monkeypatch.setattr(safe_call, "log_error_line", lambda message, **k: lines.append(message))
		monkeypatch.setattr(Recorder.register, "__wrapped__", lambda self, data: None, raising=False)
		data = {"query": "select 1 where nickname = 'S3CRET'"}
		Recorder.register(_recorder_with_calls(Recorder), data)
		assert data["query"] == OMITTED_SQL
		assert len(lines) == 1
		assert "RuntimeError" in lines[0]
		assert "S3CRET" not in lines[0]


class TestJobTimeoutIsNotSwallowed(_InitHarness):
	class _Timeout(Exception):
		pass

	@pytest.fixture(autouse=True)
	def _timeout_type(self, monkeypatch):
		from optimus import safe_call

		monkeypatch.setattr(safe_call, "job_timeout_types", lambda: (self._Timeout,))

	def test_timeout_during_request_redaction_is_raised_again(self, monkeypatch):
		import optimus.redaction as red

		monkeypatch.setattr(red, "redact_sensitive_walk", _raise(self._Timeout("t")))
		with pytest.raises(self._Timeout):
			self._run(monkeypatch, {"password": "hunter2"}, {})

	def test_timeout_during_query_redaction_is_raised_again(self, monkeypatch):
		from frappe.recorder import Recorder

		import optimus.redaction as red

		monkeypatch.setattr(red, "redact_sql_fail_closed", _raise(self._Timeout("t")))
		monkeypatch.setattr(Recorder.register, "__wrapped__", lambda self, data: None, raising=False)
		with pytest.raises(self._Timeout):
			Recorder.register(_recorder_with_calls(Recorder), {"query": "select 1"})

	def test_the_raised_timeout_is_a_fresh_instance_without_a_traceback(self):
		original = self._Timeout("t")
		with pytest.raises(self._Timeout) as caught:
			optimus._raise_if_job_timeout(original)
		assert caught.value is not original
		assert caught.value.__cause__ is None
		assert caught.value.__suppress_context__

	def test_other_errors_are_not_raised(self):
		optimus._raise_if_job_timeout(ValueError("x"))


class TestPerActionNonTextFields:
	@pytest.mark.parametrize("value", [5, ["a"], {"a": 1}, 1.5, True, None])
	@pytest.mark.parametrize(
		"cmd",
		[
			"frappe.desk.form.save.savedocs",
			"frappe.desk.form.run_method.runserverobj",
			"run_doc_method",
			"frappe.model.workflow.apply_workflow",
		],
	)
	def test_label_and_suffix_never_raise(self, cmd, value):
		from optimus.analyzers import per_action

		form_dict = {"action": value, "method": value, "dt": value, "dn": value, "doctype": "Item"}
		assert isinstance(per_action._multiplex_suffix(cmd, form_dict), str)
		rec = {"cmd": cmd, "path": "/api/method/x", "form_dict": form_dict, "method": "POST"}
		assert isinstance(per_action.humanized_label(rec), str)

	def test_text_values_still_label(self):
		from optimus.analyzers import per_action

		rec = {
			"cmd": "frappe.model.workflow.apply_workflow",
			"form_dict": {"action": " Approve ", "doctype": "Item"},
		}
		assert per_action.humanized_label(rec) == "Approve Item"


class TestOmittedWarning:
	def test_analyze_says_how_many_values_were_omitted(self):
		from optimus import analyze

		recs = [{"form_dict": {"_optimus_omitted": 2}}, {"headers": {"_optimus_omitted": 1}}, {}]
		assert analyze._omitted_values_warning(recs).startswith("3 request values were omitted")

	def test_no_warning_when_nothing_was_omitted(self):
		from optimus import analyze

		assert analyze._omitted_values_warning([{"form_dict": {"a": 1}}, {}]) is None

	def test_singular(self):
		from optimus import analyze

		warning = analyze._omitted_values_warning([{"form_dict": {"_optimus_omitted": 1}}])
		assert warning.startswith("1 request value was omitted")


class TestRedundantCallsSkipCount:
	def test_unhashable_identifier_is_skipped_and_counted_in_a_warning(self):
		from optimus.analyzers import redundant_calls
		from optimus.analyzers.base import AnalyzeContext

		deep = []
		for _ in range(5000):
			deep = [deep]
		stack = [{"filename": "apps/myapp/x.py", "lineno": 1, "function": "f"}]
		entry = {
			"fn_name": "get_doc",
			"identifier_raw": ("Item", "A"),
			"identifier_safe": deep,
			"caller_stack": stack,
		}
		good = dict(entry, identifier_safe=("Item", "h"))
		recording = {"uuid": "r", "calls": [], "sidecar": [entry, entry, good], "pyi_session": None}
		ctx = AnalyzeContext(session_uuid="t", docname="t")
		ctx.actions = [{"action_label": "a", "duration_ms": 1}]
		redundant_calls.analyze([recording], ctx)
		assert any("skipped 2 sidecar entries" in w for w in ctx.warnings)
