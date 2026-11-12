# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Resolving a pick never swallows the worker's job timeout."""

import sys
import types

import pytest

from optimus.line_profile import picker

pytestmark = pytest.mark.rq


def test_a_job_timeout_during_the_unwrap_still_escapes(monkeypatch):
	timeouts = pytest.importorskip("rq.timeouts")

	class Slow:
		def __getattr__(self, name):
			raise timeouts.JobTimeoutException("job timed out")

	module = types.ModuleType("zz_slow_pick")
	module.slow = Slow()
	monkeypatch.setitem(sys.modules, "zz_slow_pick", module)
	with pytest.raises(timeouts.JobTimeoutException):
		picker.resolve_freeform("zz_slow_pick.slow")
