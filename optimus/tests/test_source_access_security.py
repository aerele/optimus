"""Stored filenames and dotted paths are untrusted source-read requests."""

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from optimus import ai_jobs, analyze, server_script_source
from optimus.renderer import _internal, source, source_resolution

pytestmark = pytest.mark.rq


@pytest.fixture
def bench(tmp_path, monkeypatch):
	import frappe
	import frappe.utils

	root = tmp_path / "bench"
	for folder in ("sites", "apps/fake_app/fake_app", "env", "config", "logs", "archived"):
		(root / folder).mkdir(parents=True)
	monkeypatch.setattr(frappe, "local", SimpleNamespace(sites_path=str(root / "sites"), site="fake-site"), raising=False)
	monkeypatch.setattr(frappe, "flags", SimpleNamespace(in_test=False), raising=False)
	monkeypatch.setattr(frappe, "session", SimpleNamespace(user="fake-actor"), raising=False)
	monkeypatch.setattr(frappe, "get_installed_apps", lambda: ["fake_app"], raising=False)
	monkeypatch.setattr(frappe, "get_app_path", lambda app, *parts: str(root / "apps" / app / app / Path(*parts)), raising=False)
	monkeypatch.setattr(frappe.utils, "get_bench_path", lambda: str(root), raising=False)
	return root


def put(path, text="def example():\n    return 1\n"):
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text)
	return str(path)


def test_installed_app_read_failure_warns_once_without_exception_text(bench, monkeypatch):
	import sys

	import frappe
	seen = []
	monkeypatch.setattr(source, "_APPS_WARNING_SENT", False, raising=False)
	def fail():
		raise RuntimeError("private database detail")
	def warning(message, *args):
		assert sys.exception() is None
		seen.append(message % args)
	monkeypatch.setattr(frappe, "get_installed_apps", fail)
	monkeypatch.setattr(frappe, "logger", lambda name: SimpleNamespace(warning=warning), raising=False)
	for _ in range(3):
		assert source._installed_apps() == frozenset()
	assert len(seen) == 1 and "RuntimeError" in seen[0]
	assert "private" not in seen[0]


def test_source_open_wrapper_failure_closes_the_raw_descriptor(bench, monkeypatch):
	import os
	filename = put(bench / "apps/fake_app/fake_app/api.py")
	opened = []
	real_open = os.open
	def open_file(*args, **kwargs):
		fd = real_open(*args, **kwargs)
		opened.append(fd)
		return fd
	def fail(*args, **kwargs):
		raise OSError("fake descriptor wrapper failure")
	monkeypatch.setattr(source.os, "open", open_file)
	monkeypatch.setattr(source.os, "fdopen", fail)
	assert source._source_lines(filename) is None
	assert len(opened) == 1
	try:
		with pytest.raises(OSError):
			os.fstat(opened[0])
	finally:
		try:
			os.close(opened[0])
		except OSError:
			pass


@pytest.mark.parametrize("relative", ["sites/fake-site/site_config.json", "sites/other/private.py", "config/key.py", "logs/secret.py",
	"archived/old.py", "root.py", "apps/fake_app/private.json", "apps/fake_app/private.txt", "LOGS/secret.py"])
def test_non_source_or_private_bench_files_are_refused(bench, relative):
	filename = put(bench / relative)
	assert source._resolve_source_path(filename) is None
	assert source._source_lines(filename) is None


@pytest.mark.parametrize("relative", ["apps/fake_app/fake_app/api.py", "apps/fake_app/fake_app/view.html", "apps/fake_app/fake_app/client.js", "env/lib/helper.py"])
def test_normal_app_template_and_library_sources_remain_readable(bench, relative):
	filename = put(bench / relative)
	assert source._source_lines(filename) == ["def example():", "    return 1"]


def test_relative_and_parent_paths_use_real_boundary(bench):
	put(bench / "sites/private.py")
	valid = put(bench / "apps/fake_app/fake_app/api.py")
	assert Path(source._resolve_source_path("fake_app/api.py")).resolve() == Path(valid)
	for filename in ("fake_app/../../../sites/private.py", str(bench / "apps/../sites/private.py")):
		assert source._source_lines(filename) is None


def test_symlink_into_sites_is_refused_but_installed_soft_link_is_readable(bench, tmp_path):
	put(bench / "sites/private.py")
	(bench / "apps/evil").symlink_to(bench / "sites", target_is_directory=True)
	assert source._source_lines(str(bench / "apps/evil/private.py")) is None
	assert source._path_within_bench(str(bench / "apps/evil/private.py")) is False
	external = tmp_path / "external"
	filename = put(external / "fake_app/api.py")
	(bench / "apps/soft_app").symlink_to(external, target_is_directory=True)
	assert source._source_lines(filename)


