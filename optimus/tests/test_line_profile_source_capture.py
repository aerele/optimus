"""The line-profile source snapshot must hold the whole function, blank lines included."""

import importlib.util
import inspect
import itertools
import json
import sys

import pytest

from optimus import ai_fix, analyze
from optimus.line_profile import capture
from optimus.renderer import source

pytestmark = pytest.mark.rq

BLANKS = "def f(a):\n\tx = a + 1\n\n\ty = x * 2\n\n\treturn y\n"

CASES = {
	"blank_lines": BLANKS,
	"column_zero_sql_docstring": 'def f():\n\t"""\nselect 1\n\nfrom tab\n\t"""\n\n\treturn 1\n',
	"multiline_decorator": "import functools\n\n@functools.lru_cache(\n\tmaxsize=2,\n)\ndef f(a):\n\n\treturn a\n",
	"stacked_decorators": "import functools\n\n@functools.lru_cache\n@functools.lru_cache(maxsize=1)\ndef f(a):\n\n\treturn a\n",
	"nested_def": "def f():\n\tdef g():\n\t\treturn 1\n\n\treturn g()\n",
	"async_def": "async def f():\n\tx = 1\n\n\treturn x\n",
	"one_liner_before": "def other(): return 0\n\n\ndef f():\n\n\treturn 1\n",
	"crlf": "def f():\r\n\tx = 1\r\n\r\n\treturn x\r\n",
	"lone_cr": "def f():\r\tx = 1\r\r\treturn x\r",
	"backslash_def": "def \\\n\tf(a):\n\n\treturn a\n",
	"lambda_pick": "f = lambda a: (\n\ta\n\n\t+ 1\n)\n",
	"unicode_line_separator": 'def f():\n\ts = "a\u2028b"\n\n\treturn s\n',
	"form_feed": "def f():\n\tx = 1\n\f\n\treturn x\n",
	"backslash_continuation": "def f():\n\tx = 1 + \\\n\t\t2\n\n\treturn x\n",
	"no_eol_at_eof": "def f():\n\tx = 1\n\n\treturn x",
	"last_method": "class C:\n\tdef a(self):\n\t\treturn 0\n\n\tdef f(self):\n\n\t\treturn 1\n",
	"multiline_fstring": 'def f(a):\n\tq = f"""\n\tselect {a}\n\n\tfrom t\n\t"""\n\treturn q\n',
	"trailing_comment": "def f():\n\tx = 1\n\n\treturn x\n\t# trailing\n",
	"conditional_def": "if True:\n\tdef f():\n\n\t\treturn 1\n",
	"blank_inside_brackets": "def f():\n\tx = [\n\t\t1,\n\n\t\t2,\n\t]\n\treturn x\n",
}
counter = itertools.count()
CORPUS_PATHS = [capture.__file__, source.__file__, analyze.__file__, ai_fix.__file__]


def load(tmp_path, monkeypatch, text, *, data=None):
	path = tmp_path / f"lp_mod_{next(counter)}.py"
	path.write_bytes(data if data is not None else text.encode())
	monkeypatch.setattr(source, "_resolve_source_path", lambda filename, must_exist=True: str(path))
	spec = importlib.util.spec_from_file_location(path.stem, path)
	module = importlib.util.module_from_spec(spec)
	monkeypatch.setitem(sys.modules, path.stem, module)
	spec.loader.exec_module(module)
	return path, module.C.f if hasattr(module, "C") else module.f


def truth(fn):
	lines, start = inspect.getsourcelines(fn)
	return [{"lineno": start + i, "content": line.rstrip("\n")} for i, line in enumerate(lines)]


def prepare(monkeypatch, fn):
	from optimus.line_profile import picker

	monkeypatch.setattr(capture, "_LP_AVAILABLE", True)
	monkeypatch.setattr(source, "_installed_apps", lambda: {"optimus"})
	monkeypatch.setattr(
		picker,
		"resolve_freeform",
		lambda dotted: {
			"dotted_path": dotted,
			"eligible": True,
			"qualname": fn.__qualname__,
			"file": "x.py",
			"lineno": 1,
		},
	)
	monkeypatch.setattr(capture, "_resolve_attr", lambda dotted: fn)
	return capture.prepare_line_profile_picks([{"dotted_path": "optimus.mod.f"}])


