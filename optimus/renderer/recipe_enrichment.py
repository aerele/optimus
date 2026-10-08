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

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from optimus.dbdialect import get_dialect
from optimus.renderer import fix_recipes
from optimus.safe_call import best_effort


@dataclass(frozen=True)
class FieldEvidence:
	"""One DocField's index-relevant flags (Custom Fields included)."""

	fieldtype: str
	length: int
	search_index: bool
	unique: bool
	is_custom_field: bool


@dataclass(frozen=True)
class IndexEvidence:
	"""One existing index: its name, its columns in key order, and uniqueness."""

	name: str
	columns: tuple[str, ...]
	unique: bool


@dataclass(frozen=True)
class TableEvidence:
	"""What the index advisor may rely on for one ``tab*`` table (owner decision A2):
	DocField flags by fieldname, the DocType's app, the real column types and the
	indexes the database already has."""

	table: str
	doctype: str
	app: str
	is_custom_doctype: bool
	dialect: str
	fields: Mapping[str, FieldEvidence]
	column_types: Mapping[str, str]
	text_columns: frozenset[str]
	unindexable_columns: frozenset[str]
	indexes: tuple[IndexEvidence, ...]


def _int(value) -> int:
	try:
		return int(value or 0)
	except (TypeError, ValueError):
		return 0


def _get_meta_quietly(doctype: str):
	"""``frappe.get_meta(doctype)`` with messages muted, restoring the caller's flag.
	A failure leaves no new ``message_log`` entry behind, so Regenerate Reports never
	shows a red "DocType ... not found" dialog for a table read only to build advice
	(P8; frappe/utils/messages.py:61-63, :114-118)."""
	import frappe

	log = getattr(frappe.local, "message_log", None)
	before = len(log) if isinstance(log, list) else None
	muted = getattr(frappe.flags, "mute_messages", None)
	frappe.flags.mute_messages = True
	ok = False
	try:
		meta = frappe.get_meta(doctype)
		ok = True
	finally:
		frappe.flags.mute_messages = muted
		if not ok and before is not None:
			current = getattr(frappe.local, "message_log", None)
			if isinstance(current, list) and len(current) > before:
				del current[before:]
	return meta


def _read_table_evidence(table: str) -> TableEvidence | None:
	"""Evidence for ``table``, or None when it is not a DocType table, its DocType does
	not exist (checked BEFORE get_meta, P8) or its columns cannot be read."""
	import frappe

	doctype = fix_recipes._doctype_of(table)
	if doctype is None:
		return None
	if not frappe.db.exists("DocType", doctype):
		return None
	meta = _get_meta_quietly(doctype)
	dialect = get_dialect()
	column_types = dict(dialect.column_types(table) or {})
	if not column_types:
		return None
	app = best_effort(lambda: frappe.get_doctype_app(doctype), "") or ""
	fields: dict[str, FieldEvidence] = {}
	for df in getattr(meta, "fields", None) or []:
		name = getattr(df, "fieldname", None)
		if name:
			fields[name] = FieldEvidence(
				fieldtype=str(getattr(df, "fieldtype", "") or ""),
				length=_int(getattr(df, "length", 0)),
				search_index=bool(getattr(df, "search_index", 0)),
				unique=bool(getattr(df, "unique", 0)),
				is_custom_field=bool(getattr(df, "is_custom_field", 0)),
			)
	indexes = tuple(
		IndexEvidence(name=str(ix.name), columns=tuple(ix.columns or ()), unique=bool(ix.unique))
		for ix in dialect.existing_indexes(table) or []
	)
	return TableEvidence(
		table=table,
		doctype=doctype,
		app=str(app),
		is_custom_doctype=bool(getattr(meta, "custom", 0)),
		dialect=str(getattr(dialect, "name", "mariadb")),
		fields=fields,
		column_types=column_types,
		text_columns=frozenset(c for c, t in column_types.items() if dialect.is_text_type(t)),
		unindexable_columns=frozenset(c for c, t in column_types.items() if dialect.unindexable(t)),
		indexes=indexes,
	)


def make_evidence_lookup() -> Callable[[str], TableEvidence | None]:
	"""A per-render ``evidence_lookup(table)``: memoised per table (misses included), so
	each table costs its queries once per render. Ordinary failures give None; an RQ
	job timeout escapes as a fresh instance."""
	cache: dict[str, TableEvidence | None] = {}

	def lookup(table: str) -> TableEvidence | None:
		key = str(table or "").strip().strip("`")
		if key not in cache:
			cache[key] = best_effort(lambda: _read_table_evidence(key), None)
		return cache[key]

	return lookup


def _read_meta(
	doctype: str, *, tracked_apps: tuple[str, ...], installed_apps: frozenset[str] | None,
) -> dict | None:
	import frappe

	meta = best_effort(lambda: frappe.get_meta(doctype), None)
	if meta is None:
		return None
	app = best_effort(lambda: frappe.get_doctype_app(doctype), "") or ""
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
			cache[doctype] = best_effort(
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
			recipe = best_effort(lambda: fix_recipes.index_recipe(f, meta_lookup=meta_lookup), None)
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
			note = best_effort(
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
		cols = best_effort(
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
