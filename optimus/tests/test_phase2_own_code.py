# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Line profiling follows wrappers to the real function and never profiles Optimus's own code."""

import inspect
import json
import os
import types

import pytest

from optimus import capture as phase1_capture
from optimus.line_profile import capture, picker

OWN_CODE_REASON = "Optimus's own code is not line-profiled"

FILES = {
	"__init__.py": "",
	"core.py": "def get_doc(value):\n\tresult = value + 1\n\treturn result * 2\n",
	"deco.py": (
		"import functools\n\n\ndef deco(fn):\n\t@functools.wraps(fn)\n\tdef wrapper(*args, **kwargs):\n"
		"\t\treturn fn(*args, **kwargs)\n\n\treturn wrapper\n"
	),
	"typing_validations.py": (
		"import functools\n\n\ndef validate(fn):\n\t@functools.wraps(fn)\n\tdef wrapper(*args, **kwargs):\n"
		"\t\treturn fn(*args, **kwargs)\n\n\treturn wrapper\n"
	),
	"api.py": (
		"from zz_p2app.deco import deco\nfrom zz_p2app.typing_validations import validate\n\n\n"
		"@deco\ndef decorated(value):\n\ttotal = value + 1\n\treturn total * 2\n\n\n"
		"@validate\ndef whitelisted(value):\n\ttotal = value + 3\n\treturn total * 2\n\n\n"
		"def plain(value):\n\ttotal = value + 5\n\treturn total * 2\n"
	),
	"optimus/__init__.py": "",
	"optimus/report.py": "def build(value):\n\ttotal = value + 7\n\treturn total * 2\n",
}


@pytest.fixture
def app(tmp_path, monkeypatch):
	import importlib

	from optimus.renderer import source

	root = tmp_path / "zz_p2app"
	for name, text in FILES.items():
		path = root / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(text)
	monkeypatch.syspath_prepend(str(tmp_path))
	monkeypatch.setattr(source, "_installed_apps", lambda: frozenset({"zz_p2app", "optimus"}))
	monkeypatch.setattr(capture, "_FRAPPE_AVAILABLE", True)
	core = importlib.import_module("zz_p2app.core")
	core.get_doc = phase1_capture._make_wrap(core.get_doc, "get_doc", local_proxy=types.SimpleNamespace())
	yield tmp_path
	for name in [m for m in list(__import__("sys").modules) if m.split(".")[0] == "zz_p2app"]:
		del __import__("sys").modules[name]


def _profile(monkeypatch, picks, calls):
	"""prepare -> make_profiler -> serialize_stats -> aggregate_samples, as the worker runs them."""
	prepared = capture.prepare_line_profile_picks(picks)
	metas = json.loads(prepared.picks_json)
	sources = json.loads(prepared.source_json)
	fns = [capture._resolve_attr(meta["dotted_path"]) for meta in metas]
	monkeypatch.setattr(capture, "_get_or_resolve_picks", lambda run: fns)
	profiler = capture.make_profiler("run")
	profiler.enable_by_count()
	try:
		for call in calls:
			call()
	finally:
		profiler.disable_by_count()
	samples = capture.serialize_stats(profiler)
	full = [{**meta, "source_lines": sources[meta["dotted_path"]]} for meta in metas]
	return {r["dotted_path"]: r for r in capture.aggregate_samples([samples], full)}, metas


def _hits(result):
	return sum(line["hits"] for line in result["lines"])


