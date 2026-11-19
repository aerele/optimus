# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The repo-read lint gate: a test file must not read a file of the repository.

``repo_reads_scan`` finds the reads statically (see its docstring for the rules and the blind
spots), ``repo_reads_baseline`` holds the allowed per-file counts, and the last test here
checks the real tree against them. The rest of this file proves the scanner and the gate on
inline source strings and on a fake tree under ``tmp_path``.

This file is scanned like any other: its samples are strings, which the scanner never reads as code.
"""

import os
import re
import textwrap
from pathlib import Path

import pytest

from optimus.tests import repo_reads_baseline as baseline
from optimus.tests import repo_reads_scan as scanner
from optimus.tests.repo_reads_scan import check_tree, scan_source, scan_tree

TESTS_DIR = Path(__file__).resolve().parent
# ``_unused = __file__`` is a source hint, so no sample takes the hint-free early exit of the scanner:
# a negative test must be able to fail. ``TestHintExit`` covers the early exit itself.
PRELUDE = (
	"import inspect\nimport io\nimport os\nimport subprocess\nfrom pathlib import Path\n_unused = __file__\n"
)


def scan(src, **modules):
	"""Scan an inline module; ``modules`` are other optimus.tests modules it may import."""
	others = {f"optimus.tests.{name}": textwrap.dedent(text) for name, text in modules.items()}
	return scan_source(PRELUDE + textwrap.dedent(src), others=others)


def count(src, **modules):
	return len(scan(src, **modules).sites)


def lines(src, **modules):
	"""The 1-based lines of the sample (the prelude excluded) that hold a counted site."""
	prelude = PRELUDE.count("\n")
	return [s.line - prelude for s in scan(src, **modules).sites]


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


class TestSources:
	def test_the_name_dunder_file_is_a_source(self):
		assert count("Path(__file__).read_text()") == 1

	def test_dunder_file_on_any_expression_is_a_source(self):
		assert count("import x\nopen(x.__file__)") == 1
		assert count("Path(self.mod.__file__).read_bytes()") == 1

	def test_a_plain_path_is_not_a_source(self):
		assert count("Path('docs/x.md').read_text()\nopen('x.txt')") == 0

	def test_a_source_helper_returns_a_repo_path_and_is_not_a_sink(self):
		src = """
			def load(name):
				spec = spec_from_file_location(name, os.path.join(os.path.dirname(__file__), name))
				return module_from_spec(spec)

			load("a")
			load("b")
			load("c")
		"""
		assert count(src) == 0

	def test_a_call_of_a_source_helper_is_a_source(self):
		src = """
			def here():
				return Path(__file__).parent

			here().joinpath("a.json").read_text()
		"""
		assert count(src) == 1

	def test_an_imported_tainted_constant_is_a_source(self):
		support = "ROOT = __file__"
		assert count("from optimus.tests.support import ROOT\nopen(ROOT)", support=support) == 1

	def test_an_imported_untainted_constant_is_not(self):
		assert count("from optimus.tests.support import NAME\nopen(NAME)", support="NAME = 'x'") == 0

	@pytest.mark.parametrize(
		"header,use",
		[
			("from optimus.tests.support import ROOT as R", "open(R)"),
			("from optimus.tests import support as b", "open(b.ROOT)"),
			("import optimus.tests.support as m", "open(m.ROOT)"),
			("import optimus.tests.support", "open(optimus.tests.support.ROOT)"),
			("from . import support", "open(support.ROOT)"),
			("from .support import ROOT", "open(ROOT)"),
			("from optimus.tests import support", "open(support.ROOT)"),
		],
	)
	def test_every_import_form_of_a_tainted_constant_propagates(self, header, use):
		assert count(f"{header}\n{use}", support="ROOT = __file__") == 1

	def test_an_import_inside_a_function_propagates(self):
		src = """
			def f():
				from optimus.tests.support import ROOT
				return open(ROOT)
		"""
		assert count(src, support="ROOT = __file__") == 1

	def test_a_constant_that_is_itself_imported_propagates_one_hop_further(self):
		inner = "ROOT = __file__"
		outer = "from optimus.tests.inner import ROOT\nBASE = ROOT"
		assert count("from optimus.tests.outer import BASE\nopen(BASE)", inner=inner, outer=outer) == 1

	def test_a_two_module_import_cycle_gives_no_traceback(self):
		a = "from optimus.tests.b import Y\nX = __file__"
		b = "from optimus.tests.a import X\nY = X"
		assert count("from optimus.tests.a import X\nopen(X)", a=a, b=b) == 1

	def test_a_missing_or_foreign_module_is_ignored(self):
		src = "from optimus.tests.nothing import X\nfrom elsewhere import Y\nopen(X)\nopen(Y)"
		assert count(src) == 0


# ---------------------------------------------------------------------------
# Propagation and scope
# ---------------------------------------------------------------------------


class TestPropagation:
	def test_an_assignment_chain(self):
		src = """
			a = Path(__file__)
			b = a.parent
			c = b / "x"
			c.read_text()
		"""
		assert count(src) == 1

	def test_an_annotated_assignment(self):
		assert count("p: Path = Path(__file__)\np.read_text()") == 1

	def test_an_augmented_assignment(self):
		assert count("p = ''\np += __file__\nopen(p)") == 1

	def test_a_walrus_binding(self):
		assert count("if (r := Path(__file__).parent):\n\tr.read_text()") == 1

	def test_a_for_loop_target(self):
		src = """
			for p in Path(__file__).parent.glob("*.md"):
				p.read_text()
		"""
		assert count(src) == 1

	def test_a_comprehension_target(self):
		assert count("[p.read_text() for p in Path(__file__).parent.iterdir()]") == 1

	def test_a_with_target(self):
		assert count("with closing(Path(__file__)) as p:\n\tp.read_text()") == 1

	def test_tuple_unpacking(self):
		assert count("a, (b, c) = Path(__file__), 1\nb.read_text()") == 1

	def test_a_default_parameter(self):
		assert count("def f(p=Path(__file__)):\n\treturn p.read_text()") == 1

	def test_a_keyword_only_default_parameter(self):
		assert count("def f(*, p=Path(__file__)):\n\treturn p.read_text()") == 1

	def test_a_closure_sees_the_enclosing_taint(self):
		src = """
			def outer():
				root = Path(__file__)
				def inner():
					return root.read_text()
				return inner
		"""
		assert count(src) == 1

	def test_a_module_name_is_seen_by_a_function(self):
		assert count("ROOT = Path(__file__)\ndef f():\n\treturn ROOT.read_text()") == 1

	def test_the_same_name_in_two_functions_counts_only_the_repo_one(self):
		src = """
			def repo():
				p = Path(__file__)
				return p.read_text()

			def scratch(tmp_path):
				p = tmp_path / "x"
				return p.read_text()
		"""
		assert lines(src) == [4]

	def test_a_local_binding_hides_a_module_name(self):
		src = """
			ROOT = Path(__file__)

			def f(tmp_path):
				ROOT = tmp_path
				return ROOT.read_text()
		"""
		assert count(src) == 0

	def test_a_parameter_hides_a_module_name(self):
		assert count("ROOT = Path(__file__)\ndef f(ROOT):\n\treturn ROOT.read_text()") == 0

	def test_a_class_attribute_is_not_visible_in_a_method_as_a_bare_name(self):
		src = """
			class T:
				ROOT = Path(__file__)

				def test_x(self):
					return ROOT.read_text()
		"""
		assert count(src) == 0

	def test_a_class_body_read_is_counted(self):
		assert count("class T:\n\tROOT = Path(__file__)\n\tTEXT = ROOT.read_text()") == 1

	def test_a_lambda_body_is_scanned(self):
		assert count("ROOT = Path(__file__)\nread = lambda: ROOT.read_text()") == 1

	def test_a_lambda_parameter_hides_a_module_name(self):
		assert count("ROOT = Path(__file__)\nread = lambda ROOT: ROOT.read_text()") == 0


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------


class TestSinks:
	def test_read_text_and_read_bytes(self):
		assert count("Path(__file__).read_text()\nPath(__file__).read_bytes()") == 2

	def test_a_path_open_method(self):
		assert count("Path(__file__).open().read()") == 1

	def test_the_builtin_open(self):
		assert count("open(__file__)\nopen(file=__file__)") == 2

	def test_io_open(self):
		assert count("io.open(__file__)") == 1

	def test_inspect_getsource_counts_with_any_argument(self):
		src = "inspect.getsource(x)\ninspect.getsourcelines(x)\ninspect.getsourcefile(x)"
		assert count(src) == 3

	def test_a_from_import_of_getsource(self):
		assert count("from inspect import getsource as gs\ngs(x)") == 1

	@pytest.mark.parametrize(
		"call",
		[
			'Path(__file__).open("w")',
			'Path(__file__).open(mode="a")',
			'open(__file__, "w")',
			'open(__file__, "wb")',
			'open(__file__, mode="a")',
			'io.open(__file__, "x")',
		],
	)
	def test_a_write_mode_open_is_not_a_read(self, call):
		assert count(call) == 0

	@pytest.mark.parametrize(
		"call",
		[
			'Path(__file__).open("r")',
			'Path(__file__).open("r+")',
			"Path(__file__).open(mode)",
			"open(__file__, mode)",
			"open(__file__, mode=mode)",
			'open(__file__, "rb")',
		],
	)
	def test_a_read_or_unknown_mode_open_is_counted(self, call):
		assert count(call) == 1

	def test_a_path_that_never_touches_the_repo_is_not_counted(self):
		src = """
			def test_x(tmp_path):
				(tmp_path / "a").read_text()
				open(tmp_path / "b")
				tmp_path.joinpath("c").read_bytes()
				open("literal.txt")
		"""
		assert count(src) == 0

	def test_other_modules_open_and_subprocess_and_importlib_are_not_sinks(self):
		src = """
			subprocess.run([__file__])
			importlib.util.spec_from_file_location("m", __file__)
			shutil.copy(__file__, "x")
			os.stat(__file__)
		"""
		assert count(src) == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
	def test_a_helper_with_a_module_root_and_a_parameter_is_one_site_however_often_called(self):
		src = """
			ROOT = Path(__file__).parent

			def _src(name):
				return (ROOT / name).read_text()

			_src("a")
			_src("b")
			_src("c")
		"""
		assert lines(src) == [5]

	def test_a_sink_helper_counts_once_per_call_with_a_repo_path(self):
		src = """
			ROOT = Path(__file__).parent

			def _read(p):
				return p.read_text()

			_read(ROOT)
			_read(ROOT / "a")
			_read(tmp_path)
		"""
		assert lines(src) == [7, 8]

	def test_the_sink_inside_a_sink_helper_is_not_counted(self):
		assert count("def _read(p):\n\treturn p.read_text()") == 0

	def test_a_sink_helper_maps_keyword_arguments_by_name(self):
		src = """
			def _read(a, b):
				return Path(a).read_text() + Path(b).read_text()

			_read("x", b=__file__)
			_read(b="x", a="y")
		"""
		assert lines(src) == [5]

	def test_a_sink_helper_ignores_an_argument_in_an_unused_position(self):
		src = """
			def _read(base, name):
				return open(name)

			_read(__file__, "x")
			_read("x", __file__)
		"""
		assert lines(src) == [6]

	def test_a_sink_helper_with_a_star_args_call(self):
		src = """
			def _read(p):
				return p.read_text()

			_read(*[__file__])
			_read(*["x"])
			_read(**{"p": __file__})
		"""
		assert lines(src) == [5, 7]

	def test_a_sink_helper_through_a_local_derived_from_a_parameter(self):
		src = """
			def _read(base, name):
				full = os.path.join(base, name)
				return open(full)

			_read(os.path.dirname(__file__), "a")
			_read("x", "y")
		"""
		assert lines(src) == [6]

	def test_a_sink_helper_through_a_variadic_parameter(self):
		src = """
			def _read(*parts):
				return open(os.path.join(*parts))

			_read("a", __file__)
			_read("a", "b")
		"""
		assert lines(src) == [5]

	def test_a_method_with_the_name_of_a_helper_does_not_hide_it(self):
		src = """
			def _read(p):
				return p.read_text()

			class T:
				def _read(self):
					return 1

			_read(__file__)
		"""
		assert lines(src) == [9]

	def test_a_method_is_not_a_helper(self):
		src = """
			class T:
				def _read(self, p):
					return p.read_text()

				def test_x(self):
					self._read(__file__)
		"""
		assert count(src) == 0


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------


REASON = "# test-lint: repo-read-ok doctype json shape"
SHORT = "marker reason is too short: write a reason of 3+ words"


class TestMarkers:
	def test_a_marker_on_the_line_covers_the_site(self):
		result = scan(f"Path(__file__).read_text()  {REASON}")
		assert (len(result.sites), len(result.unmarked), result.markers, result.problems) == (1, 0, 1, [])

	def test_a_marker_on_the_line_above_covers_the_site(self):
		result = scan(f"{REASON}\nPath(__file__).read_text()")
		assert (len(result.unmarked), result.markers, result.problems) == (0, 1, [])

	def test_a_marker_two_lines_above_does_not_cover(self):
		result = scan(f"{REASON}\nx = 1\nPath(__file__).read_text()")
		assert len(result.unmarked) == 1
		assert [p.text for p in result.problems] == [
			"stale marker: no repo read on the next line, delete this comment"
		]

	def test_a_marker_above_a_multi_line_call_covers_it(self):
		src = f"{REASON}\ntext = Path(__file__).read_text(\n\tencoding='utf-8',\n)"
		assert len(scan(src).unmarked) == 0

	def test_one_marker_covers_two_sites(self):
		src = f"{REASON}\na = Path(__file__).read_text(); b = Path(__file__).read_bytes()"
		result = scan(src)
		assert (len(result.sites), len(result.unmarked), result.markers) == (2, 0, 1)

	def test_a_trailing_marker_covers_its_own_line_only(self):
		src = f"Path(__file__).read_text()  {REASON}\nPath(__file__).read_text()\nPath(__file__).read_text()"
		result = scan(src)
		assert [s.line - PRELUDE.count("\n") for s in result.unmarked] == [2, 3]

	def test_a_standalone_marker_covers_the_next_line_only(self):
		src = f"{REASON}\nPath(__file__).read_text()\nPath(__file__).read_text()"
		result = scan(src)
		assert [s.line - PRELUDE.count("\n") for s in result.unmarked] == [3]

	def test_a_trailing_marker_does_not_cover_the_next_line_and_is_stale_without_a_site_of_its_own(self):
		result = scan(f"x = 1  {REASON}\nPath(__file__).read_text()")
		assert len(result.unmarked) == 1
		assert [p.text for p in result.problems] == [
			"stale marker: no repo read on this line, delete this comment"
		]

	def test_marker_text_inside_a_string_is_ignored(self):
		src = 'NOTE = "# test-lint: repo-read-ok some long reason here"\nPath(__file__).read_text()'
		result = scan(src)
		assert (len(result.unmarked), result.markers, result.problems) == (1, 0, [])

	def test_a_short_reason_is_a_problem_and_covers_nothing(self):
		result = scan("# test-lint: repo-read-ok json only\nPath(__file__).read_text()")
		assert len(result.unmarked) == 1 and result.markers == 0
		assert [p.text for p in result.problems] == [SHORT]

	def test_a_missing_reason_is_a_problem(self):
		result = scan("# test-lint: repo-read-ok\nPath(__file__).read_text()")
		assert [p.text for p in result.problems] == [SHORT]

	def test_a_stale_marker_is_a_problem(self):
		result = scan(f"{REASON}\nx = 1")
		assert result.markers == 0
		assert [p.text for p in result.problems] == [
			"stale marker: no repo read on the next line, delete this comment"
		]

	def test_a_comment_that_is_not_the_marker_is_ignored(self):
		result = scan("# test-lint: repo-read-okay for three words\nPath(__file__).read_text()")
		assert (len(result.unmarked), result.markers, result.problems) == (1, 0, [])

	def test_a_marker_covers_a_sink_helper_call(self):
		src = f"def _r(p):\n\treturn p.read_text()\n\n_r(__file__)  {REASON}"
		assert len(scan(src).unmarked) == 0


# ---------------------------------------------------------------------------
# The gate on a fake tree
# ---------------------------------------------------------------------------

READ = "from pathlib import Path\n\n\ndef test_x():\n\tPath(__file__).read_text()\n"
TWO_READS = READ + "\n\ndef test_y():\n\tPath(__file__).read_bytes()\n"
CLEAN = "def test_x():\n\tassert True\n"


def tree(tmp_path, files):
	for name, text in files.items():
		path = tmp_path / name
		path.parent.mkdir(parents=True, exist_ok=True)
		if isinstance(text, bytes):
			path.write_bytes(text)
		else:
			path.write_text(text, encoding="utf-8")
	return tmp_path


def gate(tmp_path, files, base=None, marked=0, exempt=None):
	return check_tree(tree(tmp_path, files), base or {}, marked, exempt or {}, prefix="t/")


def pasted(message):
	"""The lines a person could paste into repo_reads_baseline.py."""
	return [
		m.group(1) for m in re.finditer(r'^[ \t]*("[^"]+": \d+,|MARKED_TOTAL = \d+)[ \t]*$', message, re.M)
	]


class TestGate:
	def test_a_tree_that_matches_its_baseline_passes(self, tmp_path):
		assert gate(tmp_path, {"test_a.py": READ, "test_b.py": CLEAN}, {"test_a.py": 1}) is None

	def test_a_new_file_with_a_read_fails_with_its_sites_and_the_remedies(self, tmp_path):
		message = gate(tmp_path, {"test_new.py": READ})
		assert message.startswith("repo-read lint: 1 problem(s)")
		assert "t/test_new.py:5: repo read (read_text)" in message
		assert "test_docs_truth.py" in message
		assert "# test-lint: repo-read-ok" in message and "MARKED_TOTAL" in message

	def test_a_file_that_adds_a_read_lists_all_its_sites(self, tmp_path):
		message = gate(tmp_path, {"test_a.py": TWO_READS}, {"test_a.py": 1})
		assert message.startswith("repo-read lint: 1 problem(s)")
		assert "t/test_a.py: the file has 2 unmarked repo read(s), the baseline allows 1" in message
		assert "t/test_a.py:5:" in message and "t/test_a.py:9:" in message

	def test_a_file_that_removes_a_read_must_lower_the_baseline(self, tmp_path):
		message = gate(tmp_path, {"test_a.py": READ}, {"test_a.py": 2})
		assert "repo-read lint: 1 problem(s)" in message
		assert pasted(message) == ['"test_a.py": 1,']

	def test_a_file_with_no_read_left_must_drop_its_key(self, tmp_path):
		message = gate(tmp_path, {"test_a.py": CLEAN}, {"test_a.py": 1})
		assert pasted(message) == []
		assert 'delete: "test_a.py": 1,' in message
		assert "Change optimus/tests/repo_reads_baseline.py:" in message

	def test_a_baseline_key_for_a_missing_file_fails(self, tmp_path):
		message = gate(tmp_path, {"test_a.py": CLEAN}, {"test_gone.py": 3})
		assert "t/test_gone.py" in message and 'delete: "test_gone.py": 3,' in message
		assert pasted(message) == []

	def test_an_exempt_file_is_not_scanned_and_its_marker_is_ignored(self, tmp_path):
		files = {"test_docs.py": READ.replace("\tPath", f"\t{REASON}\n\tPath")}
		assert gate(tmp_path, files, exempt={"test_docs.py": "docs pins live here"}) is None

	def test_a_baseline_key_naming_an_exempt_file_fails(self, tmp_path):
		message = gate(tmp_path, {"test_docs.py": READ}, {"test_docs.py": 1}, exempt={"test_docs.py": "x"})
		assert "t/test_docs.py" in message and "delete" in message

	def test_a_valid_marker_passes_only_when_marked_total_matches(self, tmp_path):
		files = {"test_a.py": READ.replace("\tPath", f"\t{REASON}\n\tPath")}
		assert gate(tmp_path, files, marked=1) is None

	def test_a_new_marker_asks_to_raise_marked_total_without_printing_the_number(self, tmp_path):
		files = {"test_a.py": READ.replace("\tPath", f"\t{REASON}\n\tPath")}
		message = gate(tmp_path, files, marked=0)
		assert "repo-read lint: 1 problem(s)" in message
		assert "new waiver" in message and "raise MARKED_TOTAL by the number of markers you added" in message
		assert "t/test_a.py:5" in message  # the valid marker is listed
		assert pasted(message) == []
		assert "To fix a new repo read" not in message  # no read is new or increased

	def test_a_removed_marker_asks_to_lower_marked_total_with_a_paste_line(self, tmp_path):
		message = gate(tmp_path, {"test_a.py": CLEAN}, marked=2)
		assert pasted(message) == ["MARKED_TOTAL = 0"]
		assert "valid marker(s)" in message and "lower it" in message

	def test_a_bad_marker_is_reported_with_its_line(self, tmp_path):
		files = {"test_a.py": READ.replace("\tPath", "\t# test-lint: repo-read-ok no\n\tPath")}
		message = gate(tmp_path, files, {"test_a.py": 1})
		assert f"t/test_a.py:5: {SHORT}" in message

	def test_a_stale_marker_is_reported_with_its_line(self, tmp_path):
		message = gate(tmp_path, {"test_a.py": f"x = 1\n{REASON}\ny = 2\n"})
		assert "t/test_a.py:2: stale marker" in message

	def test_a_file_named_like_pytest_test_suffix_is_scanned(self, tmp_path):
		assert "t/a_test.py:5" in gate(tmp_path, {"a_test.py": READ})

	def test_a_file_in_a_subfolder_has_a_posix_key(self, tmp_path):
		assert gate(tmp_path, {"sub/deep/test_a.py": READ}, {"sub/deep/test_a.py": 1}) is None

	def test_conftest_and_support_modules_are_scanned(self, tmp_path):
		files = {"conftest.py": READ, "ai_eval_support.py": READ, "__init__.py": READ}
		message = gate(tmp_path, files)
		assert "t/conftest.py:5" in message and "t/ai_eval_support.py:5" in message
		assert "t/__init__.py:5" in message

	def test_fixtures_and_data_folders_are_skipped(self, tmp_path):
		files = {"fixtures/x.py": READ, "data/y.py": READ, "sub/fixtures/z.py": READ}
		assert gate(tmp_path, files) is None

	def test_a_syntax_error_file_is_a_problem_line_not_a_crash(self, tmp_path):
		message = gate(tmp_path, {"test_a.py": "def broken(:\n"})
		assert "t/test_a.py:1: cannot parse" in message
		assert "To fix a new repo read" not in message

	def test_a_non_utf8_file_without_a_coding_line_is_a_problem_line(self, tmp_path):
		message = gate(tmp_path, {"test_a.py": "x = '\u00e9'\n".encode("latin-1")})
		assert "t/test_a.py" in message and "cannot parse" in message

	def test_a_null_byte_file_is_a_problem_line(self, tmp_path):
		message = gate(tmp_path, {"test_a.py": b"x = 1\x00\n"})
		assert "t/test_a.py" in message and "cannot parse" in message

	def test_a_latin1_file_with_a_coding_line_is_read_as_declared(self, tmp_path):
		body = "# -*- coding: latin-1 -*-\nx = '\u00e9'\n".encode("latin-1") + READ.encode("latin-1")
		assert gate(tmp_path, {"test_a.py": body}, {"test_a.py": 1}) is None

	def test_an_import_cycle_between_two_support_modules_does_not_crash(self, tmp_path):
		files = {
			"a.py": "from optimus.tests.b import Y\nX = __file__\n",
			"b.py": "from optimus.tests.a import X\nY = X\n",
			"test_c.py": "from optimus.tests.a import X\nopen(X)\n",
		}
		assert "t/test_c.py:2" in gate(tmp_path, files)

	def test_a_constant_from_an_exempt_module_still_propagates(self, tmp_path):
		files = {
			"test_docs.py": "ROOT = __file__\n",
			"test_c.py": "from optimus.tests.test_docs import ROOT\nopen(ROOT)\n",
		}
		message = gate(tmp_path, files, exempt={"test_docs.py": "x"})
		assert "t/test_c.py:2" in message

	def test_relative_imports_in_a_subfolder_resolve_against_its_package(self, tmp_path):
		files = {
			"sub/support.py": "ROOT = __file__\n",
			"sub/test_a.py": "from .support import ROOT\nopen(ROOT)\n",
			"sub/test_b.py": "from . import support\nopen(support.ROOT)\n",
			"sub/test_c.py": "from ..sub import support\nopen(support.ROOT)\n",
		}
		message = gate(tmp_path, files)
		assert all(f"t/sub/test_{x}.py:2" in message for x in "abc")

	def test_the_problem_lines_never_raise_a_count_or_add_a_key(self, tmp_path):
		files = {"test_up.py": TWO_READS, "test_down.py": READ, "test_new.py": READ, "test_gone2.py": CLEAN}
		base = {"test_up.py": 1, "test_down.py": 3, "test_gone.py": 1, "test_gone2.py": 2}
		message = gate(tmp_path, files, base, marked=5)
		lines_ = pasted(message)
		assert '"test_down.py": 1,' in lines_ and "MARKED_TOTAL = 0" in lines_  # the decreases are printed
		for line in lines_:
			if line.strip().startswith("MARKED_TOTAL"):
				assert int(line.split("=")[1]) < 5
				continue
			key, value = re.match(r'\s*"([^"]+)": (\d+),', line).groups()
			assert key in base and int(value) < base[key]
		assert '"test_up.py"' not in "".join(lines_) and '"test_new.py"' not in "".join(lines_)

	def test_problems_are_sorted_by_path(self, tmp_path):
		message = gate(tmp_path, {"test_b.py": READ, "test_a.py": READ})
		assert message.index("t/test_a.py") < message.index("t/test_b.py")

	def test_scan_tree_returns_one_entry_per_scanned_file(self, tmp_path):
		tree(tmp_path, {"test_a.py": READ, "sub/test_b.py": CLEAN, "fixtures/c.py": READ})
		scans = scan_tree(tmp_path, exempt={})
		assert sorted(scans) == ["sub/test_b.py", "test_a.py"]


# ---------------------------------------------------------------------------
# Helpers are resolved by the scope that defines them
# ---------------------------------------------------------------------------


class TestScopedHelpers:
	def test_a_nested_sink_helper_is_not_confused_with_a_same_named_helper_of_another_test(self):
		src = """
			def test_a():
				def helper(p):
					return p.read_text()
				helper(__file__)

			def test_b():
				def helper(p):
					return 1
				helper(__file__)
		"""
		assert lines(src) == [5]

	def test_the_same_in_the_other_order(self):
		src = """
			def test_a():
				def helper(p):
					return 1
				helper(__file__)

			def test_b():
				def helper(p):
					return p.read_text()
				helper(__file__)
		"""
		assert lines(src) == [10]

	def test_a_later_nested_def_does_not_hide_a_top_level_sink_helper_elsewhere(self):
		src = """
			def _read(p):
				return p.read_text()

			def test_x():
				def _read(p):
					return 1
				_read(__file__)

			_read(__file__)
		"""
		assert lines(src) == [10]

	def test_a_parameter_named_like_a_sink_helper_is_not_the_helper(self):
		src = """
			def _read(p):
				return p.read_text()

			def test_x(_read):
				_read(__file__)
		"""
		assert count(src) == 0

	def test_a_local_variable_named_like_a_sink_helper_is_not_the_helper(self):
		src = """
			def _read(p):
				return p.read_text()

			def test_x():
				_read = print
				_read(__file__)
		"""
		assert count(src) == 0

	def test_with_two_top_level_defs_of_one_name_the_last_one_wins(self):
		sink_last = "def _read(p):\n\treturn 1\ndef _read(p):\n\treturn p.read_text()\n_read(__file__)"
		sink_first = "def _read(p):\n\treturn p.read_text()\ndef _read(p):\n\treturn 1\n_read(__file__)"
		assert count(sink_last) == 1
		assert count(sink_first) == 0

	def test_a_nested_source_helper_is_resolved_by_its_own_scope(self):
		src = """
			def test_a():
				def here():
					return Path(__file__)
				here().read_text()

			def test_b():
				def here():
					return Path("x")
				here().read_text()
		"""
		assert lines(src) == [5]


# ---------------------------------------------------------------------------
# Cheap bypasses of the sinks
# ---------------------------------------------------------------------------


class TestSinkBypasses:
	@pytest.mark.parametrize("attr", ["read_text", "read_bytes", "open"])
	def test_a_getattr_built_read_call_is_counted(self, attr):
		assert count(f'getattr(Path(__file__), "{attr}")()') == 1

	def test_a_getattr_built_call_on_a_plain_path_or_another_attribute_is_not(self):
		assert count('getattr(Path("x"), "read_text")()') == 0
		assert count('getattr(Path(__file__), "name")') == 0

	def test_a_getattr_built_open_in_write_mode_is_not_a_read(self):
		assert count('getattr(Path(__file__), "open")("w")') == 0

	def test_a_stored_bound_method_is_counted_once_at_the_reference(self):
		src = """
			p = Path(__file__)
			r = p.read_text
			r()
			r()
		"""
		assert lines(src) == [3]

	def test_a_stored_getattr_built_method_is_counted_once(self):
		src = """
			r = getattr(Path(__file__), "read_bytes")
			r()
		"""
		assert lines(src) == [2]

	def test_a_stored_open_method_is_counted(self):
		assert count("r = Path(__file__).open\nr()") == 1

	def test_a_bound_method_of_a_plain_path_is_not_counted(self):
		assert count("r = Path('x').read_text\nr()") == 0

	@pytest.mark.parametrize(
		"call",
		[
			"Path.read_text(Path(__file__))",
			"PurePath.read_bytes(Path(__file__))",
			"pathlib.Path.read_text(Path(__file__))",
			"Path.open(Path(__file__))",
		],
	)
	def test_an_unbound_path_method_with_a_repo_argument_is_counted(self, call):
		assert count(call) == 1

	def test_an_unbound_path_method_on_a_plain_path_or_in_write_mode_is_not(self):
		assert count("Path.read_text(Path('x'))") == 0
		assert count('Path.open(Path(__file__), "w")') == 0

	@pytest.mark.parametrize("module", ["codecs", "gzip", "bz2", "lzma", "tarfile"])
	def test_other_open_functions_are_counted_by_the_open_rules(self, module):
		assert count(f"{module}.open(__file__)") == 1
		assert count(f'{module}.open(__file__, "rb")') == 1
		assert count(f'{module}.open("x")') == 0

	@pytest.mark.parametrize("mode", ["w", "wb", "a", "x", "w:gz"])
	def test_other_open_functions_in_write_mode_are_not_reads(self, mode):
		assert count(f'gzip.open(__file__, "{mode}")') == 0
		assert count(f'tarfile.open(__file__, mode="{mode}")') == 0

	def test_an_aliased_open_module_is_counted(self):
		assert count("import gzip as gz\ngz.open(__file__)") == 1

	def test_a_star_import_binds_every_tainted_constant(self):
		src = "from optimus.tests.support import *\nopen(ROOT)\nopen(OTHER)"
		assert lines(src, support="ROOT = __file__\nOTHER = 'x'") == [2]

	def test_a_star_import_is_hidden_by_a_local_binding(self):
		src = "from optimus.tests.support import *\ndef f(ROOT):\n\topen(ROOT)"
		assert count(src, support="ROOT = __file__") == 0

	def test_a_relative_star_import_binds_the_constants_of_a_sibling(self, tmp_path):
		files = {"support.py": "ROOT = __file__\n", "test_a.py": "from .support import *\nopen(ROOT)\n"}
		assert "t/test_a.py:2" in gate(tmp_path, files)


# ---------------------------------------------------------------------------
# Survivors of the first review: the less common syntax forms
# ---------------------------------------------------------------------------


class TestSyntaxForms:
	def test_a_name_bound_after_the_function_that_reads_it(self):
		assert count("def f():\n\treturn ROOT.read_text()\nROOT = Path(__file__)") == 1

	def test_a_binding_chain_written_in_reverse_order(self):
		assert count("p = q / 'x'\nq = Path(__file__)\np.read_text()") == 1

	def test_a_source_helper_defined_after_its_call(self):
		src = "here().joinpath('a').read_text()\ndef here():\n\treturn Path(__file__).parent"
		assert count(src) == 1

	def test_a_sink_helper_defined_after_its_call(self):
		assert count("_read(__file__)\ndef _read(p):\n\treturn p.read_text()") == 1

	def test_a_comprehension_condition(self):
		assert count("[x for x in names if Path(__file__).read_text()]") == 1
		assert count("[x for x in Path(__file__).iterdir() if x.read_text()]") == 1

	def test_async_for_with_and_def(self):
		src = """
			async def f():
				async for p in agen(Path(__file__)):
					p.read_text()
				async with closing(Path(__file__)) as q:
					q.read_text()
		"""
		assert lines(src) == [4, 6]

	def test_a_dict_comprehension_key_and_value(self):
		assert count("{Path(__file__).read_text(): 1 for _ in x}") == 1
		assert count("{k: p.read_text() for k, p in zip(a, Path(__file__).iterdir())}") == 1

	def test_a_set_comprehension_and_a_generator_expression(self):
		assert count("{p.read_text() for p in Path(__file__).iterdir()}") == 1
		assert count("list(p.read_text() for p in Path(__file__).iterdir())") == 1

	def test_a_walrus_in_a_comprehension_binds_in_the_enclosing_scope(self):
		src = "[y for x in z if (y := Path(__file__))]\ny.read_text()"
		assert count(src) == 1

	def test_positional_only_parameters(self):
		assert count("def f(p, /, q):\n\treturn p.read_text()") == 0
		assert lines("def _read(p, /):\n\treturn p.read_text()\n_read(__file__)") == [3]

	def test_a_decorator_and_a_default_expression_are_scanned_once_in_the_outer_scope(self):
		assert count("@deco(Path(__file__).read_text())\ndef f():\n\tpass") == 1
		assert count("def f(x=Path(__file__).read_text()):\n\tpass") == 1

	def test_an_except_as_name_hides_a_module_name(self):
		src = """
			ROOT = Path(__file__)

			def f():
				try:
					pass
				except Exception as ROOT:
					ROOT.read_text()
		"""
		assert count(src) == 0

	def test_a_parameter_hides_an_import_alias(self):
		src = "from optimus.tests import support as b\ndef f(b):\n\topen(b.ROOT)"
		assert count(src, support="ROOT = __file__") == 0

	def test_import_inspect_as_another_name(self):
		assert count("import inspect as i\ni.getsource(x)") == 1

	def test_a_sink_inside_a_comprehension_or_a_lambda_within_a_helper(self):
		comp = "def _read(p):\n\treturn [q.read_text() for q in [p]]\n_read(__file__)"
		lam = "def _read(p):\n\treturn (lambda: p.read_text())()\n_read(__file__)"
		assert lines(comp) == [3]
		assert lines(lam) == [3]

	def test_a_container_store_is_a_blind_spot_and_does_not_over_taint(self):
		src = "d = {}\nd['a'] = Path(__file__)\nopen(d)\nd.get('b').read_text()"
		assert count(src) == 0

	def test_a_marker_on_line_one_of_the_file(self):
		result = scan_source("# test-lint: repo-read-ok doctype json shape\nopen(__file__)\n")
		assert (len(result.unmarked), result.markers, result.problems) == (0, 1, [])

	def test_a_marker_covers_a_multi_line_sink_helper_call(self):
		src = f"def _r(p):\n\treturn p.read_text()\n\n{REASON}\n_r(\n\t__file__,\n)"
		assert len(scan(src).unmarked) == 0
		inside = f"def _r(p):\n\treturn p.read_text()\n\n_r(\n\t__file__,  {REASON}\n)"
		assert len(scan(inside).unmarked) == 0


# ---------------------------------------------------------------------------
# Marker placement details
# ---------------------------------------------------------------------------


class TestMarkerPlacement:
	def test_a_marker_on_any_line_of_a_multi_line_call_covers_it(self):
		src = f"text = Path(\n\t__file__,  {REASON}\n).read_text()"
		result = scan(src)
		assert (len(result.sites), len(result.unmarked), result.markers, result.problems) == (1, 0, 1, [])

	def test_a_standalone_marker_inside_a_multi_line_call_covers_it(self):
		src = f"text = Path(\n\t{REASON}\n\t__file__,\n).read_text()"
		assert len(scan(src).unmarked) == 0

	def test_a_standalone_marker_inside_a_bracket_covers_the_next_line(self):
		src = f"x = [\n\t{REASON}\n\tPath(__file__).read_text(),\n]"
		assert len(scan(src).unmarked) == 0
		trailing = f"x = [\n\tPath(__file__).read_text(),  {REASON}\n\tPath(__file__).read_text(),\n]"
		assert [s.line - PRELUDE.count("\n") for s in scan(trailing).unmarked] == [3]

	def test_a_marker_after_another_comment_is_found(self):
		result = scan(f"Path(__file__).read_text()  # noqa: S101  {REASON}")
		assert (len(result.unmarked), result.markers, result.problems) == (0, 1, [])

	@pytest.mark.parametrize("reason", ["- - -", "ok 12 34", "... ... ...", "a b"])
	def test_a_reason_needs_three_words_that_hold_a_letter(self, reason):
		result = scan(f"Path(__file__).read_text()  # test-lint: repo-read-ok {reason}")
		assert [p.text for p in result.problems] == [SHORT]
		assert len(result.unmarked) == 1

	def test_a_reason_of_three_words_is_enough(self):
		result = scan("Path(__file__).read_text()  # test-lint: repo-read-ok a b c")
		assert (len(result.unmarked), result.markers, result.problems) == (0, 1, [])


# ---------------------------------------------------------------------------
# The source hint and the hint-free early exit
# ---------------------------------------------------------------------------


class TestHintExit:
	@pytest.mark.parametrize(
		"header",
		[
			"from .support import ROOT",
			"from.support import ROOT",
			"from  .support import ROOT",
			"from\t.support import ROOT",
			"from \\\n.support import ROOT",
		],
	)
	def test_the_relative_import_hint_survives_odd_spacing(self, header):
		result = scan_source(f"{header}\nopen(ROOT)\n", others={"optimus.tests.support": "ROOT = __file__"})
		assert len(result.sites) == 1

	def test_the_early_exit_gives_the_result_of_a_full_scan(self, monkeypatch):
		text = (
			"def test_x(tmp_path):\n\t(tmp_path / 'a').read_text()\n\topen('x.txt')\n"
			"# test-lint: repo-read-ok three word reason\n"
		)
		early = scan_source(text)
		monkeypatch.setattr(scanner, "HINT_RE", re.compile(b""))  # matches everywhere: no early exit
		assert scan_source(text) == early
		assert early.sites == []


# ---------------------------------------------------------------------------
# Hostile and unreadable input
# ---------------------------------------------------------------------------


class TestUnreadableInput:
	def test_a_very_deep_expression_is_a_problem_line_not_a_crash(self):
		deep = "x = " + " + ".join(["__file__"] + ["'a'"] * 3000)
		result = scan_source(deep)
		assert result.failed
		assert [p.text.split(":")[0] for p in result.problems] in (["cannot parse"], ["cannot analyse"])

	def test_the_gate_reports_a_very_deep_expression(self, tmp_path):
		deep = "x = " + " + ".join(["__file__"] + ["'a'"] * 3000) + "\n"
		message = gate(tmp_path, {"test_a.py": deep})
		assert "t/test_a.py:1: cannot " in message

	def test_a_directory_named_like_a_module_is_skipped(self, tmp_path):
		(tmp_path / "x.py").mkdir()
		assert gate(tmp_path, {"test_a.py": CLEAN}) is None

	def test_a_dangling_symlink_is_a_problem_line(self, tmp_path):
		tree(tmp_path, {"test_a.py": CLEAN})
		os.symlink(tmp_path / "missing", tmp_path / ".#test_x.py")
		message = check_tree(tmp_path, {}, 0, {}, prefix="t/")
		assert "t/.#test_x.py:1: cannot read" in message

	@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads any file")
	def test_an_unreadable_file_is_a_problem_line(self, tmp_path):
		tree(tmp_path, {"test_a.py": READ})
		(tmp_path / "test_a.py").chmod(0)
		try:
			message = check_tree(tmp_path, {}, 0, {}, prefix="t/")
		finally:
			(tmp_path / "test_a.py").chmod(0o644)
		assert "t/test_a.py:1: cannot read: Permission denied" in message
		assert "To fix a new repo read" not in message

	def test_a_file_deleted_after_the_listing_is_a_problem_line(self, tmp_path, monkeypatch):
		monkeypatch.setattr(
			scanner, "scanned_files", lambda root, exempt: {"test_gone.py": root / "test_gone.py"}
		)
		message = check_tree(tmp_path, {}, 0, {}, prefix="t/")
		assert "t/test_gone.py:1: cannot read" in message

	def test_a_dotted_file_name_is_scanned_under_its_own_key(self, tmp_path):
		assert gate(tmp_path, {"test_a.old.py": READ}, {"test_a.old.py": 1}) is None
		assert "t/test_a.old.py:5" in gate(tmp_path, {"test_a.old.py": READ})

	def test_a_dotted_file_name_resolves_its_relative_imports_from_its_own_folder(self, tmp_path):
		files = {
			"sub/support.py": "ROOT = __file__\n",
			"sub/test_a.old.py": "from .support import ROOT\nopen(ROOT)\n",
		}
		assert "t/sub/test_a.old.py:2" in gate(tmp_path, files)

	def test_an_import_of_a_module_that_is_a_directory_is_ignored(self, tmp_path):
		files = {"test_a.py": "from optimus.tests.support import ROOT\nopen(ROOT)\n"}
		tree(tmp_path, files)
		(tmp_path / "support.py").mkdir()
		assert check_tree(tmp_path, {}, 0, {}, prefix="t/") is None


# ---------------------------------------------------------------------------
# More gate behaviour
# ---------------------------------------------------------------------------


class TestGateDetails:
	@pytest.mark.parametrize(
		"name",
		[
			"fixtures/test_x.py",
			"data/test_x.py",
			"fixtures/x_test.py",
			"data/conftest.py",
			"sub/fixtures/test_x.py",
			"sub/data/conftest.py",
		],
	)
	def test_a_collectable_file_inside_a_skipped_folder_is_scanned(self, tmp_path, name):
		assert f"t/{name}:5" in gate(tmp_path, {name: READ})

	def test_a_helper_file_inside_a_skipped_folder_is_still_skipped(self, tmp_path):
		assert gate(tmp_path, {"fixtures/helper.py": READ, "data/loader.py": READ}) is None

	@pytest.mark.parametrize(
		"consumer,left,right",
		[("test_c.py", "a", "b"), ("0c_test.py", "a", "b"), ("test_c.py", "b", "a")],
	)
	def test_an_import_cycle_does_not_make_the_result_depend_on_the_scan_order(
		self, tmp_path, consumer, left, right
	):
		files = {
			f"{left}.py": f"X = __file__\nfrom optimus.tests.{right} import Y\n",
			f"{right}.py": f"from optimus.tests.{left} import X\nY = X\n",
			consumer: f"from optimus.tests.{right} import Y\nopen(Y)\n",
		}
		message = gate(tmp_path, files)
		assert f"t/{consumer}:2" in message and "1 problem(s)" in message

	def test_a_parse_failure_hides_the_marked_total_lowering(self, tmp_path):
		message = gate(tmp_path, {"test_a.py": "def broken(:\n", "test_b.py": CLEAN}, marked=2)
		assert "cannot parse" in message
		assert "lower it" not in message and pasted(message) == []

	def test_an_exempt_key_for_a_missing_file_fails(self, tmp_path):
		message = gate(tmp_path, {"test_a.py": CLEAN}, exempt={"test_gone.py": "a reason"})
		assert "t/test_gone.py: in EXEMPT but no such file" in message

	def test_a_syntax_error_on_line_three_is_reported_on_line_three(self, tmp_path):
		message = gate(tmp_path, {"test_a.py": "x = 1\ny = 2\ndef broken(:\n"})
		assert "t/test_a.py:3: cannot parse" in message
		assert "<unknown>" not in message

	def test_problems_come_out_sorted_whatever_the_creation_order(self, tmp_path):
		files = {"test_c.py": READ, "test_a.py": READ, "test_b.py": READ, "sub/test_d.py": READ}
		message = gate(tmp_path, files)
		names = ("sub/test_d.py", "test_a.py", "test_b.py", "test_c.py")
		positions = [message.index(f"t/{name}:5") for name in names]
		assert positions == sorted(positions)

	def test_two_missing_keys_come_out_sorted_with_a_delete_line_each(self, tmp_path):
		message = gate(tmp_path, {"test_a.py": CLEAN}, {"test_z.py": 2, "test_m.py": 1})
		assert message.index("t/test_m.py") < message.index("t/test_z.py")
		assert 'delete: "test_m.py": 1,' in message and 'delete: "test_z.py": 2,' in message

	def test_the_message_names_the_rule_in_its_title(self, tmp_path):
		message = gate(tmp_path, {"test_a.py": READ})
		assert "A test must not read a repo file. See CONTRIBUTING.md, 'Test gate'." in message

	def test_marker_problems_alone_print_no_remedies(self, tmp_path):
		message = gate(tmp_path, {"test_a.py": f"x = 1\n{REASON}\ny = 2\n"})
		assert "stale marker" in message and "delete this comment" in message
		assert "To fix a new repo read" not in message

	def test_a_decrease_alone_prints_no_remedies(self, tmp_path):
		assert "To fix a new repo read" not in gate(tmp_path, {"test_a.py": READ}, {"test_a.py": 2})

	def test_a_rename_gets_a_hint(self, tmp_path):
		message = gate(tmp_path, {"test_new.py": READ}, {"test_old.py": 1})
		assert "Renamed a file? Move its BASELINE key" in message

	def test_a_vanished_key_alone_gets_no_rename_hint(self, tmp_path):
		assert "Renamed a file?" not in gate(tmp_path, {"test_a.py": CLEAN}, {"test_old.py": 1})

	def test_a_long_report_is_capped(self, tmp_path):
		files = {f"test_{n:03d}.py": READ for n in range(60)}
		message = gate(tmp_path, files)
		assert message.startswith("repo-read lint: 60 problem(s)")
		assert sum(line.startswith("t/") for line in message.splitlines()) == 40
		assert re.search(r"^\.\.\. and \d+ more line\(s\)$", message, re.M)

	def test_a_constant_defined_in_a_package_init_propagates(self, tmp_path):
		files = {
			"__init__.py": "ROOT = __file__\n",
			"test_a.py": "from optimus.tests import ROOT\nopen(ROOT)\n",
			"sub/__init__.py": "from .support import ROOT\n",
			"sub/support.py": "ROOT = __file__\n",
			"sub/test_b.py": "from . import ROOT\nopen(ROOT)\n",
			"sub/test_c.py": "from optimus.tests.sub import ROOT\nopen(ROOT)\n",
		}
		message = gate(tmp_path, files)
		assert all(f"t/{name}:2" in message for name in ("test_a.py", "sub/test_b.py", "sub/test_c.py"))

	def test_more_markers_than_marked_total_lists_each_marker_and_prints_no_number(self, tmp_path):
		one = READ.replace("\tPath", f"\t{REASON}\n\tPath")
		message = gate(tmp_path, {"test_a.py": one, "test_b.py": one}, marked=0)
		assert "t/test_a.py:5" in message and "t/test_b.py:5" in message
		assert "raise MARKED_TOTAL by the number of markers you added" in message
		assert not re.search(r"MARKED_TOTAL\s*(=|to)\s*\d", message)

	def test_fewer_markers_than_marked_total_lists_the_remaining_markers(self, tmp_path):
		one = READ.replace("\tPath", f"\t{REASON}\n\tPath")
		message = gate(tmp_path, {"test_a.py": one}, marked=3)
		assert pasted(message) == ["MARKED_TOTAL = 1"]
		assert "t/test_a.py:5" in message


# ---------------------------------------------------------------------------
# The real tree
# ---------------------------------------------------------------------------


class TestRealTree:
	def test_the_baseline_file_is_sorted_and_has_no_zero_entry(self):
		assert list(baseline.BASELINE) == sorted(baseline.BASELINE)
		assert all(isinstance(n, int) and n > 0 for n in baseline.BASELINE.values())
		assert all(reason.strip() for reason in baseline.EXEMPT.values())

	def test_the_repo_reads_match_the_baseline(self):
		message = check_tree(
			TESTS_DIR, baseline.BASELINE, baseline.MARKED_TOTAL, baseline.EXEMPT, prefix="optimus/tests/"
		)
		if message:
			pytest.fail(message, pytrace=False)
