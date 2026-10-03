"""Consent must cover existing analyzer evidence as well as recorded examples."""

import copy
import importlib
import json
from types import SimpleNamespace

import pytest

from optimus import ai_fix, analyze, settings

pytestmark = pytest.mark.rq
PRIVATE = "PRIVATE-CUSTOMER-CANARY"


@pytest.fixture
def privacy():
	return importlib.import_module("optimus.ai_privacy")


@pytest.mark.parametrize("query", [
	f"SELECT name FROM `tabCustomer` WHERE name = '{PRIVATE}' AND amount = 912345",
	f'SELECT name FROM `tabCustomer` WHERE name = "{PRIVATE}"',
	f"SELECT name FROM customer WHERE name = $tag${PRIVATE}$tag$",
	f"SELECT name FROM customer /* {PRIVATE} */ WHERE amount = 912345 -- {PRIVATE}",
	f"SELECT name FROM customer WHERE name = E'{PRIVATE}'; SELECT '{PRIVATE}'",
])
def test_sql_literals_and_comments_do_not_leave_by_default(privacy, query):
	result = privacy.query_text(query, send_raw=False)
	assert result and "SELECT" in result
	assert PRIVATE not in result and "912345" not in result


@pytest.mark.parametrize("query", [None, {}, 17, "SELECT 'unclosed PRIVATE-CUSTOMER-CANARY", "SELECT 1 /* PRIVATE-CUSTOMER-CANARY", "SELECT " + "x" * 100_000])
def test_malformed_or_excessive_sql_is_omitted(privacy, query):
	assert privacy.query_text(query, send_raw=False) == ""


def test_opt_in_keeps_business_literals_but_still_redacts_sensitive_columns(privacy):
	query = f"SELECT * FROM `tabCustomer` WHERE name='{PRIVATE}' AND password='PASSWORD-CANARY'"
	result = privacy.query_text(query, send_raw=True)
	assert PRIVATE in result and "PASSWORD-CANARY" not in result


@pytest.mark.parametrize("value,expected", [(None, False), (False, False), ("0", False), ("false", False), (2, False), ({}, False),
	(True, True), (1, True), ("1", True)])
def test_raw_consent_is_explicit_and_defaults_off(privacy, monkeypatch, value, expected):
	import frappe

	monkeypatch.setattr(settings, "get_config", lambda: SimpleNamespace(ai_send_raw_values=value))
	monkeypatch.setattr(frappe, "db", SimpleNamespace(get_single_value=lambda *a, **k: value), raising=False)
	assert privacy.raw_values_enabled() is expected


def test_cached_consent_cannot_override_a_revocation(privacy, monkeypatch):
	import frappe

	monkeypatch.setattr(settings, "get_config", lambda: SimpleNamespace(ai_send_raw_values=True))
	reads = []
	def stored(doctype, field, *, cache):
		reads.append((doctype, field, cache))
		return 0
	monkeypatch.setattr(frappe, "db", SimpleNamespace(get_single_value=stored), raising=False)
	assert privacy.raw_values_enabled() is False
	assert reads == [("Optimus Settings", "ai_send_raw_values", False)]


def test_consent_failure_is_private_but_worker_interrupt_still_escapes(privacy, monkeypatch):
	def fail():
		raise RuntimeError("unavailable")
	monkeypatch.setattr(settings, "get_config", fail)
	assert privacy.raw_values_enabled() is False
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	interrupt = Timeout("fake timeout")
	def interrupted():
		raise interrupt
	monkeypatch.setattr(settings, "get_config", interrupted)
	with pytest.raises(Timeout) as caught:
		privacy.raw_values_enabled()
	assert caught.value is not interrupt


def test_preexisting_analyzer_queries_are_filtered_at_prompt_construction():
	finding = {"finding_type": "Slow Query", "title": "Slow query", "technical_detail": {
		"normalized_query": f"SELECT * FROM `tabCustomer` WHERE name='{PRIVATE}'",
		"example_queries": [f"SELECT name FROM `tabCustomer` WHERE name='{PRIVATE}'"],
	}}
	before = copy.deepcopy(finding)
	_, messages, _ = ai_fix._build_fix_request(finding, threshold_ms=1000, context_tokens=32768)
	assert PRIVATE not in json.dumps(messages)
	assert "tabCustomer" in json.dumps(messages)
	assert finding == before
	_, messages, _ = ai_fix._build_fix_request(finding, threshold_ms=1000, context_tokens=32768, send_raw=True)
	assert PRIVATE in json.dumps(messages)