@pytest.mark.parametrize("text", CASES.values(), ids=CASES.keys())
def test_snapshot_equals_the_interpreters_own_function_source(tmp_path, monkeypatch, text):
	_, fn = load(tmp_path, monkeypatch, text)
	assert capture._capture_source_lines(fn) == truth(fn)


@pytest.mark.parametrize("text", CASES.values(), ids=CASES.keys())
def test_prepared_run_snapshots_the_whole_function(tmp_path, monkeypatch, text):
	_, fn = load(tmp_path, monkeypatch, text)
	prepared = prepare(monkeypatch, fn)
	assert json.loads(prepared.source_json)["optimus.mod.f"] == truth(fn)


def test_bom_file_with_def_on_the_first_line(tmp_path, monkeypatch):
	data = b"\xef\xbb\xbf" + b"def f():\n\tx = 1\n\n\treturn x\n"
	_, fn = load(tmp_path, monkeypatch, None, data=data)
	assert fn.__code__.co_firstlineno == 1
	assert capture._capture_source_lines(fn) == truth(fn)


def _functions_of(path):
	stack = [compile(open(path, encoding="utf-8").read(), path, "exec")]
	while stack:
		code = stack.pop()
		for const in code.co_consts:
			if not inspect.iscode(const):
				continue
			stack.append(const)
			if const.co_flags & inspect.CO_OPTIMIZED and not const.co_name.startswith(("<", "__annotate")):
				yield const


def _check_corpus_module(monkeypatch, path):
	"""Assert every function of the module is captured whole; return how many hold a blank line."""
	monkeypatch.setattr(source, "_resolve_source_path", lambda filename, must_exist=True: path)
	lines = source._source_lines(path)
	codes = list(_functions_of(path))
	blank_inside = 0
	for code in codes:
		fn = type("Fn", (), {"__code__": code})
		rows = capture._capture_source_lines(fn)
		last = max(ln for _, _, ln in code.co_lines() if ln)
		assert rows[0]["lineno"] == code.co_firstlineno
		assert rows[-1]["lineno"] >= last
		if not lines[code.co_firstlineno - 1].lstrip().startswith("@"):
			assert rows == truth(code)
		blank_inside += any(not row["content"].strip() for row in rows)
	assert len(codes) > 10
	return blank_inside


@pytest.mark.parametrize("path", CORPUS_PATHS)
def test_every_function_of_real_modules_is_captured_whole(monkeypatch, path):
	_check_corpus_module(monkeypatch, path)


def test_corpus_has_many_functions_with_blank_lines(monkeypatch):
	assert sum(_check_corpus_module(monkeypatch, path) for path in CORPUS_PATHS) >= 50


def test_snapshot_keeps_lines_timed_after_a_blank_line(tmp_path, monkeypatch):
	pytest.importorskip("line_profiler")
	_, fn = load(tmp_path, monkeypatch, BLANKS)
	snapshot = json.loads(prepare(monkeypatch, fn).source_json)["optimus.mod.f"]
	monkeypatch.setattr(capture, "_get_or_resolve_picks", lambda run: [fn])
	profiler = capture.make_profiler("blank-run")
	profiler.enable_by_count()
	try:
		fn(1)
	finally:
		profiler.disable_by_count()
	timed = {row["lineno"] for row in capture.serialize_stats(profiler)}
	assert timed and timed <= {row["lineno"] for row in snapshot}
	assert max(timed) == snapshot[-1]["lineno"]