def test_unknown_bench_fails_closed_even_when_source_exists(tmp_path, monkeypatch):
	import frappe

	monkeypatch.setattr(frappe, "local", SimpleNamespace(), raising=False)
	monkeypatch.setattr(frappe, "flags", SimpleNamespace(in_test=False), raising=False)
	assert source._source_lines(put(tmp_path / "private.py")) is None


def test_test_flag_relaxes_root_only(bench, tmp_path, monkeypatch):
	import frappe

	monkeypatch.setattr(frappe, "flags", SimpleNamespace(in_test=True), raising=False)
	assert source._source_lines(put(tmp_path / "fixture.py"))
	assert source._source_lines(put(tmp_path / "fixture.json")) is None
	assert source._source_lines(put(bench / "sites/private.py")) is None


def test_cached_lines_cannot_bypass_path_boundary(bench):
	filename = put(bench / "sites/private.py")
	cache = {filename: ["private stored value"]}
	assert source._source_lines(filename, cache=cache) is None
	assert source_resolution._skip_decorators_to_def(filename, 1, "example", cache=cache) == 1


def test_decorator_fallback_cannot_seed_a_denied_file(bench):
	filename = put(bench / "config/private.py", "@decorator\ndef example():\n    pass\n")
	cache = {}
	assert source_resolution._skip_decorators_to_def(filename, 1, "example", cache=cache) == 1
	assert not cache.get(filename)


def test_uninstalled_dotted_path_is_never_imported(bench, monkeypatch):
	imports = []
	monkeypatch.setattr(importlib, "import_module", lambda name: imports.append(name))
	assert source_resolution._resolve_dotted_to_code("uninstalled.evil.target") is None
	assert imports == []


def test_installed_app_import_is_allowed_and_bounded(bench, monkeypatch):
	filename = put(bench / "apps/fake_app/fake_app/api.py")
	namespace = {}
	exec(compile(Path(filename).read_text(), filename, "exec"), namespace)
	module = SimpleNamespace(example=namespace["example"])
	imports = []
	def load(name):
		imports.append(name)
		if name == "fake_app.api":
			return module
		raise ImportError()
	monkeypatch.setattr(importlib, "import_module", load)
	assert source_resolution._resolve_dotted_to_code("fake_app.api.example") == (filename, 1, "example")
	assert imports and all(name.startswith("fake_app") for name in imports)


@pytest.fixture
def scripts(bench, monkeypatch):
	import frappe

	state = {"allowed": {"fake-actor", "fake-owner"}, "body_reads": [], "checks": []}
	def permission(doctype, ptype, *, user, **kw):
		state["checks"].append((user, kw.get("doc")))
		return user in state["allowed"]
	def rows(doctype, fields):
		if "script" in fields:
			state["body_reads"].append("all bodies")
		return [{"name": "Fake Script", "script": "PRIVATE-SCRIPT-CANARY"}] if "script" in fields else [{"name": "Fake Script"}]
	def body(*a, **kw):
		state["body_reads"].append("one body")
		return "PRIVATE-SCRIPT-CANARY"
	monkeypatch.setattr(frappe, "has_permission", permission, raising=False)
	monkeypatch.setattr(frappe, "get_all", rows, raising=False)
	monkeypatch.setattr(frappe, "scrub", lambda value: value.lower().replace(" ", "_"), raising=False)
	monkeypatch.setattr(frappe, "db", SimpleNamespace(get_value=body), raising=False)
	return state


@pytest.mark.parametrize("denied", ["fake-actor", "fake-owner"])
def test_server_script_requires_actor_and_principal_before_read(scripts, denied):
	scripts["allowed"].remove(denied)
	with source.server_script_readers("fake-owner"):
		assert source._source_lines("<serverscript>: fake_script") is None
	assert not scripts["body_reads"]


def test_server_script_cached_body_rechecks_both_principals(scripts):
	cache = {}
	with source.server_script_readers("fake-owner"):
		assert source._source_lines("<serverscript>: fake_script", cache=cache) == ["PRIVATE-SCRIPT-CANARY"]
		scripts["allowed"].remove("fake-owner")
		assert source._source_lines("<serverscript>: fake_script", cache=cache) is None
		assert server_script_source.get_server_script_record("fake_script", cache=cache) is None
	assert scripts["body_reads"] == ["one body"]


def test_context_is_reset_after_nested_error(scripts):
	with source.server_script_readers("fake-owner"):
		with pytest.raises(ValueError), source.server_script_readers("denied-principal"):
			raise ValueError("fake")
		assert source._source_lines("<serverscript>: fake_script")
	assert source._SCRIPT_READERS.get() == ()


