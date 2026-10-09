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
existing indexes once per table per render (owner decision A2). It,
``make_refresh_check`` (Optimus Settings, through ``ai_fix``, imported lazily) and
``log_recipe_failures`` (one bench-log line) are the only functions that touch Frappe.

Index advice that raises leaves a neutral note with a next step (``RECIPE_FAILED_HINT`` /
``RECIPE_FAILED_CARD_NOTE``) and is counted, and a Hot Line gate that raises fails
closed (O-I1). ``export_advice`` is the one advice step the report and the export share,
and ``finding_display`` makes an index finding's title and description agree with its
advice in the render dict (stored text is baked at analyze time). Running the recipes
twice leaves the same dicts as running them once.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping

from optimus import ai_grounding
from optimus.analyzers.base import INDEX_FINDING_TYPES
from optimus.dbdialect import get_dialect
from optimus.renderer import index_recipes
from optimus.renderer.index_evidence import FieldEvidence, IndexEvidence, TableEvidence
from optimus.safe_call import InterruptGuard, best_effort, log_error_line


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


class EmptyIndexList(Exception):
	"""A table with columns came back with no index at all. Every DocType table has a
	primary key on name, so the index read failed: the dialect turns an ordinary SQL error
	into an empty list. Advice built on that list would give code for an index that may
	exist already, so the read counts as failed (its name is the log line's error type)."""


_evidence_savepoints = itertools.count()


def _read_table_evidence(table: str) -> TableEvidence | None:
	"""Evidence for ``table``, or None when it is not a DocType table, its DocType does
	not exist (checked BEFORE get_meta, P8) or its columns cannot be read. An index list
	that came back empty raises ``EmptyIndexList``. On Postgres the whole read runs under a
	savepoint: one failed statement there aborts the whole transaction, so a failed
	``exists``, ``get_meta`` or catalog read rolls back to the savepoint and the rest of
	the render can still query. A job timeout raised while rolling back is raised again,
	fresh."""
	if index_recipes.doctype_of(table) is None:
		return None
	dialect = get_dialect()
	if getattr(dialect, "name", "") != "postgres":
		return _read_evidence(table, dialect)
	import frappe

	savepoint = f"optimus_evidence_{next(_evidence_savepoints)}"
	frappe.db.savepoint(savepoint)
	try:
		evidence = _read_evidence(table, dialect)
		frappe.db.release_savepoint(savepoint)
		return evidence
	except Exception as error:
		failure = error
	# Outside the except, so nothing chains: a rollback that fails must not hide why the read
	# failed, but a job timeout raised while rolling back must still stop the job.
	guard = InterruptGuard()
	try:
		with guard:
			frappe.db.rollback(save_point=savepoint)
	except Exception:
		pass
	if guard.pending():
		raise guard.interrupt()
	raise failure


def _read_evidence(table: str, dialect) -> TableEvidence | None:
	import frappe

	doctype = index_recipes.doctype_of(table)
	if not frappe.db.exists("DocType", doctype):
		return None
	meta = _get_meta_quietly(doctype)
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
	if not indexes:
		raise EmptyIndexList(table)
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


MAX_LOGGED_EVIDENCE_FAILURES = 10


class _EvidenceLookup:
	"""A per-render ``evidence_lookup(table)``: memoised per table (misses included), so
	each table costs its queries once per render. Ordinary failures give None and write one
	bench-log line per table (O2); ``read_failed(table)`` tells such a failure from a table
	that has no evidence (no DocType on this site), so the advice can say which. An RQ job
	timeout escapes as a fresh instance."""

	def __init__(self) -> None:
		self._cache: dict[str, TableEvidence | None] = {}
		self._failed: set[str] = set()

	def __call__(self, table: str) -> TableEvidence | None:
		key = str(table or "").strip().strip("`")
		if key not in self._cache:
			def _failed(error_type: str) -> None:
				self._failed.add(key)
				if len(self._failed) <= MAX_LOGGED_EVIDENCE_FAILURES:
					log_error_line(f"optimus: evidence read failed for {key}: {error_type}")

			self._cache[key] = best_effort(lambda: _read_table_evidence(key), None, on_error=_failed)
		return self._cache[key]

	def read_failed(self, table: str) -> bool:
		return str(table or "").strip().strip("`") in self._failed


def make_evidence_lookup() -> Callable[[str], TableEvidence | None]:
	"""A fresh ``_EvidenceLookup``: one per render, export and AI run."""
	return _EvidenceLookup()


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


