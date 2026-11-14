# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The per-action tables scroll inside their own box, so a narrow screen never
gets a page-wide horizontal scrollbar (the table was 449 px wide at 375 px)."""

import re
import types

from optimus import renderer
from optimus.tests.test_background_job_label import _action_row
from optimus.tests.test_render_duration_threshold import _doc


def _html(framework=True):
	rows = [_action_row("HTTP Request", "GET /app/todo", path="/app/todo")]
	if framework:
		rows.append(_action_row("HTTP Request", "GET frappe.x", path="frappe.x"))
	return renderer.render_raw(_doc(rows), recordings=[])


def test_every_per_action_table_sits_in_a_scroll_box():
	html = _html()
	tables = re.findall(r"(<div[^>]*>)\s*<table class=\"tbl-clip per-action-table", html)
	assert tables, "no per-action table rendered"
	assert all('class="table-scroll"' in opening for opening in tables)


def test_scroll_box_scrolls_sideways_and_table_keeps_a_readable_width():
	html = _html()
	box = re.search(r"\.table-scroll\s*\{([^}]*)\}", html)
	assert box and "overflow-x: auto" in box.group(1)
	table = re.search(r"table\.per-action-table\s*\{([^}]*)\}", html)
	assert table and "min-width" in table.group(1)