def test_context_only_recorded_examples_are_normalized_without_mutating_capture(monkeypatch):
	monkeypatch.setattr(settings, "get_config", lambda: SimpleNamespace(ai_send_raw_values=False))
	recordings = {"fake-recording": {"calls": [
		{"query": f"SELECT name FROM `tabCustomer` WHERE name='{PRIVATE}'", "duration": 12},
		{"query": "SELECT name FROM `tabCustomer` WHERE name='another'", "duration": 10},
	]}}
	before = copy.deepcopy(recordings)
	payload = {}
	analyze._maybe_attach_recorded_queries(payload, action_ref=1, recordings_by_uuid=recordings,
		actions_by_idx={1: {"recording_uuid": "fake-recording"}})
	assert PRIVATE not in json.dumps(payload)
	assert len(payload["technical_detail"]["example_queries"]) == 1
	assert recordings == before


@pytest.mark.parametrize("path,expected", [
	(f"/app/customer/{PRIVATE}", "/app/customer/<name>"),
	(f"/api/resource/Customer/{PRIVATE}?name={PRIVATE}", "/api/resource/Customer/<name>"),
	(f"/api/v2/document/Customer/{PRIVATE}", "/api/v2/document/Customer/<name>"),
	("/api/method/frappe.desk.form.load.getdoc", "/api/method/frappe.desk.form.load.getdoc"),
	("custom.jobs.run", "custom.jobs.run"),
	(f"/unknown/{PRIVATE}", "/<path>"),
	(f"{PRIVATE}", "<path>"),
])
def test_paths_keep_only_route_shapes_by_default(privacy, path, expected):
	assert privacy.path_without_names(path) == expected


def test_steps_hide_record_names_and_session_title_without_opt_in():
	actions = [{"label": f"Open Customer {PRIVATE}", "doctype": "Customer", "cmd": "frappe.desk.form.load.getdoc",
		"path": f"/app/customer/{PRIVATE}", "method": "GET", "duration_ms": 1}]
	before = copy.deepcopy(actions)
	_, messages = ai_fix._build_steps_messages(actions, session_title=PRIVATE)
	assert PRIVATE not in json.dumps(messages) and "Customer" in json.dumps(messages)
	assert actions == before
	_, messages = ai_fix._build_steps_messages(actions, session_title=PRIVATE, send_raw=True)
	assert PRIVATE in json.dumps(messages)


def test_recorded_steps_drop_names_without_mutating_originals(monkeypatch):
	monkeypatch.setattr(settings, "get_config", lambda: SimpleNamespace(ai_send_raw_values=False))
	recordings = [{"cmd": "frappe.desk.form.load.getdoc", "form_dict": {"doctype": "Customer", "name": PRIVATE},
		"path": "/api/method/frappe.desk.form.load.getdoc", "method": "GET", "duration": 12}]
	before = copy.deepcopy(recordings)
	result = analyze._actions_for_humanizer(recordings)
	assert result and PRIVATE not in json.dumps(result) and "Customer" in json.dumps(result)
	assert recordings == before