CHANGED = {
	"truncated_open_paren": "def f(a):\n\tx = (\n",
	"cut_inside_docstring": 'def f(a):\n\t"""\n',
	"nul_byte": "def f(a):\n\tx = a\x00\n",
	"past_end_of_file": "x = 1\n",
	"one_line_short": "def f(a):\n\tx = a + 1\n\n\ty = x * 2\n\ty += 0\nz = 3\n",
	"nested_same_name_def": "def other():\n\tdef f(a):\n\t\tx = 1\n\n\t\ty = 2\n\t\treturn y\n\treturn f\n",
	"def_inside_docstring": 'def other():\n\t"""\n\tdef f(a):\n\t"""\n\ta = 1\n\tb = 2\n\treturn a\n',
	"shortened_body": "def f(a):\n\tx = a + 1\nz = 3\n",
	"another_function_at_that_line": "def other():\n\ta = 1\n\tb = 2\n\tc = 3\n\td = 4\n\te = 5\n\treturn a\n",
}


@pytest.mark.parametrize("new_text", CHANGED.values(), ids=CHANGED.keys())
def test_unreadable_user_pick_refuses_the_run_naming_the_function(tmp_path, monkeypatch, new_text):
	path, fn = load(tmp_path, monkeypatch, BLANKS)
	path.write_text(new_text)
	with pytest.raises(capture.CaptureError) as caught:
		prepare(monkeypatch, fn)
	assert "optimus.mod.f" in str(caught.value)
	assert not isinstance(caught.value, ValueError)


def test_source_unavailable_carries_its_reason(tmp_path, monkeypatch):
	path, fn = load(tmp_path, monkeypatch, BLANKS)
	path.write_text("x = 1\n")
	with pytest.raises(capture.SourceUnavailable) as caught:
		capture._capture_source_lines(fn)
	assert isinstance(caught.value, capture.CaptureError)
	assert caught.value.reason


def test_source_outside_the_boundary_names_the_function(tmp_path, monkeypatch):
	_, fn = load(tmp_path, monkeypatch, BLANKS)
	monkeypatch.setattr(source, "_resolve_source_path", lambda filename, must_exist=True: None)
	with pytest.raises(capture.CaptureError, match="optimus.mod.f"):
		prepare(monkeypatch, fn)


def test_auto_expanded_pick_with_unavailable_source_is_skipped(tmp_path, monkeypatch):
	from optimus.line_profile import picker

	path_a, good = load(tmp_path, monkeypatch, BLANKS)
	path_b, bad = load(tmp_path, monkeypatch, BLANKS)
	path_b.write_text("x = 1\n")
	paths = {good.__code__.co_filename: path_a, bad.__code__.co_filename: path_b}
	by_dotted = {"optimus.mod.good": good, "optimus.mod.bad": bad}
	monkeypatch.setattr(capture, "_LP_AVAILABLE", True)
	monkeypatch.setattr(source, "_installed_apps", lambda: {"optimus"})
	monkeypatch.setattr(
		source, "_resolve_source_path", lambda filename, must_exist=True: str(paths[filename])
	)
	monkeypatch.setattr(
		picker,
		"resolve_freeform",
		lambda dotted: {
			"dotted_path": dotted,
			"eligible": True,
			"qualname": "f",
			"file": "x.py",
			"lineno": 1,
		},
	)
	monkeypatch.setattr(capture, "_resolve_attr", by_dotted.get)
	prepared = capture.prepare_line_profile_picks(
		[
			{"dotted_path": "optimus.mod.good", "source": "curated"},
			{"dotted_path": "optimus.mod.bad", "source": "auto_expand"},
		],
		server_expanded=frozenset({"optimus.mod.bad"}),
	)
	assert list(json.loads(prepared.source_json)) == ["optimus.mod.good"]
	resolved = {r["dotted_path"]: r for r in prepared.resolved}
	assert resolved["optimus.mod.bad"]["eligible"] is False
	assert resolved["optimus.mod.bad"]["ineligible_reason"]


def test_overlong_source_line_is_a_named_capture_error(tmp_path, monkeypatch):
	text = 'def f():\n\ts = "%s"\n\treturn s\n' % ("x" * 20_001)
	_, fn = load(tmp_path, monkeypatch, text)
	with pytest.raises(capture.CaptureError, match="optimus.mod.f") as caught:
		prepare(monkeypatch, fn)
	assert not isinstance(caught.value, ValueError)


