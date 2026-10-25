# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Registration test for the v0.13.0 add_subscription_cli_fields patch: it must
be listed under [post_model_sync] in patches.txt (otherwise migrate never runs
it) and the module must import with a callable ``execute``."""

import importlib
from pathlib import Path

PATCH = "optimus.patches.v0_13_0.add_subscription_cli_fields"
PATCHES_TXT = Path(__file__).resolve().parent.parent / "patches.txt"


def _section_lines(section):
	lines, current = {}, None
	for raw in PATCHES_TXT.read_text().splitlines():
		line = raw.strip()
		if line.startswith("[") and line.endswith("]"):
			current = line[1:-1]
			lines[current] = []
		elif line and current:
			lines[current].append(line)
	return lines.get(section, [])


def test_patch_registered_under_post_model_sync():
	assert PATCH in _section_lines("post_model_sync")
	assert PATCH not in _section_lines("pre_model_sync")


def test_patch_module_imports_with_execute():
	module = importlib.import_module(PATCH)
	assert callable(getattr(module, "execute", None))
