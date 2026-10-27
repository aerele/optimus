"""A green bench exit status must not hide failed or undiscovered integration tests."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_integration_log.py"


@pytest.mark.parametrize("output,expected", [
	("Ran 4 tests in 0.2s\n\nOK\n", 0),
	("Ran 4 tests in 0.2s\n\nOK (skipped=1)\n", 0),
	("Ran 1 test in 0.2s\n\nOK (skipped=1)\n", 1),
	("Ran 4 tests in 0.2s\n\nFAILED (failures=3)\n", 1),
	("Ran 0 tests in 0.0s\n\nOK\n", 1),
	("Site not found\n", 1),
	("Ran 4 tests in 0.2s\n", 1),
	("ERROR: test_one\nRan 4 tests in 0.2s\n\nOK\n", 1),
	("\x1b[32mRan 4 tests in 0.2s\n\nOK\x1b[0m\nShutdown complete\n", 0),
])
def test_bench_result_exit_status(tmp_path, output, expected):
	log = tmp_path / "integration.log"
	log.write_text(output)
	result = subprocess.run([sys.executable, str(SCRIPT), str(log)], capture_output=True, text=True)
	assert result.returncode == expected
	assert ("::error::" in result.stderr) == bool(expected)
