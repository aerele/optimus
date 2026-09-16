# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Pure data-transform visualization helpers for the renderer.

Public helpers passed into the Jinja context: ``build_donut_data`` (breakdown
JSON into (label, ms, color) tuples), ``build_donut_svg`` (inline-SVG donut for
PDF mode), ``build_hot_frames_table`` (leaderboard rows) and
``redact_frame_name`` (tree-node display). No Frappe dependency: pure dicts in,
HTML strings / list-of-dicts out.
"""

from __future__ import annotations

import math

# Donut color palette (8 colors; rolls over for more).
_DONUT_COLORS = [
	"#ff6b6b", "#4ecdc4", "#ffd93d", "#6c5ce7",
	"#a8e6cf", "#ff8b94", "#95e1d3", "#ffaaa5",
]


def redact_frame_name(node: dict) -> str:
	"""Build a tree node's display name as ``function (shortfile:lineno)``."""
	if not isinstance(node, dict):
		return "<unknown>"

	function = node.get("function") or "<unknown>"
	filename = node.get("filename") or ""
	lineno = node.get("lineno") or 0

	short_file = filename.split("/")[-1] if filename else "?"
	return f"{function} ({short_file}:{lineno})"


def build_donut_data(breakdown: dict) -> list:
	"""Convert session_time_breakdown_json into ordered (label, ms, color)
	tuples. Slices under 1ms are hidden as noise.
	"""
	if not breakdown:
		return []

	DONUT_DISPLAY_MIN_MS = 1.0

	slices = []
	sql_ms = breakdown.get("sql_ms", 0)
	if sql_ms >= DONUT_DISPLAY_MIN_MS:
		slices.append(("SQL", sql_ms, _DONUT_COLORS[0]))

	by_app = breakdown.get("by_app", {})
	for app, ms in by_app.items():
		if ms < DONUT_DISPLAY_MIN_MS:
			continue
		color = _DONUT_COLORS[(len(slices)) % len(_DONUT_COLORS)]
		slices.append((f"Python ({app})", ms, color))

	return slices


def build_donut_svg(slices: list) -> str:
	"""Render the donut as an inline SVG pie for PDF mode (wkhtmltopdf does not
	handle CSS conic-gradient reliably). Each slice becomes a <path> arc.
	"""
	if not slices:
		return ""

	total = sum(s[1] for s in slices) or 1
	cx, cy, r = 80, 80, 70
	parts = ['<svg width="160" height="160" xmlns="http://www.w3.org/2000/svg">']
	angle_start = -math.pi / 2  # start at 12 o'clock

	for _label, ms, color in slices:
		fraction = ms / total
		angle_end = angle_start + fraction * 2 * math.pi
		x1 = cx + r * math.cos(angle_start)
		y1 = cy + r * math.sin(angle_start)
		x2 = cx + r * math.cos(angle_end)
		y2 = cy + r * math.sin(angle_end)
		large_arc = 1 if fraction > 0.5 else 0
		path = (
			f'<path d="M {cx} {cy} L {x1:.1f} {y1:.1f} '
			f'A {r} {r} 0 {large_arc} 1 {x2:.1f} {y2:.1f} Z" '
			f'fill="{color}" stroke="#fff" stroke-width="1"/>'
		)
		parts.append(path)
		angle_start = angle_end

	parts.append("</svg>")
	return "".join(parts)


def build_hot_frames_table(rows: list, is_hot: bool = False) -> list:
	"""Build the hot-frames leaderboard rows.

	``is_hot`` marks user-code rows (True) for the tinted CSS variant and
	selects the time metric: user frames display the self-sum ``total_ms``,
	framework frames (is_hot=False) display ``total_cumulative_ms`` (their
	self-time rounds to 0) and are re-sorted by it so the column reads by impact.
	"""
	out = []
	for row in rows or []:
		# The hot-frame key already encodes "<short_path>::<func>" use it
		# directly. Routing through redact_frame_name appended a bogus "(?:0)"
		# (placeholder file "?" + lineno 0, since no file/line is known here).
		display = row.get("function") or "<unknown>"
		if is_hot:
			display_ms = row.get("total_ms", 0)
		else:
			display_ms = row.get(
				"total_cumulative_ms", row.get("total_ms", 0)
			)
		out.append({
			"display_name": display,
			"total_ms": display_ms,
			"occurrences": row.get("occurrences", 0),
			"distinct_actions": row.get("distinct_actions", 0),
			"action_refs": row.get("action_refs", []),
			"is_hot": is_hot,
		})
	# Framework variant ranks by the displayed (cumulative) metric
	# the aggregator's outer sort was by self_ms, which produces
	# all-zero ties on framework rows.
	if not is_hot:
		out.sort(key=lambda r: r.get("total_ms", 0), reverse=True)
	return out
