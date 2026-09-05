# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Finding-enrichment helpers for the report renderer (extracted from
``_internal.py``): group findings by root cause, normalize callsite shapes,
walk pyinstrument trees to attach drill-down chains, build the per-finding
render dict and resolve or expand source snippets. Sibling modules are
imported lazily to avoid renderer import cycles.
"""

from __future__ import annotations

import json
import re

# v0.7.x: severity rank for picking the dominant finding within a
# root-cause group. Lower number = higher severity. Mirrors
# ``SEVERITY_ORDER`` in ``analyzers.base`` but lives here so the
# renderer doesn't drag an analyzer import into hot paths.
_GROUPING_SEVERITY_RANK = {"High": 0, "Medium": 1, "Low": 2}


def _root_cause_key(finding: dict) -> tuple | None:
	"""Return a ``(basename, function)`` tuple for the finding's deepest
	user-code anchor, or ``None`` if there's nothing to group on.

	Uses the ``drilldown_chain``'s last entry (deepest frame) when present,
	else the callsite's own ``(filename, function)``. Matched on the basename
	so dev-vs-deploy path differences don't fragment groups. ``None`` for
	findings with no usable callsite (e.g. infra observations), which don't
	group.
	"""
	import os as _os

	detail = finding.get("technical_detail") or {}
	if not isinstance(detail, dict):
		return None

	chain = detail.get("drilldown_chain") or []
	if isinstance(chain, list) and chain:
		leaf = chain[-1]
		if isinstance(leaf, dict):
			leaf_file = leaf.get("filename") or ""
			leaf_fn = leaf.get("function") or ""
			if leaf_file and leaf_fn:
				return (_os.path.basename(leaf_file), leaf_fn)

	callsite = detail.get("callsite") or {}
	if not isinstance(callsite, dict):
		return None
	fname = callsite.get("filename") or ""
	fn_name = callsite.get("function") or ""
	if not fname or not fn_name:
		return None
	return (_os.path.basename(fname), fn_name)


def _group_findings_by_root_cause(findings: list[dict]) -> list[dict]:
	"""Collapse findings sharing a ``(file, function)`` root-cause anchor into
	one primary card, the rest attached as ``sub_findings``.

	Returns a NEW list: the primary per group (chosen by severity then
	``estimated_impact_ms``, higher wins) with sorted ``sub_findings``
	attached, grouped non-primaries dropped, ungrouped findings passed through
	as-is.
	"""
	if not findings:
		return findings

	def _rank(f: dict) -> tuple:
		sev_rank = _GROUPING_SEVERITY_RANK.get(f.get("severity") or "Low", 2)
		impact = -(f.get("estimated_impact_ms") or 0)
		return (sev_rank, impact)

	# Bucket by root-cause key. None-key findings stream through as
	# singletons (no grouping).
	groups: dict[tuple, list[dict]] = {}
	passthrough: list[dict] = []
	for f in findings:
		key = _root_cause_key(f)
		if key is None:
			passthrough.append(f)
			continue
		groups.setdefault(key, []).append(f)

	result: list[dict] = []
	# Walk the input list in order; emit the primary at the first
	# position its key appears at. Skip already-emitted keys and the
	# None-key findings (handled below).
	seen_keys: set[tuple] = set()
	for f in findings:
		key = _root_cause_key(f)
		if key is None:
			continue
		if key in seen_keys:
			continue
		seen_keys.add(key)
		bucket = groups[key]
		if len(bucket) <= 1:
			result.append(bucket[0])
			continue
		ordered = sorted(bucket, key=_rank)
		primary = ordered[0]
		subs = ordered[1:]
		# Attach a compact, render-time-only sub_findings list onto
		# the primary. The template reads this to render the collapsed
		# `<details>` rows; the sub findings are NOT re-emitted as
		# standalone cards elsewhere in the bucket.
		primary["sub_findings"] = [
			{
				"finding_type": s.get("finding_type") or "",
				"severity": s.get("severity") or "Low",
				"title": s.get("title") or "",
				"customer_description": s.get("customer_description") or "",
				"estimated_impact_ms": s.get("estimated_impact_ms") or 0,
				"affected_count": s.get("affected_count") or 0,
			}
			for s in subs
		]
		result.append(primary)

	result.extend(passthrough)
	return result


def _normalize_callsite(callsite) -> dict | None:
	"""Normalize a callsite (a dict, or a ``"file.py:lineno"`` string from
	``top_queries``) into ``{"filename": str, "lineno": int|None,
	"function": str}``. Returns ``None`` for falsy/unrecognized input so
	callers can short-circuit. The string form is split from the right so
	Windows drive letters survive.
	"""
	if not callsite:
		return None
	if isinstance(callsite, dict):
		return callsite
	if isinstance(callsite, str):
		# Shape: "file.py:lineno" split from the RIGHT so Windows
		# paths ("C:\\foo\\bar.py:12") keep their drive letter.
		filename = callsite
		lineno: int | None = None
		if ":" in callsite:
			head, _, tail = callsite.rpartition(":")
			if tail.isdigit():
				filename = head
				try:
					lineno = int(tail)
				except ValueError:
					lineno = None
		return {"filename": filename, "lineno": lineno, "function": ""}
	# Unknown shape log-worthy but don't crash. Return None so the
	# template skips the Callsite block entirely.
	return None


# ---------------------------------------------------------------------------
# Phase 2 (v0.12.19): drill-down chain attachers
# ---------------------------------------------------------------------------


def _find_node_in_tree(tree: dict, basename: str, function: str) -> dict | None:
	"""Depth-first walk a pyinstrument call tree for the first node matching
	``(basename(filename), function)``. Returns the node or ``None``. Matches
	on basename so bench-relative vs absolute path differences don't matter.
	"""
	if not isinstance(tree, dict):
		return None
	want = (basename or "").strip()
	want_fn = (function or "").strip()
	if not want_fn:
		return None
	stack = [tree]
	while stack:
		node = stack.pop()
		if not isinstance(node, dict):
			continue
		node_file = (node.get("filename") or "").rsplit("/", 1)[-1]
		node_fn = (node.get("function") or "")
		if node_fn == want_fn and (not want or node_file == want):
			return node
		children = node.get("children") or []
		# DFS in-order via reverse-append so leftmost children pop first.
		stack.extend(reversed(children))
	return None


def _walk_drilldown_chain(
	tree: dict,
	callsite: dict,
	tracked_apps: tuple[str, ...] = (),
	max_depth: int = 4,
	signal_floor_pct: float = 10.0,
) -> list[dict]:
	"""Build a drill-down chain below the finding's origin frame: locate the
	origin node in the pyinstrument ``tree`` from the ``callsite``, then follow
	hottest-child links downward.

	Stops when a child is framework code, depth reaches ``max_depth``, no
	children remain, or a child's ``cumulative_ms`` falls below
	``signal_floor_pct`` % of the origin's (drops noisy near-leaf frames).

	Returns one ``{filename, lineno, function, cumulative_ms, pct_of_origin}``
	dict per level below the origin (origin omitted). Malformed input gives ``[]``.
	"""
	# Lazy imports these belong to sibling modules; importing at
	# module-top would create a circular if call_tree_renderer ever
	# grew a dependency on this submodule.
	from optimus.analyzers.base import is_framework_callsite
	from optimus.renderer.call_tree_renderer import _ct_is_other_frame

	if not isinstance(tree, dict) or not isinstance(callsite, dict):
		return []
	filename = callsite.get("filename") or ""
	function = callsite.get("function") or ""
	if not function:
		return []

	# If the finding's own callsite is already in framework code, the chain
	# below would be even further from user-actionable code skip.
	if is_framework_callsite(filename, tracked_apps=tracked_apps or None):
		return []

	origin = _find_node_in_tree(tree, filename.rsplit("/", 1)[-1], function)
	if origin is None:
		return []

	origin_ms = float(origin.get("cumulative_ms") or 0)
	if origin_ms <= 0:
		return []

	floor_ms = origin_ms * (signal_floor_pct / 100.0)
	chain: list[dict] = []
	node = origin
	for _ in range(max(0, max_depth)):
		children = node.get("children") or []
		if not children:
			break
		# Pick the hottest child by cumulative_ms, skipping synthetic
		# "[other: N frames]" collapse nodes (v0.7.x: not shown in chains
		# you can't drill into a collapsed bucket).
		hottest = max(
			(c for c in children
			 if isinstance(c, dict) and not _ct_is_other_frame(c.get("function"))),
			key=lambda c: float(c.get("cumulative_ms") or 0),
			default=None,
		)
		if hottest is None:
			break
		child_ms = float(hottest.get("cumulative_ms") or 0)
		if child_ms < floor_ms:
			break
		child_file = hottest.get("filename") or ""
		if is_framework_callsite(child_file, tracked_apps=tracked_apps or None):
			break
		chain.append({
			"filename": child_file,
			"lineno": int(hottest.get("lineno") or 0),
			"function": hottest.get("function") or "",
			"cumulative_ms": child_ms,
			"pct_of_origin": int(round(child_ms / origin_ms * 100)) if origin_ms else 0,
		})
		node = hottest

	return chain


def _attach_drilldown_chains(findings, actions, tracked_apps: tuple[str, ...] = ()) -> None:
	"""Attach a ``drilldown_chain`` to each finding's ``technical_detail``,
	mutating findings in place. Tree JSON is parsed once per ``action_idx``
	(cached) so several findings on the same action share one deserialise.
	"""
	if not findings or not actions:
		return

	# Index actions by their original ``idx`` so action_ref lookups survive the
	# min_action_duration_ms filter (which preserves idx but reshapes the list).
	actions_by_idx: dict[int, dict] = {}
	for a in actions:
		try:
			actions_by_idx[int(a.get("idx"))] = a
		except (TypeError, ValueError):
			continue

	tree_cache: dict[int, dict] = {}
	for finding in findings:
		detail = finding.get("technical_detail") or {}
		callsite = detail.get("callsite") or {}
		if not callsite.get("function"):
			continue
		ref = finding.get("action_ref")
		if ref in (None, ""):
			continue
		try:
			idx = int(ref)
		except (TypeError, ValueError):
			continue
		action = actions_by_idx.get(idx)
		if not action:
			continue
		if idx not in tree_cache:
			try:
				tree_cache[idx] = json.loads(action.get("call_tree_json") or "{}")
			except (TypeError, ValueError):
				tree_cache[idx] = {}
		tree = tree_cache.get(idx) or {}
		if not tree:
			continue
		chain = _walk_drilldown_chain(tree, callsite, tracked_apps=tracked_apps)
		# v0.7.x: always attach the chain even empty. The template
		# distinguishes "key absent" (no callsite/action/tree, never
		# attempted) from "empty list" (attempted but no eligible
		# user-code descendants) to decide whether to render a "no
		# deeper user-code frame" placeholder in place of the chain.
		detail["drilldown_chain"] = chain
		finding["technical_detail"] = detail


# ---------------------------------------------------------------------------
# Phase 3 (v0.12.26): finding-dict builder + representative-callsite
# attacher + self-time snippet expander + phase-1 callsite retargeter +
# their helpers (markdown-to-html, function-body snippet reader, AST
# call-line finder). The HIGH-coupling subset that was deferred from
# phases 1 + 2 now ships because v0.12.23's source_resolution.py
# extraction supplied the dependency.
# ---------------------------------------------------------------------------

# v0.6.x: SQL "red flag" findings (Missing Index, Full Table Scan, Filesort,
# Temporary Table, Low Filter Ratio) are keyed by (finding_type, table) and
# carry no callsite the offending query is issued from many places. At
# render time we still have the recordings, so we pick a *representative*
# callsite: the hottest user-app frame among the calls whose normalized query
# matches the finding's. Surfaced as "Most-called from:" with a
# "representative callsite" note in the template.
_SQL_REDFLAG_FINDING_TYPES = frozenset({
	"Missing Index", "Full Table Scan", "Filesort", "Temporary Table",
	"Low Filter Ratio",
})


def _find_call_line_in_function_body(
	parent_filename: str,
	parent_def_lineno: int,
	callee_function: str,
	*,
	file_cache: dict | None = None,
) -> int | None:
	"""Return the lineno of the first call to ``callee_function`` inside the
	function whose ``def`` starts at ``parent_def_lineno`` in
	``parent_filename``. ``None`` if the source can't be read or no call is
	found.

	Primary path parses the AST and matches the ``Call`` target against both
	``callee(...)`` and ``obj.callee(...)``; on parse failure it falls back to
	a regex scan of the function body. ``file_cache`` shares reads across
	findings in the same render.
	"""
	# Lazy import to avoid the circular: _internal re-imports from this
	# submodule; importing source.py here is fine (it has no back-ref
	# into us), but we keep the import inside the function for symmetry
	# with the other lazy paths in this module.
	from optimus.renderer.source import _source_lines

	if not parent_filename or not callee_function or not parent_def_lineno:
		return None

	# Shared reader so a Server Script callsite resolves its body from the DB (not a bare open() on the sentinel); files behave as before.
	lines = _source_lines(parent_filename, cache=file_cache)

	if not lines:
		return None
	source = "\n".join(lines)

	# Strategy 1: AST parse.
	try:
		import ast as _ast
		tree = _ast.parse(source, filename=parent_filename)
	except Exception:
		tree = None

	if tree is not None:
		# Find the function def closest to parent_def_lineno (defensive
		# against the parent being a nested function pick by name match
		# AND minimum lineno distance).
		candidates: list[_ast.AST] = []
		for node in _ast.walk(tree):
			if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
				candidates.append(node)
		if candidates:
			# Filter by lineno proximity to the recorded def lineno (within
			# a few lines is usually enough pyinstrument's lineno can
			# be the def or the first executed line of the body).
			best = min(
				candidates,
				key=lambda n: abs((n.lineno or 0) - parent_def_lineno),
			)
			# Walk only inside the chosen function; collect Call linenos
			# whose func name matches.
			matches: list[int] = []
			for sub in _ast.walk(best):
				if not isinstance(sub, _ast.Call):
					continue
				func = sub.func
				name = None
				if isinstance(func, _ast.Name):
					name = func.id
				elif isinstance(func, _ast.Attribute):
					name = func.attr
				if name == callee_function and sub.lineno:
					matches.append(sub.lineno)
			if matches:
				return min(matches)

	# Strategy 2: regex fallback. Scan from parent_def_lineno+1 until
	# a sibling def/class at the parent's indentation level or 200
	# lines, whichever comes first. Stop at first match.
	import re as _re
	call_re = _re.compile(r"\b" + _re.escape(callee_function) + r"\s*\(")
	# Parent's indentation level (number of leading spaces / tabs).
	if not (1 <= parent_def_lineno <= len(lines)):
		return None
	parent_line = lines[parent_def_lineno - 1]
	parent_indent = len(parent_line) - len(parent_line.lstrip())
	# Body must be MORE indented than the parent def.
	sentinel_re = _re.compile(
		r"^(?P<indent>\s*)(?:async\s+def|def|class)\s",
	)
	max_scan = min(len(lines), parent_def_lineno + 200)
	for i in range(parent_def_lineno, max_scan):
		raw = lines[i]
		# Strip trailing # comments naively (string-aware comment
		# stripping is overkill here the worst case is a false
		# negative which falls back to the def-line behavior).
		hash_idx = raw.find("#")
		body = raw if hash_idx < 0 else raw[:hash_idx]
		stripped = body.lstrip()
		if not stripped:
			continue
		current_indent = len(body) - len(stripped)
		# Sibling def/class at parent's level (or shallower) → end of body.
		sentinel_match = sentinel_re.match(body)
		if sentinel_match and current_indent <= parent_indent:
			break
		if current_indent <= parent_indent:
			# We've exited the parent function's block.
			break
		if call_re.search(body):
			return i + 1  # convert 0-based index back to lineno

	return None


def _retarget_phase1_callsites_to_drilldown_leaf(
	findings: list[dict],
	file_cache: dict | None = None,
) -> None:
	"""Re-aim finding callsites at the call expression that invokes the deepest
	user-code frame in their drill-down chain, mutating findings in place. This
	points the snippet at the actionable call line rather than the wrapper's
	entry or the leaf's ``def`` header.

	Skips Hot Line / Function Not Invoked findings and SQL red-flag findings
	with a representative callsite. Resolves the call line via AST, then regex,
	then the leaf's own def lineno.
	"""
	from optimus.renderer.source import _read_source_snippet
	from optimus.renderer.source_resolution import _bench_relative_display

	if not findings:
		return

	for finding in findings:
		ftype = finding.get("finding_type") or ""
		if ftype in {"Hot Line", "Function Not Invoked"}:
			continue
		detail = finding.get("technical_detail")
		if not isinstance(detail, dict):
			continue
		chain = detail.get("drilldown_chain") or []
		if not chain:
			continue
		callsite = detail.get("callsite") or {}
		if callsite.get("is_representative"):
			continue
		fname = callsite.get("filename") or ""
		fn_name = callsite.get("function") or ""
		if not fn_name:
			continue
		leaf = chain[-1] if isinstance(chain[-1], dict) else None
		if not leaf:
			continue
		leaf_function = leaf.get("function") or ""
		leaf_lineno = leaf.get("lineno")
		leaf_filename = leaf.get("filename") or ""
		if not leaf_function or leaf_lineno is None or not leaf_filename:
			continue
		if leaf_function == fn_name:
			continue

		if len(chain) >= 2 and isinstance(chain[-2], dict):
			parent_filename = chain[-2].get("filename") or ""
			parent_lineno = int(chain[-2].get("lineno") or 0)
			parent_function = chain[-2].get("function") or ""
		else:
			parent_filename = fname
			parent_lineno = int(callsite.get("lineno") or 0)
			parent_function = fn_name

		call_lineno = _find_call_line_in_function_body(
			parent_filename, parent_lineno, leaf_function,
			file_cache=file_cache,
		)

		if call_lineno is not None:
			anchor_filename = parent_filename
			anchor_lineno = call_lineno
			anchor_function = parent_function
		else:
			anchor_filename = leaf_filename
			anchor_lineno = leaf_lineno
			anchor_function = leaf_function

		snippet = _read_source_snippet(
			anchor_filename, anchor_lineno, cache=file_cache,
		)

		display_filename = (
			_bench_relative_display(anchor_filename)
			if anchor_filename.startswith("/")
			else anchor_filename
		)
		new_callsite = {
			"filename": display_filename,
			"_abs": (
				anchor_filename
				if anchor_filename.startswith("/")
				else callsite.get("_abs")
			),
			"lineno": anchor_lineno,
			"function": anchor_function,
			"source_snippet": snippet,
			"original_wrapper": {
				"filename": fname,
				"lineno": callsite.get("lineno"),
				"function": fn_name,
			},
			"phase2_lookup_filename": leaf_filename,
			"phase2_lookup_function": leaf_function,
		}
		# If we anchored on a Server Script parent (its body is now readable, so
		# call_lineno resolves and the anchor keeps the ``<serverscript>`` filename),
		# tag it as a Desk link like _finding_to_dict does otherwise the template
		# emits a broken ``vscode://file`` link built from the wrapper's real path.
		if anchor_filename.startswith("<serverscript") or anchor_filename.startswith("<server-script"):
			from optimus.server_script_source import desk_url, extract_script_name

			_scrubbed = extract_script_name(anchor_filename)
			if _scrubbed:
				new_callsite["_abs"] = desk_url(_scrubbed, cache=file_cache)
				new_callsite["_link_kind"] = "desk"
		detail["callsite"] = new_callsite


def _markdown_to_safe_html(text) -> str:
	"""Render Markdown to sanitized HTML for the report (``frappe.utils.markdown``
	+ ``sanitize_html(always_sanitize=True)``). On any failure, falls back to an
	HTML-escaped ``<pre>`` block so the report never renders un-sanitized model
	output. Fenced ``diff`` blocks then get per-line ``dh-add`` / ``dh-del`` /
	``dh-meta`` span wrappers (added only around already-escaped text) for diff
	colouring.
	"""
	from optimus.renderer.syntax import _highlight_diff_html

	raw = "" if text is None else str(text)
	try:
		from frappe.utils import markdown as _md
		from frappe.utils.html_utils import sanitize_html
		return _highlight_diff_html(sanitize_html(_md(raw), always_sanitize=True))
	except Exception:
		import html as _html
		return _highlight_diff_html(
			'<pre style="white-space:pre-wrap;">' + _html.escape(raw) + "</pre>"
		)


def _finding_to_dict(child, file_cache: dict | None = None) -> dict:
	"""Flatten an Optimus Finding child row (parsing its JSON detail blob) into
	the render dict, synthesizing a unified ``callsite`` shape and lazily
	attaching a source snippet when none was persisted. ``file_cache`` is
	shared across findings in a render so one source file is read once.
	"""
	from optimus.renderer.source import _read_source_snippet
	from optimus.renderer.source_resolution import (
		_bench_relative_display,
		_resolve_dotted_to_code,
		_resolve_frame_key_to_callsite,
	)

	try:
		detail = json.loads(child.technical_detail_json or "{}")
	except Exception:
		detail = {}
	# A non-object blob (list / ``null``) would crash the ``detail.get()`` calls
	# below normalize so one bad finding can't take down the render.
	if not isinstance(detail, dict):
		detail = {}

	# Back-compat: backfill impact_scope_label for sessions analyzed before that
	# field shipped, so the card's scope tag agrees with the TL;DR hero on
	# re-render but ONLY for findings that carry a loop-scoped magnitude
	# (loop_count / run_count). A pre-loop-scoping session stored
	# estimated_impact_ms as the cross-request CUMULATIVE total; tagging that
	# 'recoverable' (a loop-scoped claim) would misdescribe the number, so those
	# are left unlabelled rather than mislabelled.
	if (
		child.finding_type == "N+1 Query"
		and "impact_scope_label" not in detail
		and (detail.get("loop_count") is not None or detail.get("run_count") is not None)
	):
		detail["impact_scope_label"] = "recoverable"

	# v0.6.0 Round 2: synthesize callsite from legacy top-level shape
	# when the analyzer didn't wrap it.
	if not detail.get("callsite"):
		fname = detail.get("filename") or detail.get("file")
		lineno = detail.get("lineno")
		if fname and lineno is not None:
			detail["callsite"] = {
				"filename": fname,
				"lineno": lineno,
				"function": detail.get("function") or "",
			}

	# v0.5.3: normalize callsite shape so downstream code can assume dict.
	if "callsite" in detail:
		detail["callsite"] = _normalize_callsite(detail.get("callsite"))

	# v0.6.x: findings that name a function but carry no file:line resolve
	# them so the smoking-gun block can render.
	if not (detail.get("callsite") or {}).get("lineno"):
		_ftype = child.finding_type or ""
		if _ftype == "Repeated Hot Frame" and detail.get("function"):
			_cs = _resolve_frame_key_to_callsite(detail["function"], cache=file_cache)
			if _cs:
				detail["callsite"] = _cs
		elif _ftype == "Function Not Invoked" and detail.get("dotted_path"):
			_resolved = _resolve_dotted_to_code(
				detail["dotted_path"], file_cache=file_cache,
			)
			if _resolved:
				_abs, _ln, _name = _resolved
				detail["callsite"] = {
					"filename": _bench_relative_display(_abs),
					"_abs": _abs,
					"lineno": _ln,
					"function": _name or str(detail["dotted_path"]).rsplit(".", 1)[-1],
					"source_snippet": _read_source_snippet(_abs, _ln, cache=file_cache),
				}

	# Hot Line finding line_content windowing keep the profiled text
	# authoritative for the hot line itself.
	callsite = detail.get("callsite") or {}
	if (
		callsite
		and not callsite.get("source_snippet")
		and detail.get("line_content")
		and callsite.get("lineno") is not None
	):
		hot_ln = callsite["lineno"]
		window = _read_source_snippet(
			callsite.get("filename"), hot_ln, cache=file_cache,
		)
		if window and any(r.get("lineno") == hot_ln for r in window):
			for r in window:
				if r.get("lineno") == hot_ln:
					r["content"] = detail["line_content"]
			callsite["source_snippet"] = window
		else:
			callsite["source_snippet"] = [{
				"lineno": hot_ln,
				"content": detail["line_content"],
			}]
		detail["callsite"] = callsite

	# Lazy snippet read at render time when nothing has been attached yet.
	if (
		callsite
		and callsite.get("filename")
		and callsite.get("lineno") is not None
		and not callsite.get("source_snippet")
	):
		snippet = _read_source_snippet(
			callsite["filename"], callsite["lineno"], cache=file_cache,
		)
		if snippet:
			callsite["source_snippet"] = snippet
			detail["callsite"] = callsite

	# v0.7.x+: Server Script findings carry the synthetic ``<serverscript>``
	# filename. Set ``_abs`` to the Desk URL + tag ``_link_kind = "desk"``.
	if callsite and callsite.get("filename"):
		_fn = str(callsite["filename"])
		if _fn.startswith("<serverscript") or _fn.startswith("<server-script"):
			from optimus.server_script_source import desk_url, extract_script_name

			_scrubbed = extract_script_name(_fn)
			if _scrubbed:
				callsite["_abs"] = desk_url(_scrubbed, cache=file_cache)
				callsite["_link_kind"] = "desk"
				detail["callsite"] = callsite

	# v0.6.0: AI-suggested fix (on-demand; empty until generated).
	llm_fix = None
	try:
		raw_llm = json.loads(getattr(child, "llm_fix_json", None) or "{}")
	except Exception:
		raw_llm = {}
	if isinstance(raw_llm, dict) and (raw_llm.get("suggestion") or "").strip():
		llm_fix = {
			"suggestion_html": _markdown_to_safe_html(raw_llm.get("suggestion")),
			"model": raw_llm.get("model") or "",
			"provider": raw_llm.get("provider") or "",
			"generated_at": raw_llm.get("generated_at") or "",
			"source_available": raw_llm.get("source_available", True),
			# Token usage the provider reported (Aerele managed proxy forwards
			# the real billed count). None when the suggestion predates this or
			# the provider returned no usage.
			"tokens": raw_llm.get("tokens") or None,
		}

	return {
		"finding_type": child.finding_type or "",
		"severity": child.severity or "Low",
		"title": child.title or "",
		"customer_description": child.customer_description or "",
		"estimated_impact_ms": child.estimated_impact_ms or 0,
		"affected_count": child.affected_count or 0,
		"action_ref": child.action_ref or "",
		"technical_detail": detail,
		"llm_fix": llm_fix,
	}


def _attach_representative_callsites(findings, recordings, *, file_cache: dict | None = None) -> None:
	"""Attach a representative ``callsite`` (with ``is_representative``) to SQL
	red-flag findings by matching their normalized query against the recording
	calls and picking the hottest user-app frame. Mutates ``findings`` in
	place; no-op when there are no such findings, no recordings, or no match.
	"""
	from optimus.renderer.source import _read_source_snippet, _resolve_source_path
	from optimus.renderer.source_resolution import _bench_relative_display

	if not findings or not recordings:
		return
	wanted: list[dict] = []
	for f in findings:
		if (f.get("finding_type") or "") not in _SQL_REDFLAG_FINDING_TYPES:
			continue
		detail = f.get("technical_detail") or {}
		if (detail.get("callsite") or {}).get("lineno"):
			continue  # already has one
		nq = (detail.get("normalized_query") or "").strip()
		if not nq:
			continue
		wanted.append({
			"finding": f,
			"nq": nq,
			"table": (detail.get("table") or "").strip(),
			"tally": {},  # (filename, lineno, function) → weight
		})
	if not wanted:
		return

	try:
		from optimus.analyzers.base import walk_callsite
	except Exception:
		return

	for rec in recordings:
		if not isinstance(rec, dict):
			continue
		for call in rec.get("calls") or []:
			if not isinstance(call, dict):
				continue
			cnq = (call.get("normalized_query") or "").strip()
			if not cnq:
				continue
			cquery = call.get("query") or ""
			for w in wanted:
				# Equality or prefix either way (survives truncation), plus
				# the table name must appear in the raw query.
				if not (cnq == w["nq"] or cnq.startswith(w["nq"]) or w["nq"].startswith(cnq)):
					continue
				if w["table"] and w["table"] not in cquery:
					continue
				frame = walk_callsite(call.get("stack"))
				if not frame or not frame.get("filename") or frame.get("lineno") is None:
					continue
				k = (frame.get("filename"), frame.get("lineno"), frame.get("function") or "")
				w["tally"][k] = w["tally"].get(k, 0) + (call.get("duration") or 0) + 1

	for w in wanted:
		if not w["tally"]:
			continue
		(filename, lineno, function), _weight = max(w["tally"].items(), key=lambda kv: kv[1])
		abs_path = _resolve_source_path(filename)
		w["finding"]["technical_detail"]["callsite"] = {
			"filename": _bench_relative_display(abs_path) if abs_path else filename,
			"_abs": abs_path,
			"lineno": lineno,
			"function": function,
			"source_snippet": _read_source_snippet(abs_path or filename, lineno, cache=file_cache),
			"is_representative": True,
		}


def _snippet_row(lines: list[str], idx: int, limit: int) -> dict:
	"""One ``{lineno, content}`` snippet row, truncating an over-long line."""
	content = lines[idx - 1]
	if len(content) > limit:
		content = content[:limit] + "..."
	return {"lineno": idx, "content": content}


# A ``def`` / ``async def`` line (any indent, tab or space).
_DEF_RE = re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]")
# A ``class`` line. A decorator block that reaches a ``class`` before a ``def``
# is a class header, not a function's stop rather than grab a method inside.
_CLASS_RE = re.compile(r"^[ \t]*class[ \t]")
# Cap on how far a decorator block may reach before its ``def`` (stacked and/or
# multi-line decorators). Generous; real whitelisted endpoints use one or two.
_MAX_DECORATOR_BLOCK_LINES = 25


def _find_header_def_line(lines: list[str], ln: int) -> int | None:
	"""Lineno of the ``def`` for the header at ``lines[ln-1]``: ``ln`` itself, or
	(CPython 3.11+, where ``co_firstlineno`` is the first ``@decorator``) scanning
	forward past the decorator block to it. None if ``ln`` is neither def nor
	decorator, a ``class`` is hit first, or no ``def`` within the block cap."""
	if _DEF_RE.match(lines[ln - 1]):
		return ln
	if not lines[ln - 1].lstrip().startswith("@"):
		return None
	end = min(len(lines), ln + _MAX_DECORATOR_BLOCK_LINES)
	for i in range(ln + 1, end + 1):
		row = lines[i - 1]
		if _DEF_RE.match(row):
			return i
		if _CLASS_RE.match(row):
			return None
	return None


def _decorator_through_def_rows(
	filename: str,
	lineno,
	*,
	cache: dict | None = None,
) -> tuple[list[dict] | None, int]:
	"""``(rows, def_lineno)`` spanning a self-time finding's recorded line through
	the function's ``def``: so the card shows the function name, not just a
	decorator (pyinstrument anchors a decorated fn at its first ``@decorator`` on
	CPython 3.11+). ``def_lineno`` anchors the highlight; ``(None, lineno)`` when the
	file can't be read / the line is out of range (caller keeps its snippet)."""
	from optimus.renderer.source import _SNIPPET_TRUNCATE_CHARS, _source_lines

	try:
		ln = int(lineno)
	except (TypeError, ValueError):
		return None, lineno
	if ln <= 0 or not filename:
		return None, lineno
	lines = _source_lines(filename, cache=cache)
	if not lines or ln > len(lines):
		return None, ln

	def_lineno = _find_header_def_line(lines, ln)
	if def_lineno is None:
		def_lineno = ln  # not a header line show only the recorded line.

	limit = _SNIPPET_TRUNCATE_CHARS
	rows = [_snippet_row(lines, i, limit) for i in range(ln, def_lineno + 1)]
	return rows, def_lineno