# What ``best_effort`` gives back when the index advisor raised (the report and the export).
RECIPE_FAILED = object()
# A failure is no verdict on the index, so the notes say what to do next (U2, U3). The
# quoted text is how log_recipe_failures' bench-log line starts.
_FAILED_NEXT = (
	'if it keeps happening, send the bench log line "optimus: index advice failed" to the Optimus maintainers.'
)
RECIPE_FAILED_HINT = (
	f"Optimus could not build index advice for this finding. Check the query with EXPLAIN yourself; {_FAILED_NEXT}"
)
RECIPE_FAILED_CARD_NOTE = (
	f"{index_recipes.NO_VERDICT} It could not build index advice for this table. Check the slow queries on this "
	f"table with EXPLAIN yourself; {_FAILED_NEXT}"
)

# The finding's own text when its index advice gives no code or failed (U1/E2/A2). The
# analyzers bake "Add index on ..." and "Ask your developer to add this index in a database
# migration" into the stored title and description at analyze time; the render dict (and
# the export) get these instead, and the stored JSON keeps the analyzer's text.
NO_INDEX_TITLE = "Index on {table}({column}): no new index recommended"
# The title when the advice is no verdict: "no new index recommended" would be one.
NO_INDEX_UNKNOWN_TITLE = "Index on {table}({column}): Optimus cannot say"
NO_INDEX_DESCRIPTION = (
	"Queries in this session filtered on the **{column}** column of the **{table}** table. Optimus does not "
	"recommend a new index on it: How to fix says why and what to check instead."
)
NO_INDEX_NOTE = "Optimus gives no index code for this query: How to fix says why and what to check instead."
# The same when the advice is no verdict (``unknown``: Optimus could not tell, or failed).
NO_INDEX_UNKNOWN_DESCRIPTION = (
	"Queries in this session filtered on the **{column}** column of the **{table}** table. Optimus cannot say "
	"whether a new index on it would help: How to fix says what to check."
)
NO_INDEX_UNKNOWN_NOTE = "Optimus cannot say whether an index would help this query: How to fix says what to check."
# The action plan's step label for a no-code Missing Index, instead of "Add a database index",
# and for a Filesort or Temporary Table whose index leaves the sort or the temporary table in
# place, instead of "Avoid the filesort" or "Avoid the temporary table".
NO_INDEX_ACTION_TITLE = "Check the query with EXPLAIN"
# The note a Filesort or Temporary Table finding gets when its index keeps the sort or the
# temporary table (the advice's ``sort_stays`` is "stays"), in place of the promise that an
# index fixes it; when the sort comes from elsewhere in the query and only may stay ("may
# stay"), the note says so.
SORT_STAYS_NOTES: dict[str, str] = {
	"Filesort": "The index under How to fix does not remove the sort: How to fix says why.",
	"Temporary Table": "The index under How to fix does not remove the temporary table: How to fix says why.",
}
SORT_MAY_STAY_NOTES: dict[str, str] = {
	"Filesort": "The index under How to fix may not remove the sort: How to fix says why.",
	"Temporary Table": "The index under How to fix may not remove the temporary table: How to fix says why.",
}
_MIGRATION_PHRASE = "in a database migration"
_HOW_TO_FIX_PHRASE = "using the code and steps under How to fix"
# The explain_flags sentences that promise an index fixes the finding.
_INDEX_FIX_SENTENCES: tuple[str, ...] = (
	"Adding an appropriate index is usually the fix.",
	"Adding an index that covers the ORDER BY clause usually fixes it.",
	"Usually fixable by adding or reshaping an index so the filter is applied at the index level instead of "
	"per-row.",
)


class _QueryParser:
	"""A per-render (or per-export) ``parse(query)`` for the advisor, memoised on the query
	text (a query can back several findings) and never run on a query over
	``index_recipes.MAX_QUERY_CHARS`` (the advisor explains those instead). ``aliases(query)``
	memoises the alias map, a second sql_metadata parse of the same text (PF2: it took 42 to
	69 percent of the recipe stage when it ran once per finding)."""

	def __init__(self) -> None:
		self._parsed: dict[str, dict] = {}
		self._aliases: dict[str, dict] = {}

	def __call__(self, query: str) -> dict:
		if len(query or "") > index_recipes.MAX_QUERY_CHARS:
			return {}
		if query not in self._parsed:
			self._parsed[query] = index_recipes.parse_query(query)
		return self._parsed[query]

	def aliases(self, query: str) -> dict:
		if len(query or "") > index_recipes.MAX_QUERY_CHARS:
			return {}
		if query not in self._aliases:
			self._aliases[query] = index_recipes.table_aliases(query)
		return self._aliases[query]


