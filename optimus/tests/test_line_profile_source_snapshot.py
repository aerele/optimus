"""The line-profile source snapshot must match what Python reports for the function."""

import importlib.util
import inspect
import sys
import tokenize
import types

import frappe
import pytest

from optimus import analyze
from optimus.line_profile import capture
from optimus.renderer import source

pytestmark = pytest.mark.rq

MODULE = "optimus_snapshot_fixture"
REAL_PREPARE = capture.prepare_line_profile_picks

FIXTURE_SOURCE = '''import functools


def blank_in_body(a):
	x = a

	y = x + 1


	return y


def blank_in_docstring(a):
	"""First paragraph.

	Second paragraph.
	"""
	return a


def deco(fn):
	@functools.wraps(fn)
	def inner(*args):
		return fn(*args)
	return inner


@deco
def decorated(a):

	return a


async def async_fn(a):
	b = a

	return b


def one_liner(a): return a


def last_function(a):
	x = a

	return x'''

BAD_SOURCE = "def broken(a):\n\tx = (\n\treturn x\n"


@pytest.fixture
def fixture_module(tmp_path, monkeypatch):
	path = tmp_path / f"{MODULE}.py"
	path.write_text(FIXTURE_SOURCE)
	spec = importlib.util.spec_from_file_location(MODULE, path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	monkeypatch.setitem(sys.modules, MODULE, module)
	monkeypatch.setattr(source, "_installed_apps", lambda: {MODULE})
	return module


@pytest.fixture
def capture_env(fixture_module, monkeypatch):
	logged = []

	def log(message, *args):
		logged.append(message % args)

	logger = types.SimpleNamespace(warning=log, info=log)
	monkeypatch.setattr(capture, "frappe", types.SimpleNamespace(logger=lambda name=None: logger))
	monkeypatch.setattr(capture, "_FRAPPE_AVAILABLE", True)
	monkeypatch.setattr(capture, "_LP_AVAILABLE", True)
	return logged


def expected_snapshot(fn):
	lines, first = inspect.getsourcelines(fn)
	return [{"lineno": first + i, "content": line.rstrip("\n")} for i, line in enumerate(lines)]


@pytest.mark.parametrize(
	"name", ["blank_in_body", "blank_in_docstring", "decorated", "async_fn", "one_liner", "last_function"]
)
def test_snapshot_matches_inspect_getsourcelines(fixture_module, name):
	fn = getattr(fixture_module, name)
	assert capture._capture_source_lines(fn) == expected_snapshot(fn)


def test_blank_line_does_not_cut_the_function(fixture_module):
	lines = capture._capture_source_lines(fixture_module.blank_in_body)
	assert [line["content"] for line in lines][-1] == "\treturn y"


@pytest.mark.parametrize("error", [SyntaxError, tokenize.TokenError])
def test_unreadable_source_returns_empty_snapshot(fixture_module, monkeypatch, error):
	def fail(lines):
		raise error("fake")

	monkeypatch.setattr(capture, "inspect", types.SimpleNamespace(unwrap=inspect.unwrap, getblock=fail))
	assert capture._capture_source_lines(fixture_module.blank_in_body) == []


def test_unparseable_source_returns_empty_snapshot(tmp_path, monkeypatch):
	path = tmp_path / "bad_snapshot_fixture.py"
	path.write_text(BAD_SOURCE)
	monkeypatch.setattr(source, "_installed_apps", lambda: {"bad_snapshot_fixture"})
	assert source._source_lines(str(path))

	def stand_in():
		pass

	stand_in.__code__ = stand_in.__code__.replace(co_filename=str(path), co_firstlineno=1)
	assert capture._capture_source_lines(stand_in) == []


def make_pick(name):
	return {"dotted_path": f"{MODULE}.{name}", "source": "freeform"}


def test_prepare_keeps_the_full_source_for_functions_with_blank_lines(capture_env):
	prepared = REAL_PREPARE([make_pick("blank_in_body"), make_pick("blank_in_docstring")])
	assert [r["dotted_path"] for r in prepared.resolved] == [
		f"{MODULE}.blank_in_body",
		f"{MODULE}.blank_in_docstring",
	]
	snapshot = capture.json.loads(prepared.source_json)
	assert snapshot[f"{MODULE}.blank_in_body"][-1]["content"] == "\treturn y"
	assert snapshot[f"{MODULE}.blank_in_docstring"][-1]["content"] == "\treturn a"


def test_mixed_selection_marks_the_unreadable_pick_ineligible(capture_env, monkeypatch):
	real = capture._capture_source_lines
	bad = f"{MODULE}.one_liner"
	monkeypatch.setattr(
		capture, "_capture_source_lines", lambda fn: [] if fn.__name__ == "one_liner" else real(fn)
	)
	prepared = REAL_PREPARE([make_pick("blank_in_body"), make_pick("one_liner")])
	by_path = {r["dotted_path"]: r for r in prepared.resolved}
	assert list(by_path) == [f"{MODULE}.blank_in_body", bad]
	assert by_path[f"{MODULE}.blank_in_body"]["eligible"] is True
	assert by_path[bad]["eligible"] is False
	assert by_path[bad]["ineligible_reason"]
	assert sum(1 for r in prepared.resolved if r["eligible"]) == 1
	assert [p["dotted_path"] for p in capture.json.loads(prepared.picks_json)] == [f"{MODULE}.blank_in_body"]
	assert list(capture.json.loads(prepared.source_json)) == [f"{MODULE}.blank_in_body"]
	assert len(capture_env) == 1 and bad in capture_env[0]


def test_selection_without_any_readable_source_is_refused(capture_env, monkeypatch):
	monkeypatch.setattr(capture, "_capture_source_lines", lambda fn: [])
	with pytest.raises(capture.CaptureError, match="have no readable source"):
		REAL_PREPARE([make_pick("blank_in_body"), make_pick("one_liner")])
	assert len(capture_env) == 2


def test_selection_without_any_eligible_function_is_refused(capture_env):
	with pytest.raises(capture.CaptureError, match="No selected function has eligible source"):
		REAL_PREPARE([make_pick("missing_fn")])


def test_oversized_line_marks_the_pick_ineligible(capture_env, monkeypatch):
	real = capture._capture_source_lines

	def wide(fn):
		lines = real(fn)
		if fn.__name__ == "one_liner":
			lines[0]["content"] = "x" * (capture.MAX_SOURCE_LINE_CHARS + 1)
		return lines

	monkeypatch.setattr(capture, "_capture_source_lines", wide)
	prepared = REAL_PREPARE([make_pick("blank_in_body"), make_pick("one_liner")])
	by_path = {r["dotted_path"]: r for r in prepared.resolved}
	assert by_path[f"{MODULE}.blank_in_body"]["eligible"] is True
	assert by_path[f"{MODULE}.one_liner"]["eligible"] is False
	assert "too large" in by_path[f"{MODULE}.one_liner"]["ineligible_reason"]
	assert list(capture.json.loads(prepared.source_json)) == [f"{MODULE}.blank_in_body"]


def test_snapshot_over_the_line_budget_marks_the_later_pick_ineligible(capture_env, monkeypatch):
	monkeypatch.setattr(capture, "MAX_SOURCE_LINES", len(expected_snapshot(sys.modules[MODULE].blank_in_body)))
	prepared = REAL_PREPARE([make_pick("blank_in_body"), make_pick("blank_in_docstring")])
	by_path = {r["dotted_path"]: r for r in prepared.resolved}
	assert by_path[f"{MODULE}.blank_in_body"]["eligible"] is True
	assert by_path[f"{MODULE}.blank_in_docstring"]["eligible"] is False
	assert "too large" in by_path[f"{MODULE}.blank_in_docstring"]["ineligible_reason"]


def test_only_oversized_picks_are_refused_with_a_capture_error(capture_env, monkeypatch):
	monkeypatch.setattr(capture, "MAX_SOURCE_LINES", 1)
	with pytest.raises(capture.CaptureError, match="have no readable source"):
		REAL_PREPARE([make_pick("blank_in_body")])


def test_crlf_source_file_snapshots_like_inspect(tmp_path, monkeypatch):
	name = "optimus_crlf_fixture"
	path = tmp_path / f"{name}.py"
	path.write_bytes(FIXTURE_SOURCE.replace("\n", "\r\n").encode())
	spec = importlib.util.spec_from_file_location(name, path)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	monkeypatch.setattr(source, "_installed_apps", lambda: {name})
	snapshot = capture._capture_source_lines(module.blank_in_body)
	assert snapshot == expected_snapshot(module.blank_in_body)
	assert snapshot[-1]["content"] == "\treturn y"
	assert not any("\r" in line["content"] for line in snapshot)


def _function_from_file(tmp_path, monkeypatch, name, data):
	path = tmp_path / f"{name}.py"
	path.write_bytes(data)
	monkeypatch.setattr(source, "_installed_apps", lambda: {name})

	def stand_in():
		pass

	stand_in.__code__ = stand_in.__code__.replace(co_filename=str(path), co_firstlineno=1)
	return stand_in, path


def test_deleted_source_file_is_not_readable(tmp_path, monkeypatch):
	fn, path = _function_from_file(tmp_path, monkeypatch, "optimus_gone_fixture", b"def f(a):\n\treturn a\n")
	assert source._source_lines(str(path))
	path.unlink()
	assert source._source_lines(str(path)) is None
	assert capture._capture_source_lines(fn) == []


def test_non_utf8_source_file_is_not_readable(tmp_path, monkeypatch):
	data = b"def f(a):\n\treturn '\xff'\n"
	fn, path = _function_from_file(tmp_path, monkeypatch, "optimus_latin_fixture", data)
	assert source._source_lines(str(path)) is None
	assert capture._capture_source_lines(fn) == []


@pytest.mark.parametrize("damage", ["delete", "non_utf8"])
def test_unreadable_file_reaches_the_ineligible_path(capture_env, fixture_module, damage):
	path = fixture_module.__file__
	assert source._source_lines(path)
	if damage == "delete":
		import os

		os.unlink(path)
	else:
		with open(path, "ab") as fh:
			fh.write(b"\n# \xff\n")
	with pytest.raises(capture.CaptureError, match="have no readable source"):
		REAL_PREPARE([make_pick("blank_in_body")])
	assert len(capture_env) == 1


@pytest.fixture
def auto_arm(capture_env, monkeypatch):
	"""Run the real prepare step inside _arm_phase2 with the persistence layer stubbed."""
	import optimus.line_profile.jobs as jobs
	import optimus.line_profile.picker as picker
	import optimus.settings as settings
	from optimus import api, realtime

	doc = types.SimpleNamespace(
		user="u@x.com", owner="u@x.com", session_uuid="sess-1", get=lambda key: [], name="PS-1"
	)
	state = types.SimpleNamespace(candidates=[], persisted=[], events=[])
	monkeypatch.setattr(frappe, "conf", types.SimpleNamespace(get=lambda k, d=None: True), raising=False)
	monkeypatch.setattr(frappe, "cache", types.SimpleNamespace(get_value=lambda k: None), raising=False)
	monkeypatch.setattr(frappe, "get_doc", lambda *a, **k: doc, raising=False)
	monkeypatch.setattr(frappe, "db", types.SimpleNamespace(rollback=lambda: None), raising=False)
	monkeypatch.setattr(settings, "get_config", lambda: types.SimpleNamespace(phase2_max_runs_per_session=10))
	monkeypatch.setattr(settings, "get_ignored_apps", lambda: [])
	monkeypatch.setattr(picker, "_build_tree_indented_candidates", lambda trees: state.candidates)
	monkeypatch.setattr(jobs, "_authorized", lambda parent, user: True)
	monkeypatch.setattr(api, "_lock_phase2_for_write", lambda docname: None)
	monkeypatch.setattr(
		api,
		"_persist_phase2_capture",
		lambda parent, docname, session_uuid, run_uuid, user, prepared: (
			state.persisted.append(prepared) or list(prepared.resolved)
		),
	)
	monkeypatch.setattr(realtime, "publish_session_event", lambda event, payload: state.events.append((event, payload)))
	return state


def test_auto_arm_arms_a_function_with_blank_lines(auto_arm):
	auto_arm.candidates = [{"dotted_path": f"{MODULE}.blank_in_docstring", "recommended": True}]
	analyze._arm_phase2("PS-1", types.SimpleNamespace(session_uuid="sess-1"))
	(prepared,) = auto_arm.persisted
	snapshot = capture.json.loads(prepared.source_json)
	assert snapshot[f"{MODULE}.blank_in_docstring"] == expected_snapshot(sys.modules[MODULE].blank_in_docstring)


def test_auto_arm_event_excludes_ineligible_picks_of_a_mixed_selection(auto_arm, monkeypatch):
	real = capture._capture_source_lines
	monkeypatch.setattr(
		capture, "_capture_source_lines", lambda fn: [] if fn.__name__ == "one_liner" else real(fn)
	)
	auto_arm.candidates = [
		{"dotted_path": f"{MODULE}.blank_in_body", "recommended": True},
		{"dotted_path": f"{MODULE}.one_liner", "recommended": True},
	]
	analyze._arm_phase2("PS-1", types.SimpleNamespace(session_uuid="sess-1"))
	assert len(auto_arm.persisted[0].resolved) == 2
	((event, payload),) = auto_arm.events
	assert event == "optimus_phase2_armed"
	assert payload["count"] == 1
	assert payload["functions"] == ["blank_in_body"]
