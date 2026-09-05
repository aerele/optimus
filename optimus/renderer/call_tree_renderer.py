# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Call-tree panel renderer: the hierarchical "where did wall-clock time go"
section of the report.

Sourced from the top actions' ``call_tree_json`` (built by the analyzer);
rendered as nested ``<details>`` with an auto-open breadcrumb down to the
user-app's first hot frame and depth-capped expanders past
``_CALL_TREE_MAX_DEPTH``. Synthetic placeholder nodes (``[other: N frames]``,
``[N more frames omitted]``) and ``<sql>`` query leaves are dropped: this panel
shows only the Python hierarchy (queries live in their own sections).
"""

from __future__ import annotations

import json
import re

# Depth caps for the call-tree panel. The default cap is what the user
# sees without clicking; the hard cap is the absolute runaway-protection
# ceiling beyond which children are silently truncated.
_CALL_TREE_MAX_DEPTH = 12
_CALL_TREE_HARD_CAP = 64
# v0.13: the panel renders the top-N slowest actions' call trees, not just the
# single slowest so a flat #1 action (e.g. an RQ job that just loops one
# function) doesn't hide the deep, structurally-rich trees of the next-slowest
# actions in the same session.
_CALL_TREE_MAX_ACTIONS = 3

_CT_OTHER_RE = re.compile(
	r"^\[(?:other: \d+ frames?|\d+ more frames? omitted)\]$"
)


def _e(text: object) -> str:
	"""HTML-escape. Local copy of ``_internal._e`` to avoid a circular import."""
	import html as _html

	return _html.escape("" if text is None else str(text))


def _ct_is_other_frame(fn) -> bool:
	"""True for a synthetic collapse node (``[other: N frames]`` or ``[N more
	frames omitted]``). These carry no callsite and are dropped from the tree."""
	return bool(_CT_OTHER_RE.match((fn or "").strip()))


def _ct_is_sql_leaf(node) -> bool:
	"""True for a ``<sql>`` query leaf frame. Dropped from the call-tree display
	(the tree shows only the Python hierarchy; queries live in their own sections)."""
	cn = node or {}
	return cn.get("function") == "<sql>" and not cn.get("children")


def _ct_is_user_frame(node) -> bool:
	"""A real user-app python frame (not framework, not a synthetic
	``<sql>`` / ``[other]`` / ``<root>`` node). Used to auto-open the tree
	down to the first user-app frame."""
	fn = node.get("function") or ""
	if not fn or fn.startswith("<") or fn.startswith("["):
		return False
	fname = (node.get("filename") or "").replace("\\", "/")
	app = fname.split("/", 1)[0] if fname else ""
	if not app:
		return False
	try:
		from optimus.analyzers.base import FRAMEWORK_APPS
	except Exception:
		FRAMEWORK_APPS = frozenset()
	return app not in FRAMEWORK_APPS


def _render_call_tree_node(node, parent_ms, depth=0, unlimited=False, breadcrumb=True):
	"""Recursively emit nested ``<details>`` for a single call_tree node.

	Auto-opens the hottest path down to the first user-app frame (``breadcrumb``);
	deeper branches start collapsed. Past ``_CALL_TREE_MAX_DEPTH`` the rest of the
	subtree is wrapped in a click-to-expand ``<details>`` (with ``unlimited=True``
	so expanders don't nest at every level); ``_CALL_TREE_HARD_CAP`` is the
	absolute runaway ceiling.
	"""
	if not isinstance(node, dict):
		return ""
	fn = node.get("function") or "<?>"
	# v0.7.x: drop synthetic "[other: N frames]" collapse nodes entirely (user
	# request accepts that a branch's visible children may not sum to its total).
	if _ct_is_other_frame(fn):
		return ""
	file = node.get("filename") or ""
	lineno = node.get("lineno") or ""
	cum_ms = float(node.get("cumulative_ms") or 0)
	self_ms = float(node.get("self_ms") or 0)
	children = node.get("children") or []

	pct = (cum_ms / parent_ms * 100.0) if parent_ms else 0.0
	cls = "call-tree-node"
	if parent_ms and cum_ms / parent_ms >= 0.5:
		cls += " call-tree-hot"

	# v0.7.x: auto-open the hottest path down to the first user-app frame so the
	# tree "opens at" the user's code; collapse below it.
	is_user = _ct_is_user_frame(node)
	open_attr = " open" if breadcrumb else ""
	meta_lineno = f":{lineno}" if lineno else ""
	pct_label = f" &middot; {pct:.0f}%" if parent_ms else ""
	self_label = ""
	if self_ms and cum_ms - self_ms > 1:
		self_label = f" &middot; self {self_ms:.0f}ms"

	out = [
		f'<details class="{cls}"{open_attr}>',
		'<summary>',
		f'<span class="frame-name">{_e(fn)}</span>',
		f'<span class="frame-meta">{_e(file)}{meta_lineno} &middot; '
		f'{cum_ms:.0f}ms{pct_label}{self_label}</span>',
		'</summary>',
	]
	if children:
		# Drop [other: N frames] synthetic nodes AND <sql> query leaves (the
		# call tree is the Python hierarchy; per-query rows belong in the
		# Slowest-queries / DB-tables sections), then order hottest-first.
		main = sorted(
			[
				c for c in children
				if not _ct_is_other_frame((c or {}).get("function"))
				and not _ct_is_sql_leaf(c)
			],
			key=lambda c: float((c or {}).get("cumulative_ms") or 0),
			reverse=True,
		)

		within_default = unlimited or depth < _CALL_TREE_MAX_DEPTH
		within_hard = depth < _CALL_TREE_HARD_CAP

		if within_default and within_hard:
			out.append('<div class="call-tree-children">')
			for idx, c in enumerate(main):
				# Continue the auto-open breadcrumb down the single hottest path
				# until we reach a user-app frame; collapse once we're there.
				child_bc = (
					breadcrumb and not is_user and idx == 0
					and depth < _CALL_TREE_MAX_DEPTH
				)
				out.append(_render_call_tree_node(
					c, cum_ms, depth + 1, unlimited, breadcrumb=child_bc,
				))
			out.append('</div>')
		elif within_hard:
			# Past default cap click-to-expand the rest of the
			# subtree. ``unlimited=True`` prevents further wrapping
			# at every nested level.
			out.append(
				'<div class="call-tree-children call-tree-deeper">'
				'<details class="call-tree-deeper-toggle">'
				'<summary>'
				f'<em>show {len(main)} deeper frame(s) &middot; '
				f'depth {depth + 1}+</em>'
				'</summary>'
				'<div class="call-tree-children">'
			)
			for c in main:
				out.append(_render_call_tree_node(
					c, cum_ms, depth + 1, unlimited=True, breadcrumb=False,
				))
			out.append('</div></details></div>')
		else:
			# depth >= HARD_CAP; absolute truncation as safety net.
			out.append(
				'<div class="call-tree-children call-tree-truncated">'
				f'<em>... {len(children)} child frame(s) hidden '
				f'(hard cap {_CALL_TREE_HARD_CAP} reached) ...</em>'
				'</div>'
			)
	out.append('</details>')
	return "".join(out)


def _render_one_call_tree(top):
	"""Render the ``<div class="call-tree">`` block for a single action dict
	(``call_tree_json`` + ``duration_ms`` + ``action_label``). Returns the
	tree HTML, or "" when the action has no renderable Python frames (empty
	tree, or every root child is an ``[other]`` / ``<sql>`` synthetic node).
	"""
	try:
		tree = json.loads(top.get("call_tree_json") or "{}")
	except Exception:
		return ""
	if not isinstance(tree, dict):
		return ""
	root_children = tree.get("children") or []
	if not root_children:
		return ""

	total_ms = float(tree.get("cumulative_ms") or 0) or float(top.get("duration_ms") or 0)
	root_children_sorted = sorted(
		root_children,
		key=lambda c: float((c or {}).get("cumulative_ms") or 0),
		reverse=True,
	)
	# The profiler attributes SQL queries as root-level siblings of the entry
	# frame, so the panel renders them directly (bypassing the per-node child
	# loop). Drop [other] nodes and <sql> query leaves here too.
	nodes = []
	for c in root_children_sorted:
		cn = c or {}
		if _ct_is_other_frame(cn.get("function")) or _ct_is_sql_leaf(cn):
			continue
		nodes.append(_render_call_tree_node(c, total_ms, depth=0))
	if not nodes:
		return ""
	return '<div class="call-tree">' + "".join(nodes) + '</div>'


def _render_call_tree_panel(actions):
	"""Render the call-tree panel for the top-N slowest actions (up to
	``_CALL_TREE_MAX_ACTIONS``) that carry a ``call_tree_json``, each as its own
	labeled sub-tree. Empty string when no action carries a renderable tree.
	"""
	if not actions:
		return ""
	candidates = [a for a in actions if isinstance(a, dict) and a.get("call_tree_json")]
	if not candidates:
		return ""
	ranked = sorted(
		candidates, key=lambda a: float(a.get("duration_ms") or 0), reverse=True
	)

	rendered = []  # (action_label, total_ms, tree_html) for actions that render
	for top in ranked:
		if len(rendered) >= _CALL_TREE_MAX_ACTIONS:
			break
		tree_html = _render_one_call_tree(top)
		if not tree_html:
			continue
		total_ms = float(top.get("duration_ms") or 0)
		rendered.append((top.get("action_label") or "", total_ms, tree_html))

	if not rendered:
		return ""

	multi = len(rendered) > 1
	if multi:
		heading = "Call trees (top actions)"
		tag = f"{len(rendered)} slowest"
		intro = (
			"Hierarchical breakdown of where wall-clock time went inside the "
			f"{len(rendered)} slowest actions. Each tree auto-opens down to your "
			"app's first hot frame; click any frame to expand or collapse it. "
			"Numbers are cumulative time (including children) and percentage of the "
			"parent. Branches consuming &ge;50% of their parent are highlighted as hot."
		)
	else:
		heading = "Call tree (top action)"
		tag = rendered[0][0]
		intro = (
			"Hierarchical breakdown of where wall-clock time went inside the slowest "
			"action. The tree auto-opens down to your app's first hot frame; click any "
			"frame to expand or collapse it. Numbers are cumulative time (including "
			"children) and percentage of the parent. Branches consuming &ge;50% of their "
			"parent are highlighted as hot."
		)

	parts = [
		'<section class="section" id="call-tree">',
		'<div class="section-head">',
		f'<h2>{heading}</h2>',
		f'<span class="section-tag">{_e(tag)}</span>',
		'</div>',
		f'<p class="section-intro">{intro}</p>',
	]
	if multi:
		for rank, (label, total_ms, tree_html) in enumerate(rendered, start=1):
			parts.append('<div class="call-tree-action">')
			parts.append(
				'<div class="call-tree-action-head">'
				f'<span class="call-tree-action-rank">#{rank}</span>'
				f'<span class="call-tree-action-label">{_e(label)}</span>'
				f'<span class="call-tree-action-meta">{total_ms:.0f}ms</span>'
				'</div>'
			)
			parts.append(tree_html)
			parts.append('</div>')
	else:
		parts.append(rendered[0][2])
	parts.append('</section>')
	return "".join(parts)
