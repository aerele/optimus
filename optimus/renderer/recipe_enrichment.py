# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Render-time glue for the deterministic fix recipes.

Writes ``fix_recipes`` output into slots the report template already renders
(a finding's ``technical_detail.fix_hint`` prose and ``suggested_ddl`` code
block, a table card's ``recommended_index.columns``), hides AI output the
report no longer shows (index-family and Framework N+1 ``llm_fix``, table
``ai_index``, the stored fix of a gated Hot Line) and fails closed: an
index-family finding without a recipe loses the analyzer's raw
``ALTER TABLE`` / ``CREATE INDEX`` text. Stored JSON is never modified.

``make_meta_lookup`` is the only function that touches Frappe.
"""

from __future__ import annotations

from collections.abc import Callable

from optimus.renderer import fix_recipes


def _read_meta(
	doctype: str, *, tracked_apps: tuple[str, ...], installed_apps: frozenset[str] | None,
) -> dict | None:
	import frappe

	meta = fix_recipes._best_effort(lambda: frappe.get_meta(doctype), None)
	if meta is None:
		return None
	app = fix_recipes._best_effort(lambda: frappe.get_doctype_app(doctype), "") or ""
	fields: dict[str, dict] = {}
	for df in getattr(meta, "fields", None) or []:
		name = getattr(df, "fieldname", None)
		if name:
			fields[name] = {
				"fieldtype": getattr(df, "fieldtype", "") or "",
				"is_custom_field": bool(getattr(df, "is_custom_field", 0)),
			}
	return {
		"module_app": app,
		"is_own_app": fix_recipes.is_own_app(app, tracked_apps=tracked_apps, installed_apps=installed_apps),
		"is_custom_doctype": bool(getattr(meta, "custom", 0)),
		"fields": fields,
	}


def make_meta_lookup(
	*, tracked_apps: tuple[str, ...] = (), installed_apps: frozenset[str] | None = None,
) -> Callable[[str], dict | None]:
	"""A per-render ``meta_lookup(doctype)`` for ``fix_recipes`` (memoised;
	returns None when the DocType cannot be read; ordinary failures return None; job timeouts propagate)."""
	cache: dict[str, dict | None] = {}
	scope = tuple(tracked_apps or ())

	def lookup(doctype: str) -> dict | None:
		if doctype not in cache:
			cache[doctype] = fix_recipes._best_effort(
				lambda: _read_meta(doctype, tracked_apps=scope, installed_apps=installed_apps), None,
			)
		return cache[doctype]

	return lookup


def apply_finding_recipes(
	findings: list[dict],
	*,
	meta_lookup: Callable[[str], dict | None],
	tracked_apps: tuple[str, ...] = (),
	installed_apps: frozenset[str] | None = None,
) -> None:
	"""Mutate render dicts in place (see the module docstring)."""
	for f in findings or []:
		if not isinstance(f, dict):
			continue
		detail = f.get("technical_detail")
		if not isinstance(detail, dict):
			continue
		ftype = f.get("finding_type") or ""
		if ftype in fix_recipes.INDEX_FINDING_TYPES:
			f["llm_fix"] = None
			recipe = fix_recipes._best_effort(lambda: fix_recipes.index_recipe(f, meta_lookup=meta_lookup), None)
			detail.pop("suggested_ddl", None)
			if recipe:
				detail["fix_hint"] = recipe["text"]
				if recipe.get("code"):
					detail["suggested_ddl"] = recipe["code"]
		elif ftype == "Redundant Call" and fix_recipes.analyzed_before_callsite_fix(f):
			existing = str(detail.get("validation_note") or "").strip()
			detail["validation_note"] = f"{existing} {fix_recipes.PRE_L5_REDUNDANT_CALL_NOTE}".strip()
		elif ftype == "Framework N+1":
			f["llm_fix"] = None
		elif ftype == "Hot Line":
			note = fix_recipes._best_effort(
				lambda: fix_recipes.hot_line_gate(
					f, tracked_apps=tuple(tracked_apps or ()), installed_apps=installed_apps,
				), None,
			)
			if note:
				detail["fix_hint"] = note
				f["llm_fix"] = None


def apply_table_recipes(
	table_breakdown: list[dict], *, meta_lookup: Callable[[str], dict | None],
) -> None:
	"""Drop ``ai_index`` from every table entry and replace each card's
	``recommended_index.columns`` with the durable column list (or drop the
	recommendation so the card falls back to its candidate list)."""
	for t in table_breakdown or []:
		if not isinstance(t, dict):
			continue
		t.pop("ai_index", None)
		rec = t.get("recommended_index")
		if not isinstance(rec, dict) or not rec.get("columns"):
			continue
		cols = fix_recipes._best_effort(
			lambda: fix_recipes.table_card_columns(
				t.get("table") or "", list(rec.get("columns") or []), meta_lookup=meta_lookup,
			), None,
		)
		if cols is None:
			t.pop("recommended_index", None)
		else:
			rec["columns"] = cols


def mark_outdated_ai_fixes(findings: list[dict], *, current_version: int | None = None) -> None:
	"""Set ``llm_fix["outdated"]`` on every rendered AI suggestion: True when it
	was generated with an older prompt version than ``current_version``
	(default ``ai_prompts.PROMPT_VERSION``), or with none recorded (before
	prompt versions existed)."""
	if current_version is None:
		from optimus.ai_prompts import PROMPT_VERSION as current_version
	for f in findings or []:
		fix = f.get("llm_fix") if isinstance(f, dict) else None
		if not isinstance(fix, dict):
			continue
		version = fix.get("prompt_version")
		current = isinstance(version, int) and not isinstance(version, bool) and version >= current_version
		fix["outdated"] = not current
