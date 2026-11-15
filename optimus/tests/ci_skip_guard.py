# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""pytest plugin: fail the Frappe-free unit run on a skip that ``ci_skips.ALLOWED`` does not list.

``conftest`` registers it. It is active only where Frappe is not installed (CI, a pure pip
venv): a bench run, where the Frappe-bound tests run, is left alone, and so are xdist
workers (the controller sees every report). ``OPTIMUS_CI_SKIP_GUARD=1`` forces it on, for the
guard's own subprocess tests.

A passing run with an unlisted skip exits 1 with its own "Unlisted CI skips" section. Any
other non-zero status (2 interrupted, 3 internal error, 4 usage, 5 nothing collected) is kept.
"""

import importlib.util
import os
import re
import sys

import pytest

from optimus.tests import ci_skips

FORCE_ENV = "OPTIMUS_CI_SKIP_GUARD"
PLUGIN_NAME = "optimus.tests.ci_skip_guard"
_SKIPPED_PREFIX = "Skipped: "


def frappe_is_real() -> bool:
	module = sys.modules.get("frappe")
	if module is not None:
		return hasattr(module, "__file__")
	return importlib.util.find_spec("frappe") is not None


def guard_applies(config) -> bool:
	if hasattr(config, "workerinput"):
		return False
	return os.environ.get(FORCE_ENV) == "1" or not frappe_is_real()


def python_key() -> str:
	return f"{sys.version_info[0]}.{sys.version_info[1]}"


def skip_reason(longrepr) -> str:
	text = longrepr[2] if isinstance(longrepr, tuple) else str(longrepr)
	return text.removeprefix(_SKIPPED_PREFIX)


def _limit(row, python):
	if isinstance(row.max_count, dict):
		return row.max_count.get(python, row.max_count["default"])
	return row.max_count


def _matches(row, test_file, reason) -> bool:
	if row.file is not None and row.file != test_file:
		return False
	return re.search(row.reason, reason) is not None


def violations(skips, allowed, python) -> list[str]:
	"""``skips`` is ``[(nodeid, reason)]``; return a line per unlisted skip and per row over its count."""
	problems = []
	used = {}
	for nodeid, reason in skips:
		test_file = nodeid.split("::")[0].rsplit("/", 1)[-1]
		index = next((i for i, r in enumerate(allowed) if _matches(r, test_file, reason)), None)
		if index is None:
			problems.append(f"unlisted skip: {nodeid}: {reason}")
			continue
		used[index] = used.get(index, 0) + 1
	for index, count in used.items():
		row = allowed[index]
		limit = _limit(row, python)
		if limit is not None and count > limit:
			problems.append(
				f"{count} skips for ({row.file or 'any file'}, /{row.reason}/) but at most {limit} are listed "
				f"on Python {python}: {row.why}"
			)
	return problems


def escalate(exitstatus, problems):
	if problems and exitstatus == pytest.ExitCode.OK:
		return pytest.ExitCode.TESTS_FAILED
	return exitstatus


class SkipGuard:
	def __init__(self, allowed=None):
		self.allowed = ci_skips.ALLOWED if allowed is None else allowed
		self.skips = []
		self.problems = []

	def _record(self, report):
		if report.skipped and not hasattr(report, "wasxfail"):
			self.skips.append((report.nodeid, skip_reason(report.longrepr)))

	def pytest_runtest_logreport(self, report):
		self._record(report)

	def pytest_collectreport(self, report):
		self._record(report)

	def pytest_sessionfinish(self, session, exitstatus):
		self.problems = violations(self.skips, self.allowed, python_key())
		session.exitstatus = escalate(session.exitstatus, self.problems)

	def pytest_terminal_summary(self, terminalreporter):
		if not self.problems:
			return
		terminalreporter.section("Unlisted CI skips", sep="=", red=True)
		for line in self.problems:
			terminalreporter.line(line, red=True)
		terminalreporter.line(
			"Fix the cause, or add a row with a reason to optimus/tests/ci_skips.py.", red=True
		)


def pytest_configure(config):
	if config.pluginmanager.has_plugin(f"{PLUGIN_NAME}.guard") or not guard_applies(config):
		return
	config.pluginmanager.register(SkipGuard(), f"{PLUGIN_NAME}.guard")