class TestHitsJoin:
	def test_a_make_wrap_target_gets_hits_on_the_real_file(self, app, monkeypatch):
		import zz_p2app.core as core

		results, metas = _profile(monkeypatch, [{"dotted_path": "zz_p2app.core.get_doc"}],
			[lambda: core.get_doc(1), lambda: core.get_doc(2)])
		result = results["zz_p2app.core.get_doc"]
		assert os.path.realpath(result["file"]) == os.path.realpath(app / "zz_p2app" / "core.py")
		assert _hits(result) > 0
		assert metas[0]["first_lineno"] == 1

	@pytest.mark.parametrize("name", ["decorated", "whitelisted"])
	def test_a_decorator_from_another_module_gets_hits_on_the_users_file(self, app, monkeypatch, name):
		import zz_p2app.api as api

		fn = getattr(api, name)
		results, metas = _profile(monkeypatch, [{"dotted_path": f"zz_p2app.api.{name}"}], [lambda: fn(1)])
		result = results[f"zz_p2app.api.{name}"]
		assert os.path.realpath(result["file"]) == os.path.realpath(app / "zz_p2app" / "api.py")
		assert _hits(result) > 0
		assert metas[0]["first_lineno"] == inspect.unwrap(fn).__code__.co_firstlineno

	def test_a_plain_function_and_a_user_optimus_folder_still_get_hits(self, app, monkeypatch):
		import zz_p2app.api as api
		import zz_p2app.optimus.report as report

		results, _ = _profile(monkeypatch,
			[{"dotted_path": "zz_p2app.api.plain"}, {"dotted_path": "zz_p2app.optimus.report.build"}],
			[lambda: api.plain(1), lambda: report.build(1)])
		assert all(_hits(r) > 0 for r in results.values())


class TestWrapperChain:
	def test_make_wrap_exposes_the_original_as_wrapped(self):
		def original(*args, **kwargs):
			return 1

		wrapped = phase1_capture._make_wrap(original, "get_doc", local_proxy=types.SimpleNamespace())
		assert wrapped.__wrapped__ is original
		assert wrapped._profiler_original is original
		assert inspect.unwrap(wrapped) is original

	def test_a_wrapped_pick_resolves_to_the_real_file_and_line(self, app):
		meta = picker.resolve_freeform("zz_p2app.api.decorated")
		real = inspect.unwrap(__import__("zz_p2app.api", fromlist=["x"]).decorated).__code__
		assert (meta["file"], meta["lineno"]) == (real.co_filename, real.co_firstlineno)
		assert meta["eligible"] is True

	def test_an_unwrap_cycle_is_ineligible_without_raising(self, app):
		import zz_p2app.api as api

		def one():
			pass

		def two():
			pass

		one.__wrapped__, two.__wrapped__ = two, one
		api.looping = one
		meta = picker.resolve_freeform("zz_p2app.api.looping")
		assert meta["eligible"] is False
		assert meta["ineligible_reason"] == f"{picker.UNRESOLVED_REASON} (ValueError)"

	def test_an_object_whose_wrapped_lookup_raises_is_ineligible_without_raising(self, app):
		import zz_p2app.api as api

		class Hostile:
			def __getattr__(self, name):
				raise RuntimeError("no attribute access")

		api.hostile = Hostile()
		meta = picker.resolve_freeform("zz_p2app.api.hostile")
		assert meta["eligible"] is False
		assert meta["ineligible_reason"] == f"{picker.UNRESOLVED_REASON} (RuntimeError)"