def test_config_and_settings_field_default_to_private():
	from pathlib import Path

	assert settings.OptimusConfig().ai_send_raw_values is False
	assert settings._DEFAULTS["ai_send_raw_values"] is False
	path = Path(settings.__file__).parent / "optimus/doctype/optimus_settings/optimus_settings.json"
	fields = {field["fieldname"]: field for field in json.loads(path.read_text())["fields"]}
	assert fields["ai_send_raw_values"]["fieldtype"] == "Check"
	assert fields["ai_send_raw_values"]["default"] == "0"


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("entry", ["fix", "steps"])
def test_real_entry_points_apply_current_consent_to_outbound_messages(monkeypatch, enabled, entry):
	import frappe

	monkeypatch.setattr(settings, "get_config", lambda: SimpleNamespace(ai_send_raw_values=enabled))
	monkeypatch.setattr(frappe, "db", SimpleNamespace(get_single_value=lambda *a, **k: enabled), raising=False)
	monkeypatch.setattr(ai_fix, "_provider_config", lambda: {
		"name": "OpenAI-compatible", "protocol": "openai", "model": "fake", "needs_key": False,
		"base_url": "https://provider.invalid/v1", "context_tokens": 32768,
	})
	monkeypatch.setattr(ai_fix, "llm_gate_note", lambda *a: None)
	monkeypatch.setattr(ai_fix, "_reask_enabled", lambda: False)
	sent = []
	def dispatch(provider, system, messages, **kwargs):
		sent.append(copy.deepcopy(messages))
		return "**Fix**\n\nNo code changes are needed."
	monkeypatch.setattr(ai_fix, "_dispatch_call", dispatch)
	if entry == "fix":
		ai_fix.suggest_fix({"finding_type": "Slow Query", "title": "Slow query", "technical_detail": {
			"example_queries": [f"SELECT name FROM customer WHERE name='{PRIVATE}'"],
		}})
	else:
		ai_fix.humanize_steps([{"label": f"Open Customer {PRIVATE}", "cmd": "frappe.desk.form.load.getdoc", "doctype": "Customer"}], session_title=PRIVATE)
	assert sent
	assert (PRIVATE in json.dumps(sent)) is enabled


def test_parser_failure_does_not_echo_raw_query(privacy, monkeypatch, capsys):
	def fail(*a):
		raise ValueError(PRIVATE)
	monkeypatch.setattr(privacy.sqlparse, "parse", fail)
	assert privacy.query_text(f"SELECT '{PRIVATE}'", send_raw=False) == ""
	assert capsys.readouterr().out == ""


def test_parser_interrupt_discards_private_frames(privacy, monkeypatch):
	interrupt = SystemExit(1)
	def fail(query):
		raise interrupt
	monkeypatch.setattr(privacy.sqlparse, "parse", fail)
	with pytest.raises(SystemExit) as caught:
		privacy.query_text(f"SELECT '{PRIVATE}'", send_raw=False)
	assert caught.value is interrupt
	tb = caught.value.__traceback__
	while tb:
		if tb.tb_frame.f_code.co_filename == privacy.__file__:
			assert all(PRIVATE not in repr(value) for value in tb.tb_frame.f_locals.values())
		tb = tb.tb_next


def test_compact_actions_preserve_submit_without_document_names():
	recordings = [{"cmd": "frappe.desk.form.save.savedocs", "duration": 1,
		"form_dict": {"action": "Submit", "doc": json.dumps({"doctype": "Sales Invoice", "name": PRIVATE})}}]
	actions = analyze._actions_for_humanizer(recordings, send_raw=False)
	_, messages = ai_fix._build_steps_messages(actions, session_title="")
	assert "Submit Sales Invoice" in json.dumps(messages)
	assert PRIVATE not in json.dumps(messages)


def test_compact_action_json_interrupt_is_not_swallowed(monkeypatch):
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	interrupt = Timeout("fake timeout")
	def fail(*a, **kw):
		raise interrupt
	monkeypatch.setattr(json, "loads", fail)
	with pytest.raises(Timeout) as caught:
		analyze._actions_for_humanizer([{"cmd": "frappe.desk.form.save.savedocs",
			"form_dict": {"doc": "{}"}}], send_raw=False)
	assert caught.value is not interrupt


def test_malformed_action_fields_do_not_fail_valid_actions():
	data = [None, [], {"cmd": [], "path": {}, "duration": "bad", "form_dict": {"doctype": []}},
		{"cmd": "frappe.client.get_list", "duration": 12, "form_dict": {"doctype": "Customer"}}]
	actions = analyze._actions_for_humanizer(data, send_raw=False)
	assert actions[-1]["label"] == "List Customer"
	assert all(isinstance(action["duration_ms"], (int, float)) for action in actions)
