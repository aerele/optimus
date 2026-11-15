# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The browser shim must keep page-load timings: one observer call per type, once per load."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
SHIM = TESTS.parent / "public/js/optimus_frontend.js"
HARNESS = TESTS / "fixtures/vitals_observer.js"
SCENARIOS = re.findall(r"^\t(\w+)\(\) \{", HARNESS.read_text(), re.M)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_vitals_shim_scenario(scenario):
	node = shutil.which("node")
	if not node:
		pytest.skip("node not installed")
	result = subprocess.run(
		[node, str(HARNESS), str(SHIM), scenario], capture_output=True, text=True, timeout=60
	)
	assert result.returncode == 0, result.stdout + result.stderr


def test_scenarios_were_found():
	assert len(SCENARIOS) >= 10