def test_source_line_budget_overflow_is_a_capture_error_not_a_server_error(tmp_path, monkeypatch):
	_, fn = load(tmp_path, monkeypatch, BLANKS)
	monkeypatch.setattr(capture, "MAX_SOURCE_LINES", 2)
	with pytest.raises(capture.CaptureError) as caught:
		prepare(monkeypatch, fn)
	assert not isinstance(caught.value, ValueError)


def test_open_paren_reason_says_it_could_not_be_parsed(tmp_path, monkeypatch):
	path, fn = load(tmp_path, monkeypatch, BLANKS)
	path.write_text(CHANGED["truncated_open_paren"])
	with pytest.raises(capture.SourceUnavailable) as caught:
		capture._capture_source_lines(fn)
	assert caught.value.reason == "its source could not be parsed"


def test_one_line_short_block_is_refused_as_not_matching(tmp_path, monkeypatch):
	path, fn = load(tmp_path, monkeypatch, BLANKS)
	path.write_text(CHANGED["one_line_short"])
	with pytest.raises(capture.SourceUnavailable) as caught:
		capture._capture_source_lines(fn)
	assert "no longer matches" in caught.value.reason


def test_class_body_code_is_not_a_def_statement():
	text = "class C:\n\tx = 1\n\n\ty = 2\n"
	code = next(c for c in compile(text, "c.py", "exec").co_consts if inspect.iscode(c))
	with pytest.raises(capture.SourceUnavailable) as caught:
		capture._function_block(text.split("\n")[:-1], code)
	assert caught.value.reason == "it is not a def statement"


def test_only_auto_expanded_unreadable_picks_leave_nothing_to_run(tmp_path, monkeypatch):
	path, fn = load(tmp_path, monkeypatch, BLANKS)
	path.write_text("x = 1\n")
	from optimus.line_profile import picker

	monkeypatch.setattr(capture, "_LP_AVAILABLE", True)
	monkeypatch.setattr(source, "_installed_apps", lambda: {"optimus"})
	monkeypatch.setattr(
		picker,
		"resolve_freeform",
		lambda dotted: {
			"dotted_path": dotted,
			"eligible": True,
			"qualname": "f",
			"file": "x.py",
			"lineno": 1,
		},
	)
	monkeypatch.setattr(capture, "_resolve_attr", lambda dotted: fn)
	with pytest.raises(capture.CaptureError, match="No selected function has eligible source"):
		capture.prepare_line_profile_picks(
			[{"dotted_path": "optimus.mod.f", "source": "auto_expand"}],
			server_expanded=frozenset({"optimus.mod.f"}),
		)


def test_multiline_lambda_in_a_call_says_its_source_spans_several_lines():
	text = "f = dict(\n\tk=lambda a: a.get(1)\n\t\tor 2,\n)\n"
	module_code = compile(text, "c.py", "exec")
	code = next(c for c in module_code.co_consts if inspect.iscode(c))
	with pytest.raises(capture.SourceUnavailable) as caught:
		capture._function_block(text.split("\n")[:-1], code)
	assert caught.value.reason == "its source spans several lines"


def test_refusal_reason_is_not_spliced_into_a_translated_sentence(tmp_path, monkeypatch):
	path, fn = load(tmp_path, monkeypatch, BLANKS)
	path.write_text("x = 1\n")
	msgids = []

	def translate(msg):
		msgids.append(msg)
		return msg

	monkeypatch.setattr(capture, "_", translate)
	with pytest.raises(capture.CaptureError) as caught:
		prepare(monkeypatch, fn)
	reason = capture._pick_source(fn)[1]
	assert reason in msgids and reason in str(caught.value)
	sentences = [m for m in msgids if "{1}" in m]
	assert sentences
	assert all(not m.split("{1}")[0].endswith(("because ", "is ")) for m in sentences)
