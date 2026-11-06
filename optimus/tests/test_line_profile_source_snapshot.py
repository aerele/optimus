"""The line-profile source snapshot must match what Python reports for the function."""

import importlib.util
import inspect
import sys
import tokenize
import types

import pytest

from optimus import analyze
from optimus.line_profile import capture
from optimus.renderer import source
from optimus.tests.test_auto_arm_phase2 import _ctx, arm_env  # noqa: F401

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
	logger = types.SimpleNamespace(warning=logged.append, info=logged.append)
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
	with pytest.raises(capture.CaptureError, match="have no readable source") as raised:
		REAL_PREPARE([make_pick("blank_in_body"), make_pick("one_liner")])
	assert len(capture_env) == 2
	with pytest.raises(capture.CaptureError, match="No selected function has eligible source") as no_eligible:
		REAL_PREPARE([{"dotted_path": f"{MODULE}.missing_fn", "source": "freeform"}])
	assert str(raised.value) != str(no_eligible.value)


def test_auto_arm_arms_a_function_with_blank_lines(arm_env, capture_env, monkeypatch):  # noqa: F811
	monkeypatch.setattr(
		capture.frappe,
		"logger",
		lambda name=None: types.SimpleNamespace(warning=lambda *a: None, info=lambda *a: None),
	)
	monkeypatch.setattr(capture, "prepare_line_profile_picks", REAL_PREPARE)
	arm_env.candidates = [{"dotted_path": f"{MODULE}.blank_in_docstring", "recommended": True}]
	analyze._auto_arm_phase2("PS-1", _ctx())
	assert len(arm_env.start_calls) == 1
	prepared = arm_env.start_calls[0]["picks"]
	snapshot = capture.json.loads(prepared.source_json)
	assert snapshot[f"{MODULE}.blank_in_docstring"] == expected_snapshot(
		sys.modules[MODULE].blank_in_docstring
	)
