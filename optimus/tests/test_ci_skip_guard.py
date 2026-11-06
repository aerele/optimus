# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The CI skip guard (``ci_skip_guard`` with ``ci_skips.ALLOWED``) and the integration-module
guard.

The guard's behaviour is proven in subprocesses (a fresh pytest, no conftest stub), so the
checks see the real exit code and output a CI step would.
"""

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from optimus.tests import ci_skip_guard, ci_skips
from optimus.tests.ai_eval_support import REPO_ROOT

Row = ci_skips.Row
ROWS = (
	Row(None, r"tool X is missing", None, "an environment gate"),
	Row("test_a.py", r"needs real Frappe", 2, "at most two"),
	Row(None, r"old python", {"default": 0, "3.10": 3}, "per version"),
)


def _violations(skips, python="3.14"):
	return ci_skip_guard.violations(skips, ROWS, python)


def test_a_listed_skip_passes():
	assert _violations([("optimus/tests/test_any.py::test_x", "tool X is missing")]) == []


def test_an_unlisted_skip_is_reported_with_its_test_and_reason():
	problems = _violations([("optimus/tests/test_any.py::test_x", "something new")])
	assert problems == ["unlisted skip: optimus/tests/test_any.py::test_x: something new"]


def test_a_row_for_one_file_does_not_cover_the_same_reason_in_another_file():
	problems = _violations([("optimus/tests/test_b.py::test_x", "needs real Frappe")])
	assert len(problems) == 1 and problems[0].startswith("unlisted skip")


def test_rows_match_on_the_test_file_of_the_node_id_not_on_where_skip_was_called():
	skips = [("optimus/tests/test_a.py::test_x[param]", "needs real Frappe")]
	assert _violations(skips) == []


def test_a_row_over_its_count_is_reported_and_under_its_count_is_fine():
	skip = ("optimus/tests/test_a.py::t1", "needs real Frappe")
	assert _violations([skip, skip]) == []
	problem = _violations([skip] * 3)[0]
	assert problem.startswith("3 skips for (test_a.py") and "at most 2" in problem


def test_counts_can_differ_per_python_version():
	skips = [("optimus/tests/test_x.py::t", "old python")] * 3
	assert _violations(skips, "3.10") == []
	assert "at most 0" in _violations(skips, "3.14")[0]


@pytest.mark.parametrize("status", [1, 2, 3, 4, 5])
def test_a_status_other_than_ok_is_never_overridden(status):
	assert ci_skip_guard.escalate(pytest.ExitCode(status), ["unlisted skip: x"]) == status


def test_a_passing_run_with_a_problem_becomes_a_test_failure():
	assert ci_skip_guard.escalate(pytest.ExitCode.OK, ["x"]) == pytest.ExitCode.TESTS_FAILED
	assert ci_skip_guard.escalate(pytest.ExitCode.OK, []) == pytest.ExitCode.OK


def test_every_allowlist_row_has_a_reason_a_valid_pattern_and_a_sane_count():
	for row in ci_skips.ALLOWED:
		assert row.why.strip(), row
		re.compile(row.reason)
		limit = row.max_count
		assert limit is None or isinstance(limit, int) or "default" in limit, row


def test_the_guard_is_off_on_xdist_workers_and_when_real_frappe_is_installed(monkeypatch):
	monkeypatch.delenv(ci_skip_guard.FORCE_ENV, raising=False)
	monkeypatch.setattr(ci_skip_guard, "frappe_is_real", lambda: False)
	assert ci_skip_guard.guard_applies(SimpleNamespace()) is True
	assert ci_skip_guard.guard_applies(SimpleNamespace(workerinput={})) is False
	monkeypatch.setattr(ci_skip_guard, "frappe_is_real", lambda: True)
	assert ci_skip_guard.guard_applies(SimpleNamespace()) is False
	monkeypatch.setenv(ci_skip_guard.FORCE_ENV, "1")
	assert ci_skip_guard.guard_applies(SimpleNamespace()) is True
	assert ci_skip_guard.guard_applies(SimpleNamespace(workerinput={})) is False


def test_the_conftest_stub_does_not_count_as_real_frappe(monkeypatch):
	import types

	monkeypatch.setitem(sys.modules, "frappe", types.ModuleType("frappe"))
	assert ci_skip_guard.frappe_is_real() is False
	real = types.ModuleType("frappe")
	real.__file__ = "/bench/apps/frappe/frappe/__init__.py"
	monkeypatch.setitem(sys.modules, "frappe", real)
	assert ci_skip_guard.frappe_is_real() is True


# --- the guard in a fresh pytest ----------------------------------------------------------


def _run(tmp_path, files, *args, force=True, extra_path=()):
	for name, body in files.items():
		(tmp_path / name).write_text(textwrap.dedent(body), encoding="utf-8")
	env = {key: value for key, value in os.environ.items() if key != ci_skip_guard.FORCE_ENV}
	env["PYTHONPATH"] = os.pathsep.join([REPO_ROOT, *[str(p) for p in extra_path]])
	if force:
		env[ci_skip_guard.FORCE_ENV] = "1"
	return subprocess.run(
		[
			sys.executable,
			"-m",
			"pytest",
			"-p",
			ci_skip_guard.PLUGIN_NAME,
			"-p",
			"no:cacheprovider",
			"-q",
			"--rootdir",
			str(tmp_path),
			str(tmp_path),
			*args,
		],
		cwd=tmp_path,
		env=env,
		capture_output=True,
		text=True,
		timeout=120,
	)


_PASS_AND_SKIP = """
	import pytest

	def test_passes():
		pass

	def test_skips():
		pytest.skip({reason!r})
