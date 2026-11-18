# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The LLM index-suggestion path is deleted: the retired Settings toggle is read-only and the
report template adds no new safe sink."""

import json
import os

from optimus import ai_fix


def test_ai_suggest_indexes_setting_is_read_only_and_says_so():
	path = os.path.join(
		os.path.dirname(ai_fix.__file__), "optimus", "doctype", "optimus_settings", "optimus_settings.json",
	)
	with open(path, encoding="utf-8") as fh:
		fields = json.load(fh)["fields"]
	field = next(f for f in fields if f["fieldname"] == "ai_suggest_indexes")
	assert field.get("read_only") == 1
	assert field["label"] == "Index recommendations (DB-tables breakdown, retired)"
	assert field["description"].startswith("This setting no longer does anything.")


def test_report_template_adds_no_new_safe_sink():
	"""Deleting the block removed one `| safe`; the count must not grow past the audited baseline."""
	path = os.path.join(os.path.dirname(ai_fix.__file__), "templates", "report.html")
	with open(path, encoding="utf-8") as fh:
		now = fh.read().count("| safe")
	assert now == 18  # 19 at 719f3dd minus the deleted ai_index block