class TestOptimusCodeIsNotProfiled:
	def test_a_code_object_without_a_filename_does_not_raise(self):
		target = types.SimpleNamespace(__code__=types.SimpleNamespace(co_firstlineno=3), __name__="odd")
		eligible, _reason, _code = picker._profile_target(target)
		assert eligible is True

	def test_a_typed_optimus_function_is_ineligible_with_a_fixed_reason(self):
		meta = picker.resolve_freeform("optimus.analyzers.base.is_optimus_path")
		assert meta["eligible"] is False
		assert meta["ineligible_reason"] == OWN_CODE_REASON

	def test_a_frappe_style_function_wrapped_by_optimus_is_eligible_and_points_at_the_original(self, app):
		meta = picker.resolve_freeform("zz_p2app.core.get_doc")
		assert meta["eligible"] is True
		assert meta["file"].endswith("zz_p2app/core.py")

	def test_a_user_module_with_optimus_in_its_path_stays_eligible(self, app):
		meta = picker.resolve_freeform("zz_p2app.optimus.report.build")
		assert meta["eligible"] is True and meta["ineligible_reason"] is None

	def test_the_reason_is_visible_in_the_prepared_metadata_without_paths(self, app, monkeypatch):
		prepared = capture.prepare_line_profile_picks(
			[{"dotted_path": "optimus.analyzers.base.is_optimus_path"}, {"dotted_path": "zz_p2app.api.plain"}]
		)
		by_path = {entry["dotted_path"]: entry for entry in prepared.resolved}
		assert by_path["optimus.analyzers.base.is_optimus_path"]["ineligible_reason"] == OWN_CODE_REASON
		assert [m["dotted_path"] for m in json.loads(prepared.picks_json)] == ["zz_p2app.api.plain"]

	def test_all_ineligible_names_each_pick_and_its_reason(self, app):
		with pytest.raises(capture.NoEligiblePickError) as caught:
			capture.prepare_line_profile_picks([{"dotted_path": "optimus.analyzers.base.is_optimus_path"}])
		assert str(caught.value) == (
			f"No selected function can be line-profiled: optimus.analyzers.base.is_optimus_path: {OWN_CODE_REASON}"
		)

	def test_the_no_eligible_error_is_a_capture_error_so_the_manual_path_still_shows_it(self):
		assert issubclass(capture.NoEligiblePickError, capture.CaptureError)

	def test_all_ineligible_lists_at_most_five_picks_and_no_filesystem_paths(self, app, monkeypatch):
		def refuse(dotted):
			raise picker.PickerError(
				f"could not import any prefix of '{dotted}' (last error: cannot import name 'q' "
				"from 'x' (/home/dev/bench/apps/x/x/__init__.py))"
			)

		monkeypatch.setattr(picker, "resolve_freeform", refuse)
		picks = [{"dotted_path": f"zz_p2app.api.fn{i}"} for i in range(7)]
		with pytest.raises(capture.CaptureError) as caught:
			capture.prepare_line_profile_picks(picks)
		text = str(caught.value)
		assert text.startswith("No selected function can be line-profiled: ")
		assert "fn4" in text and "fn5" not in text
		assert "/home" not in text and "__init__.py" not in text

	def test_an_own_code_pick_carries_no_optimus_file_path(self):
		meta = picker.resolve_freeform("optimus.analyzers.base.is_optimus_path")
		assert not picker.is_optimus_code_file(meta["file"]) and meta["file"] == "<unknown>"
		assert meta["lineno"] == 0

	def test_the_check_resolves_symlinks_and_respects_directory_boundaries(self, tmp_path, monkeypatch):
		package = tmp_path / "optimus"
		package.mkdir()
		(tmp_path / "optimus_other").mkdir()
		link = tmp_path / "via_link"
		link.symlink_to(package, target_is_directory=True)
		monkeypatch.setattr(picker, "_OPTIMUS_PACKAGE_DIR", os.path.realpath(package))
		assert picker.is_optimus_code_file(str(package / "capture.py"))
		assert picker.is_optimus_code_file(str(link / "capture.py"))
		assert not picker.is_optimus_code_file(str(tmp_path / "optimus_other" / "capture.py"))
		assert not picker.is_optimus_code_file(str(tmp_path / "myapp" / "optimus" / "x.py"))
		assert not picker.is_optimus_code_file("<server_script: x>")
		assert not picker.is_optimus_code_file("")
		assert not picker.is_optimus_code_file(None)

	@pytest.mark.parametrize("error", [OSError("unreadable"), ValueError("embedded null byte")])
	def test_a_path_that_cannot_be_resolved_is_not_own_code(self, monkeypatch, error):
		def refuse(path):
			raise error

		monkeypatch.setattr(picker.os.path, "realpath", refuse)
		assert not picker.is_optimus_code_file("/any/path/capture.py")

	def test_a_run_without_any_reason_still_gets_the_plain_message(self, app, monkeypatch):
		monkeypatch.setattr(picker, "resolve_freeform", lambda dotted: {"dotted_path": dotted, "eligible": False})
		with pytest.raises(capture.NoEligiblePickError) as caught:
			capture.prepare_line_profile_picks([{"dotted_path": "zz_p2app.api.plain"}])
		assert str(caught.value) == "No selected function has eligible source. Check the selected paths."