def make_query_parser() -> Callable[[str], dict]:
	"""A fresh ``_QueryParser``: one per render and one per export."""
	return _QueryParser()


MAX_LOGGED_RECIPE_ERRORS = 10


def log_recipe_failures(count: int, *, where: str = "render", errors=()) -> None:
	"""One bench-log line, at ERROR (Frappe drops lower levels on a production site), for
	index advice that raised during one render or export (``where``, O-I1). ``errors`` are the
	``(finding type or table, error type)`` pairs, deduped and capped, so the line says what
	failed. Called after the recipes ran, never inside an ``except``. A logger failure is
	ignored; an RQ job timeout escapes as a fresh instance."""
	if not count:
		return
	pairs = list(dict.fromkeys((str(label or "?"), str(kind or "?")) for label, kind in errors or ()))
	line = f"optimus: index advice failed for {count} finding(s) or table(s) in one {where}"
	if pairs:
		shown = ", ".join(f"{label}: {kind}" for label, kind in pairs[:MAX_LOGGED_RECIPE_ERRORS])
		more = len(pairs) - MAX_LOGGED_RECIPE_ERRORS
		line += f" ({shown}{f', and {more} more' if more > 0 else ''})"

	def _write() -> None:
		import frappe

		frappe.logger("optimus").error(line)

	best_effort(_write, None)


def export_advice(
	finding: dict,
	*,
	evidence_lookup: Callable[[str], TableEvidence | None],
	tracked_apps: tuple[str, ...] = (),
	parser: Callable[[str], dict] | None = None,
	errors: list | None = None,
) -> tuple[dict | None, bool]:
	"""``(advice, failed)`` for one index-family finding, the one advice step the report
	and the export share, so the export equals the report (M4). ``advice`` is the export's
	``index_advice`` dict (``route``, ``doctype``, ``table``, ``columns``, ``index_name``,
	``text`` and ``code``; the report shows ``text`` as the fix hint and ``code`` as the
	suggested index), or None when the advisor has nothing to say. ``failed`` is True when
	the advisor raised: ``advice`` is then the failure shape (route no_code, the
	``RECIPE_FAILED_HINT`` text, no code), and the caller counts it for one log line.
	``unknown`` is True when the advice is no verdict on the index (``IndexAdvice.unknown``,
	or a failure), so the finding's description stays neutral. A failure appends
	``(finding type, error type)`` to ``errors`` for the caller's one log line.
	``sort_stays`` is "stays" when a Filesort or Temporary Table finding's code leaves the
	sort or the temporary table in place, "may stay" when it may, else ""
	(``IndexAdvice.sort_stays``)."""
	advice = best_effort(
		lambda: index_recipes.advise_finding(
			finding, evidence_lookup=evidence_lookup, tracked_apps=tuple(tracked_apps or ()), parser=parser,
		),
		RECIPE_FAILED,
		on_error=lambda kind: errors.append((str(finding.get("finding_type") or "finding"), kind))
		if errors is not None else None,
	)
	if advice is RECIPE_FAILED:
		detail = finding.get("technical_detail")
		table = str(detail.get("table") or "") if isinstance(detail, dict) else ""
		doctype = index_recipes.doctype_of(table)
		return {
			"route": index_recipes.ROUTE_NO_CODE,
			"doctype": doctype,
			"table": f"tab{doctype}" if doctype else None,
			"columns": [],
			"index_name": None,
			"text": RECIPE_FAILED_HINT,
			"code": None,
			"unknown": True,
			"sort_stays": "",
		}, True
	if advice is None:
		return None, False
	return {
		"route": advice.route,
		"doctype": advice.doctype,
		"table": advice.table,
		"columns": list(advice.columns),
		"index_name": (advice.entry or {}).get("index_name"),
		"text": index_recipes.finding_text(advice),
		"code": advice.code,
		"unknown": advice.unknown,
		"sort_stays": advice.sort_stays,
	}, False


