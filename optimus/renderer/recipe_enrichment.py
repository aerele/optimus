# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Render-time glue for the deterministic recipes.

Writes ``index_recipes`` advice into slots the report template already renders (a
finding's ``technical_detail.fix_hint`` prose and ``suggested_ddl`` code block, a table
card's ``recommended_index``), hides AI output the report no longer shows (index-family
and Framework N+1 ``llm_fix``, table ``ai_index``, the stored fix of a gated Hot Line)
and fails closed: an index-family finding never shows the analyzer's raw ``ALTER TABLE``
/ ``CREATE INDEX`` text. Stored JSON is never modified.

``make_evidence_lookup`` reads DocField flags, the DocType's app, real column types and
existing indexes once per table per render (owner decision A2). It and
``make_refresh_check`` (Optimus Settings, through ``ai_fix``, imported lazily) are the
only functions that touch Frappe.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from optimus import ai_grounding
from optimus.analyzers.base import INDEX_FINDING_TYPES
from optimus.dbdialect import get_dialect
from optimus.renderer import index_recipes
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

	doctype = index_recipes.doctype_of(table)
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


def _with_note(existing, note: str) -> str:
	"""``existing`` text (a list is joined) with ``note`` appended once: running the
	recipes twice leaves one copy."""
	if isinstance(existing, list):
		existing = " ".join(str(item) for item in existing if item)
	text = str(existing or "").strip()
	return text if note in text else f"{text} {note}".strip()


def make_refresh_check() -> Callable[[dict], bool]:
	"""``check(finding)``: True when Refresh AI suggestions would regenerate the
	finding's suggestion (AI fixes are available, the finding passes the eligibility
	gate and its type is not excluded). Only then does the outdated footer name it."""
	from optimus import ai_fix

	available = best_effort(lambda: ai_fix.is_available(section="findings"), False)

	# A distinctive name: test_ai_log_audit.py matches AI helpers by callee name.
	def _refreshable(finding: dict) -> bool:
		if not available:
			return False
		if ai_fix.is_finding_type_excluded(finding.get("finding_type")):
			return False
		return ai_fix.llm_gate_note(finding) is None

	return _refreshable


def count_ai_tokens(findings: list[dict], tables: list[dict]) -> int:
	"""Tokens every stored AI output of this session cost. The renderer reads them BEFORE
	it drops findings (no callsite, Function Not Invoked, Ignored Apps) or hides retired
	output (index-family, Framework N+1, gated Hot Line, table ``ai_index``), because
	those tokens were spent all the same."""
	total = 0
	for item, key in [*((f, "llm_fix") for f in findings or []), *((t, "ai_index") for t in tables or [])]:
		blob = item.get(key) if isinstance(item, dict) else None
		tokens = blob.get("tokens") if isinstance(blob, dict) else None
		if isinstance(tokens, dict):
			try:
				total += int(tokens.get("total_tokens") or 0)
			except (TypeError, ValueError):
				continue
	return total


def apply_finding_recipes(
	findings: list[dict],
	*,
	evidence_lookup: Callable[[str], TableEvidence | None],
	tracked_apps: tuple[str, ...] = (),
	installed_apps: frozenset[str] | None = None,
) -> None:
	"""Fill each render dict's recipe slots in place (see the module docstring)."""
	scope = tuple(tracked_apps or ())
	for f in findings or []:
		if not isinstance(f, dict):
			continue
		detail = f.get("technical_detail")
		if not isinstance(detail, dict):
			continue
		ftype = f.get("finding_type") or ""
		if ftype in INDEX_FINDING_TYPES:
			f["llm_fix"] = None
			advice = best_effort(
				lambda: index_recipes.advise_finding(f, evidence_lookup=evidence_lookup, tracked_apps=scope), None,
			)
			detail.pop("suggested_ddl", None)
			if advice is not None:
				detail["fix_hint"] = index_recipes.finding_text(advice)
				if advice.code:
					detail["suggested_ddl"] = advice.code
		elif ftype == "Redundant Call":
			if f.get("llm_fix") and ai_grounding.analyzed_before_callsite_fix(f):
				detail["validation_note"] = _with_note(
					detail.get("validation_note"), ai_grounding.UNSTAMPED_REDUNDANT_CALL_NOTE,
				)
		elif ftype == "Framework N+1":
			f["llm_fix"] = None
			detail["fix_hint"] = _with_note(detail.get("fix_hint"), ai_grounding.FRAMEWORK_N1_NOTE)
		elif ftype == "Hot Line":
			note = best_effort(
				lambda: ai_grounding.hot_line_gate(f, tracked_apps=scope, installed_apps=installed_apps), None,
			)
			if note:
				detail["fix_hint"] = note
				f["llm_fix"] = None


def apply_table_recipes(
	table_breakdown: list[dict],
	*,
	evidence_lookup: Callable[[str], TableEvidence | None],
	tracked_apps: tuple[str, ...] = (),
) -> None:
	"""Drop ``ai_index`` from every table entry and run each card's
	``recommended_index`` through the same advisor as the findings. The recommendation
	is kept, single column included (P6), and gains ``route``, ``route_note`` (the
	card's note), ``code`` and ``index_name``; it is dropped only when the advisor has
	nothing to say (no DocType table, no usable column)."""
	scope = tuple(tracked_apps or ())
	for t in table_breakdown or []:
		if not isinstance(t, dict):
			continue
		t.pop("ai_index", None)
		rec = t.get("recommended_index")
		if not isinstance(rec, dict) or not rec.get("columns"):
			continue
		advice = best_effort(
			lambda: index_recipes.advise_table(
				t.get("table") or "", list(rec.get("columns") or []), evidence_lookup=evidence_lookup,
				tracked_apps=scope,
			),
			None,
		)
		if advice is None:
			t.pop("recommended_index", None)
			continue
		rec["columns"] = list(advice.columns)
		rec["route"] = advice.route
		rec["route_note"] = index_recipes.card_note(advice)
		rec["code"] = advice.code
		rec["index_name"] = (advice.entry or {}).get("index_name")


def mark_outdated_ai_fixes(
	findings: list[dict], *, current_version: int | None = None, refresh_check: Callable[[dict], bool] | None = None,
) -> None:
	"""On every rendered AI suggestion set, in place, ``llm_fix["outdated"]`` (made with
	an older prompt version than ``current_version``, default
	``ai_prompts.PROMPT_VERSION``, or with none recorded) and ``llm_fix["refreshable"]``
	(``refresh_check(finding)`` says Refresh AI suggestions would redo it)."""
	if current_version is None:
		from optimus.ai_prompts import PROMPT_VERSION as current_version
	for f in findings or []:
		fix = f.get("llm_fix") if isinstance(f, dict) else None
		if not isinstance(fix, dict):
			continue
		version = fix.get("prompt_version")
		current = isinstance(version, int) and not isinstance(version, bool) and version >= current_version
		fix["outdated"] = not current
		fix["refreshable"] = bool(
			not current and refresh_check is not None and best_effort(lambda: refresh_check(f), False)
		)
