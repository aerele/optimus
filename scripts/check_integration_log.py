"""Fail CI when bench exits successfully without a successful unittest result."""

from __future__ import annotations

import re
import sys
from pathlib import Path


def successful_result(output: str) -> bool:
	output = re.sub(r"\x1b\[[0-9;]*m", "", output)
	# v15's bench command does not propagate TextTestResult.wasSuccessful().
	if re.search(r"^(?:FAILED\b|FAIL:|ERROR:)", output, re.MULTILINE):
		return False
	results = re.findall(
		r"^Ran (\d+) tests? in [^\n]+\n\s*\n(OK(?: \([^\n]*\))?)\s*$",
		output,
		re.MULTILINE,
	)
	if not results:
		return False
	for count, outcome in results:
		skipped = re.search(r"\bskipped=(\d+)", outcome)
		if int(count) <= (int(skipped[1]) if skipped else 0):
			return False
	return True


def main() -> int:
	path = Path(sys.argv[1])
	if successful_result(path.read_text(encoding="utf-8", errors="replace")):
		return 0
	print(f"::error::{path.name}: no successful nonempty unittest result", file=sys.stderr)
	return 1


if __name__ == "__main__":
	raise SystemExit(main())