def test_payload_and_report_set_the_correct_principal(scripts, monkeypatch):
	seen = []
	def operation(*a, **kw):
		seen.append(source._SCRIPT_READERS.get())
		return "result"
	monkeypatch.setattr(analyze, "_ai_payload_for_finding", operation)
	assert ai_jobs._build_payload({}, {}, {}, principal="fake-requester") == "result"
	monkeypatch.setattr(_internal, "render", operation)
	assert _internal.render_raw(SimpleNamespace(owner="fake-owner"), []) == "result"
	assert seen == [("fake-requester",), ("fake-owner",)]
	assert source._SCRIPT_READERS.get() == ()


def test_import_timeout_stops_resolution_instead_of_being_swallowed(bench, monkeypatch):
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	interrupt = Timeout("fake timeout")
	def fail(name):
		raise interrupt
	monkeypatch.setattr(importlib, "import_module", fail)
	with pytest.raises(Timeout) as caught:
		source_resolution._resolve_dotted_to_code("fake_app.api.example")
	assert caught.value is not interrupt


def test_document_permission_is_checked_before_script_body(scripts, monkeypatch):
	import frappe

	monkeypatch.setattr(frappe, "has_permission", lambda *a, **kw: not kw.get("doc"), raising=False)
	assert server_script_source.get_server_script_record("fake_script") is None
	assert scripts["body_reads"] == []


def test_permission_timeout_escapes_without_reading_body(scripts, monkeypatch):
	import frappe

	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	interrupt = Timeout("fake timeout")
	def fail(*a, **kw):
		raise interrupt
	monkeypatch.setattr(frappe, "has_permission", fail, raising=False)
	with pytest.raises(Timeout) as caught:
		source._source_lines("<serverscript>: fake_script")
	assert caught.value is not interrupt and not scripts["body_reads"]


def test_soft_linked_frappe_does_not_change_bench(bench, tmp_path, monkeypatch):
	import frappe.utils

	monkeypatch.setattr(frappe.utils, "get_bench_path", lambda: str(tmp_path / "different"), raising=False)
	assert source._source_lines(put(bench / "apps/fake_app/fake_app/real.py"))
	assert source._source_lines(put(bench / "sites/private.py")) is None


def test_large_source_is_omitted(bench):
	filename = put(bench / "apps/fake_app/fake_app/large.py", "x" * (4 * 1024 * 1024 + 1))
	assert source._source_lines(filename) is None


def test_baked_server_script_snippet_cannot_bypass_read_permission(scripts):
	import json

	scripts["allowed"].remove("fake-actor")
	child = SimpleNamespace(finding_type="Hot Line", severity="High", title="fake", customer_description="",
		estimated_impact_ms=1, affected_count=1, action_ref=0, llm_fix_json=None,
		technical_detail_json=json.dumps({"callsite": {"filename": "<serverscript>: fake_script", "lineno": 1,
			"source_snippet": [{"lineno": 1, "content": "PRIVATE-SCRIPT-CANARY"}]}, "line_content": "PRIVATE-SCRIPT-CANARY"}))
	with source.server_script_readers("fake-owner"):
		payload = analyze._ai_payload_for_finding(child, {})
	assert "PRIVATE-SCRIPT-CANARY" not in json.dumps(payload)
	assert scripts["body_reads"] == []


def test_frame_resolution_preserves_import_interrupt(bench, monkeypatch):
	Timeout = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	interrupt = Timeout("fake timeout")
	def fail(*a, **kw):
		raise interrupt
	monkeypatch.setattr(source_resolution, "_resolve_dotted_to_code", fail)
	with pytest.raises(Timeout) as caught:
		source_resolution._resolve_frame_key_to_callsite("fake_app/api.py::example")
	assert caught.value is not interrupt


def test_cached_snapshot_keeps_allowed_source_after_file_removal(bench):
	filename = put(bench / "apps/fake_app/fake_app/removed.py")
	cache = {}
	assert source._source_lines(filename, cache=cache)
	Path(filename).unlink()
	assert source._source_lines(filename, cache=cache) == ["def example():", "    return 1"]


def test_cached_script_rechecks_document_permission(scripts, monkeypatch):
	import frappe

	cache = {}
	assert source._source_lines("<serverscript>: fake_script", cache=cache)
	monkeypatch.setattr(frappe, "has_permission", lambda *a, **kw: not kw.get("doc"), raising=False)
	assert source._source_lines("<serverscript>: fake_script", cache=cache) is None
	assert scripts["body_reads"] == ["one body"]


def test_analyze_enrichment_checks_session_owner(scripts):
	import json

	scripts["allowed"].remove("fake-owner")
	findings = [{"technical_detail_json": json.dumps({"callsite": {"filename": "<serverscript>: fake_script", "lineno": 1}})}]
	analyze._enrich_findings_with_source_snippets(findings, owner="fake-owner")
	assert "source_snippet" not in json.loads(findings[0]["technical_detail_json"])["callsite"]
	assert not scripts["body_reads"]