"""


def test_an_unlisted_skip_fails_a_passing_run_with_its_own_section(tmp_path):
	result = _run(tmp_path, {"test_new.py": _PASS_AND_SKIP.format(reason="zz not in the allowlist")})
	assert result.returncode == 1, result.stdout
	assert "1 passed, 1 skipped" in result.stdout
	assert "Unlisted CI skips" in result.stdout
	assert "unlisted skip: test_new.py::test_skips: zz not in the allowlist" in result.stdout


def test_a_listed_skip_leaves_the_run_green(tmp_path):
	result = _run(tmp_path, {"test_ok.py": _PASS_AND_SKIP.format(reason="node not installed")})
	assert result.returncode == 0, result.stdout
	assert "Unlisted CI skips" not in result.stdout


def test_a_collection_time_skip_is_checked_too(tmp_path):
	files = {
		"test_ok.py": "def test_passes():\n\tpass\n",
		"test_gone.py": "import pytest\npytest.importorskip('zz_missing_module_for_guard')\n",
	}
	result = _run(tmp_path, files)
	assert result.returncode == 1, result.stdout
	assert "unlisted skip: test_gone.py" in result.stdout


def test_a_row_over_its_count_fails_the_run(tmp_path):
	body = """
		import pytest

		@pytest.mark.parametrize("n", range(7))
		def test_download(n):
			pytest.skip("Requires installed Frappe; a stub cannot prove the download route")
	"""
	result = _run(tmp_path, {"test_permissions_download_boundary.py": body})
	assert result.returncode == 1, result.stdout
	assert "7 skips for (test_permissions_download_boundary.py" in result.stdout
	assert "at most 6" in result.stdout


def test_an_expected_failure_is_not_a_skip(tmp_path):
	body = """
		import pytest

		@pytest.mark.xfail(reason="known")
		def test_known():
			assert False

		def test_passes():
			pass
	"""
	result = _run(tmp_path, {"test_x.py": body})
	assert result.returncode == 0, result.stdout


def test_nothing_selected_keeps_pytests_own_status(tmp_path):
	result = _run(tmp_path, {"test_x.py": "def test_a():\n\tpass\n"}, "-k", "no_such_test")
	assert result.returncode == 5, result.stdout


def test_real_frappe_leaves_the_run_alone(tmp_path):
	package = tmp_path / "fake_site" / "frappe"
	package.mkdir(parents=True)
	(package / "__init__.py").write_text("", encoding="utf-8")
	result = _run(
		tmp_path,
		{"test_new.py": _PASS_AND_SKIP.format(reason="zz not in the allowlist")},
		force=False,
		extra_path=[tmp_path / "fake_site"],
	)
	assert result.returncode == 0, result.stdout
	assert "Unlisted CI skips" not in result.stdout


# --- every integration module is scheduled or consciously manual ---------------------------

INTEGRATION_DIR = Path(REPO_ROOT) / "optimus" / "tests_integration"
INTEGRATION_WORKFLOW = Path(REPO_ROOT) / ".github" / "workflows" / "integration.yml"

# Modules integration.yml does not run, each with the reason.
MANUAL_ONLY = {
	"test_api_hardening": "Frappe's own POST enforcement and the real Redis list; the Frappe-free "
	"half runs in test_submit_frontend_metrics.py and test_api_session_actions.py",
}


def _scheduled_modules():
	text = INTEGRATION_WORKFLOW.read_text(encoding="utf-8")
	block = re.search(r"INTEGRATION_MODULES: \|-\n((?:\s+test_\w+\n)+)", text)
	assert block, "INTEGRATION_MODULES list not found in integration.yml"
	return set(block.group(1).split())


def test_every_integration_module_is_scheduled_or_manual_with_a_reason():
	modules = {path.stem for path in INTEGRATION_DIR.glob("test_*.py")}
	unaccounted = modules - _scheduled_modules() - set(MANUAL_ONLY)
	assert not unaccounted, (
		f"{sorted(unaccounted)} are in neither integration.yml's INTEGRATION_MODULES nor MANUAL_ONLY"
	)


def test_the_integration_lists_name_only_real_modules():
	modules = {path.stem for path in INTEGRATION_DIR.glob("test_*.py")}
	assert (_scheduled_modules() | set(MANUAL_ONLY)) <= modules
	assert all(reason.strip() for reason in MANUAL_ONLY.values())
