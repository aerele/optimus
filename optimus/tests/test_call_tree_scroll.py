# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""CSS guards for the call-tree horizontal-scroll fix (report.html)."""

import os
import re


def _read_template() -> str:
	here = os.path.dirname(__file__)
	tpath = os.path.join(here, "..", "templates", "report.html")
	with open(tpath, encoding="utf-8") as f:
		return f.read()


def _rule_body(tpl: str, selector_regex: str) -> str:
	"""Body of the first CSS rule matching selector_regex, comments stripped."""
	m = re.search(selector_regex + r"\s*\{", tpl)
	if not m:
		return ""
	body = tpl[m.end() : tpl.index("}", m.end())]
	return re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)


class TestCallTreeScrollCSS:
	def test_call_tree_container_scrolls_horizontally(self):
		"""`.call-tree` sets overflow-x: auto so a deep tree scrolls."""
		tpl = _read_template()
		body = _rule_body(tpl, r"\n\s*\.call-tree")
		assert re.search(r"overflow-x:\s*auto", body), (
			".call-tree must declare overflow-x: auto so a deep tree scrolls "
			f"horizontally instead of clipping. Found body: {body.strip()!r}"
		)

	def test_call_tree_rows_take_natural_width(self):
		"""Rows use width:max-content + min-width:100% so paths are not ellipsized."""
		tpl = _read_template()
		body = _rule_body(tpl, r"details\.call-tree-node > summary")
		assert re.search(r"width:\s*max-content", body), (
			"summary must set width: max-content so deep rows keep their full "
			f"width. Found body: {body.strip()!r}"
		)
		assert re.search(r"min-width:\s*100%", body), (
			"summary must set min-width: 100% so shallow rows still fill the "
			f"container. Found body: {body.strip()!r}"
		)

	def test_print_frees_overflow_on_both_axes(self):
		"""Print frees overflow on both axes; a visible/auto pair coerces to auto and clips to page 1."""
		tpl = _read_template()
		blocks = re.findall(r"@media print\s*\{.*?\n\s{2}\}", tpl, re.DOTALL)
		ct_block = next(
			(
				b for b in blocks
				if re.search(r"\.call-tree\s*\{[^}]*overflow", b)
			),
			None,
		)
		assert ct_block, "no @media print block sets .call-tree overflow"
		rule = re.search(r"\.call-tree\s*\{([^}]*)\}", ct_block).group(1)
		frees_both = "overflow: visible" in rule or (
			"overflow-x: visible" in rule and "overflow-y: visible" in rule
		)
		assert frees_both, (
			"print must free .call-tree overflow on both axes (overflow: visible, "
			"or overflow-x + overflow-y visible). A visible/auto pair is coerced "
			f"back to auto and clips the tree to the first PDF page. Found: {rule.strip()!r}"
		)

	def test_print_fits_rows(self):
		"""Print resets rows to width: auto so the meta ellipsizes instead of running off-page."""
		tpl = _read_template()
		blocks = re.findall(r"@media print\s*\{.*?\n\s{2}\}", tpl, re.DOTALL)
		ct_block = next(
			(b for b in blocks if re.search(r"\.call-tree\s*\{[^}]*overflow", b)),
			None,
		)
		assert ct_block, "call-tree @media print block not found"
		assert re.search(
			r"details\.call-tree-node > summary\s*\{[^}]*width:\s*auto", ct_block
		), (
			"the print block must reset the summary rows to width: auto so they "
			"fit the page width and the meta ellipsizes, instead of max-content "
			"rows running off the page edge and clipping the trailing timings"
		)

	def test_rows_stretch_to_full_scroll_width_for_highlight(self):
		"""Nodes/children grow to max-content so a row highlight fills the full scrolled width."""
		tpl = _read_template()
		node_body = _rule_body(tpl, r"details\.call-tree-node")
		assert re.search(r"min-width:\s*max-content", node_body), (
			"details.call-tree-node must set min-width: max-content so row "
			"highlights fill the full scrolled width. "
			f"Found: {node_body.strip()!r}"
		)
		children_body = _rule_body(tpl, r"\.call-tree-children")
		assert re.search(r"min-width:\s*max-content", children_body), (
			"nested .call-tree-children must also set min-width: max-content so "
			f"deep rows stretch to the scroll width. Found: {children_body.strip()!r}"
		)

	def test_print_resets_row_min_width(self):
		"""Print resets node/children min-width to 0 so the tree fits the page."""
		tpl = _read_template()
		blocks = re.findall(r"@media print\s*\{.*?\n\s{2}\}", tpl, re.DOTALL)
		ct_block = next(
			(b for b in blocks if re.search(r"\.call-tree\s*\{[^}]*overflow", b)),
			None,
		)
		assert ct_block, "call-tree @media print block not found"
		assert re.search(
			r"details\.call-tree-node,\s*\.call-tree-children\s*\{[^}]*min-width:\s*0",
			ct_block,
		), (
			"the print block must reset details.call-tree-node / .call-tree-children "
			"min-width to 0 so the tree fits the page instead of keeping the "
			"on-screen max-content width"
		)


class TestCallTreeStillRenders:
	"""The CSS-only change must not break call-tree rendering."""

	def test_deep_tree_renders_with_call_tree_container(self):
		import json

		from optimus.renderer import call_tree_renderer

		# Build a nested tree deep enough to have previously clipped. Each node
		# is a user-app frame so the hottest-path auto-open walks down it.
		def frame(depth):
			return {
				"function": f"_check_user_exists_{depth}",
				"filename": "ugly_code/python/common.py",
				"lineno": 14 + depth,
				"cumulative_ms": 34091.0 - depth,
				"self_ms": 1.0,
				"children": [],
			}

		top_frame = frame(0)
		cur = top_frame
		for d in range(1, 12):
			child = frame(d)
			cur["children"] = [child]
			cur = child

		tree = {"cumulative_ms": 35497.0, "children": [top_frame]}
		html = call_tree_renderer._render_one_call_tree(
			{"call_tree_json": json.dumps(tree), "duration_ms": 35497.0},
			threshold_ms=1.0,
		)
		assert 'class="call-tree"' in html
		assert "call-tree-node" in html
		# Frames across the depth range render (collapsed <details> still keep
		# their children in the markup), so both a shallow and a deeper frame
		# are present rather than dropped.
		assert "_check_user_exists_0" in html
		assert "_check_user_exists_10" in html