def finding_display(finding: dict, advice: dict | None) -> dict:
	"""The ``title`` and ``customer_description`` an index-family finding shows next to
	``advice`` (``export_advice``'s dict), only the keys that change (U1/E2/A2). Pure; the
	stored row is never touched. With no code (no_code, or a failed advisor):

	- a Missing Index gets "Index on <table>(<column>): no new index recommended" and a
	  neutral line instead of "Add index on ..." and "Ask your developer to add this index";
	- an EXPLAIN-family finding loses the sentence that promises an index fixes it and
	  gains ``NO_INDEX_NOTE``.

	When the advice is no verdict (``advice["unknown"]``: Optimus could not tell, or the
	advisor failed) the title, the line and the note say Optimus cannot say whether an index
	would help, never that it recommends none.

	With code, a Missing Index points at the code and steps under How to fix instead of "a
	database migration", and a Filesort or Temporary Table whose index leaves the sort or
	the temporary table in place (``advice["sort_stays"]``) loses the sentence that promises
	an index fixes it and gains ``SORT_STAYS_NOTES`` (``SORT_MAY_STAY_NOTES`` when it only
	may stay). Applying it to its own output changes
	nothing."""
	ftype = finding.get("finding_type") or ""
	if advice is None or ftype not in INDEX_FINDING_TYPES:
		return {}
	description = str(finding.get("customer_description") or "")
	if advice.get("route") != index_recipes.ROUTE_NO_CODE:
		if ftype == "Missing Index" and _MIGRATION_PHRASE in description:
			return {"customer_description": description.replace(_MIGRATION_PHRASE, _HOW_TO_FIX_PHRASE)}
		if ftype in SORT_STAYS_NOTES and advice.get("sort_stays"):
			notes = SORT_MAY_STAY_NOTES if advice["sort_stays"] == "may stay" else SORT_STAYS_NOTES
			for sentence in _INDEX_FIX_SENTENCES:
				description = description.replace(sentence, "")
			return {"customer_description": _with_note(" ".join(description.split()), notes[ftype])}
		return {}
	unknown = bool(advice.get("unknown"))
	if ftype == "Missing Index":
		detail = finding.get("technical_detail")
		detail = detail if isinstance(detail, dict) else {}
		table = str(detail.get("table") or "").strip().strip("`")
		column = str(detail.get("column") or "").strip().strip("`")
		if not table or not column:
			return {}
		line = NO_INDEX_UNKNOWN_DESCRIPTION if unknown else NO_INDEX_DESCRIPTION
		title = NO_INDEX_UNKNOWN_TITLE if unknown else NO_INDEX_TITLE
		return {
			"title": title.format(table=table, column=column),
			"customer_description": line.format(table=table, column=column),
		}
	for sentence in _INDEX_FIX_SENTENCES:
		description = description.replace(sentence, "")
	note = NO_INDEX_UNKNOWN_NOTE if unknown else NO_INDEX_NOTE
	return {"customer_description": _with_note(" ".join(description.split()), note)}


def apply_finding_recipes(
	findings: list[dict],
	*,
	evidence_lookup: Callable[[str], TableEvidence | None],
	tracked_apps: tuple[str, ...] = (),
	installed_apps: frozenset[str] | None = None,
	parser: Callable[[str], dict] | None = None,
	errors: list | None = None,
) -> dict:
	"""Fill each render dict's recipe slots in place and return ``{"failed": n}``, the
	index advice that raised. An index-family finding's title, description and action-plan
	label follow its advice (``finding_display``; ``action_title`` is the render-only label
	for a no-code Missing Index, and for a Filesort or Temporary Table whose index keeps the
	sort or the temporary table). ``errors`` collects ``(finding type, error type)`` for each
	failure (the caller's one log line). Running it twice leaves the same dicts as running it once."""
	stats = {"failed": 0}
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
			advice, failed = export_advice(
				f, evidence_lookup=evidence_lookup, tracked_apps=scope, parser=parser, errors=errors,
			)
			# Raw analyzer DDL never reaches the report, whatever the advice turned out to be.
			detail.pop("suggested_ddl", None)
			stats["failed"] += failed
			if advice is not None:
				detail["fix_hint"] = advice["text"]
				if advice["code"]:
					detail["suggested_ddl"] = advice["code"]
			f.update(finding_display(f, advice))
			if advice is not None and (
				(ftype == "Missing Index" and advice["route"] == index_recipes.ROUTE_NO_CODE)
				or (ftype in SORT_STAYS_NOTES and advice["route"] != index_recipes.ROUTE_NO_CODE and advice["sort_stays"])
			):
				f["action_title"] = NO_INDEX_ACTION_TITLE
		elif ftype == "Redundant Call":
			if f.get("llm_fix") and ai_grounding.analyzed_before_callsite_fix(f):
				detail["validation_note"] = _with_note(
					detail.get("validation_note"), ai_grounding.UNSTAMPED_REDUNDANT_CALL_NOTE,
				)
		elif ftype == "Framework N+1":
			f["llm_fix"] = None
			detail["fix_hint"] = _with_note(detail.get("fix_hint"), ai_grounding.FRAMEWORK_N1_NOTE)
		elif ftype == "Hot Line":
			# A gate that raises fails closed: no stored AI fix, a neutral note (O-I1).
			note = best_effort(
				lambda: ai_grounding.hot_line_gate(f, tracked_apps=scope, installed_apps=installed_apps),
				ai_grounding.GATE_CHECK_FAILED_NOTE,
				on_error=lambda kind: log_error_line(f"optimus: hot-line gate failed: {kind}"),
			)
			if note:
				detail["fix_hint"] = note
				f["llm_fix"] = None
	return stats