def _expand_self_time_snippets(findings, *, file_cache: dict | None = None) -> None:
	"""For self-time Slow Hot Path findings with no deeper user-code frame (empty
	``drilldown_chain``), narrow the snippet to the function's signature line and
	flag it ``self_time_no_pinpoint`` (sampling can't pinpoint a single hot line,
	so the card shows just the ``def`` plus a note to run a Line-Level Drilldown).

	Runs after ``_attach_drilldown_chains`` and mutates findings in place; acts
	only on the empty-``drilldown_chain`` case."""
	for finding in findings or []:
		if (finding.get("finding_type") or "") != "Slow Hot Path":
			continue
		detail = finding.get("technical_detail") or {}
		# Only when a chain was attempted AND came back empty (no deeper frame).
		if detail.get("drilldown_chain") != []:
			continue
		callsite = detail.get("callsite") or {}
		fn = callsite.get("filename")
		ln = callsite.get("lineno")
		if not fn or ln is None:
			continue
		# Flag drives the "no single hot line run a Line-Level Drilldown" note.
		callsite["self_time_no_pinpoint"] = True
		# ``ln`` is pyinstrument's frame line; on CPython 3.11+ a decorated
		# function reports its FIRST decorator line (e.g. @frappe.whitelist()),
		# not the ``def``. Show the decorator(s) through the ``def`` signature
		# and anchor the highlight + card file:line on the ``def`` itself.
		rows, def_lineno = _decorator_through_def_rows(fn, ln, cache=file_cache)
		if rows:
			callsite["source_snippet"] = rows
			callsite["lineno"] = def_lineno
		detail["callsite"] = callsite
		finding["technical_detail"] = detail