def apply_table_recipes(
	table_breakdown: list[dict],
	*,
	evidence_lookup: Callable[[str], TableEvidence | None],
	tracked_apps: tuple[str, ...] = (),
	errors: list | None = None,
) -> dict:
	"""Drop ``ai_index`` from every table entry and run each card's ``recommended_index``
	through the same advisor as the findings, in place; return ``{"failed": n}``. The
	recommendation is kept, single column included (P6), and gains ``route``,
	``route_note`` (the card's note), ``code``, ``index_name`` and ``requested_columns``
	(the analyzer's columns; ``columns`` becomes the advice's); it is dropped only when
	the advisor has nothing to say (no DocType table, no usable column). Running it twice
	leaves the same cards as running it once. ``errors`` collects ``(table, error type)`` for
	each failure."""
	stats = {"failed": 0}
	scope = tuple(tracked_apps or ())
	for t in table_breakdown or []:
		if not isinstance(t, dict):
			continue
		t.pop("ai_index", None)
		rec = t.get("recommended_index")
		if not isinstance(rec, dict) or not rec.get("columns"):
			continue
		# The analyzer's columns, kept on the first pass: ``columns`` becomes the advice's,
		# so a second pass advises the same request and still names a column the advice
		# left out (a Postgres text column, the key width limit).
		requested = rec.get("requested_columns")
		if not isinstance(requested, list):
			requested = rec["requested_columns"] = list(rec.get("columns") or [])
		advice = best_effort(
			lambda: index_recipes.advise_table(
				t.get("table") or "", list(requested), evidence_lookup=evidence_lookup, tracked_apps=scope,
			),
			RECIPE_FAILED,
			on_error=lambda kind: errors.append((str(t.get("table") or "table"), kind)) if errors is not None else None,
		)
		if advice is RECIPE_FAILED:
			rec.update({"route": index_recipes.ROUTE_NO_CODE, "route_note": RECIPE_FAILED_CARD_NOTE, "code": None, "index_name": None})
			stats["failed"] += 1
			continue
		if advice is None:
			t.pop("recommended_index", None)
			continue
		rec["columns"] = list(advice.columns)
		rec["route"] = advice.route
		rec["route_note"] = index_recipes.card_note(advice)
		rec["code"] = advice.code
		rec["index_name"] = (advice.entry or {}).get("index_name")
	return stats


def mark_outdated_ai_fixes(
	findings: list[dict], *, current_version: int | None = None, refresh_check: Callable[[dict], bool] | None = None,
) -> None:
	"""On every rendered AI suggestion set, in place, ``llm_fix["outdated"]`` (made with
	an older prompt version than ``current_version``, default
	``ai_prompts.PROMPT_VERSION``, or with none recorded) and ``llm_fix["refreshable"]``
	(``refresh_check(finding)`` says Refresh AI suggestions would redo it)."""
	from optimus.ai_prompts import is_current

	for f in findings or []:
		fix = f.get("llm_fix") if isinstance(f, dict) else None
		if not isinstance(fix, dict):
			continue
		current = is_current(fix, current_version)
		fix["outdated"] = not current
		fix["refreshable"] = bool(
			not current and refresh_check is not None and best_effort(lambda: refresh_check(f), False)
		)
