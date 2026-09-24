# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""On-demand LLM-suggested fixes for Optimus Findings.

Turns a finding's callsite / source snippet / normalized SQL + EXPLAIN into a
concrete fix by asking a configured LLM. Invoked only from the
``optimus.api.suggest_fix`` whitelisted endpoint, never from an analyzer, so
the pure-analyzer / frozen-capture invariants are untouched.

Provider-agnostic: two wire formats (Anthropic Messages, OpenAI Chat
Completions) chosen by the ``ai_provider`` Select; ``ai_base_url`` /
``ai_model`` / ``ai_api_key`` are overridable in Optimus Settings so a local
model (Ollama / LM Studio / vLLM) can be used with nothing leaving the box.
``frappe`` is lazy-imported inside each function so the pure prompt / HTTP
helpers are unit-testable without a bench.
"""

from __future__ import annotations

import re
import traceback
from datetime import datetime, timezone
from typing import Any

import requests

from optimus.analyzers.base import humanize_duration_ms


class AiFixError(Exception):
	"""User-facing error from the AI-fix path. The API endpoint converts
	this into ``frappe.throw`` so the message is shown to the operator.
	``status_code`` carries the provider's HTTP status when the error came
	from an HTTP response, so callers can react to it (the temperature retry
	fires only on a 400 or 422). ``kind`` classifies the failure
	(``"config"``, ``"transport"``, ``"timeout"``, ``"bad_response"`` here;
	later releases fill the rest). ``usage`` carries token usage already
	billed before the failure, when there was any.

	The message must never contain the API key: it is shown to the operator
	and written to the Error Log."""

	def __init__(
		self,
		message: str = "",
		*,
		status_code: int | None = None,
		kind: str = "unknown",
		usage: dict | None = None,
	):
		super().__init__(message)
		self.status_code = status_code
		self.kind = kind
		self.usage = usage


# Findings that carry enough code / SQL context for the LLM to reason about
# a concrete fix. Infra / frontend / "function not invoked" findings are
# excluded the LLM would only get a title + a couple of numbers.
#
# v0.7.x: Slow Hot Path / Hook Bottleneck / Repeated Hot Frame removed.
# Their AI suggestions are structurally generic (the LLM only sees a
# function name + percentage + line range) and the actionable insight
# already lives on the embedded N+1 / Hot Line / Redundant Call that
# shares the same chain leaf. Skipping these types saves tokens without
# losing diagnostic signal the broader hot-path findings still appear
# in the Findings section with their smoking-gun + drill-down; they
# just no longer carry an LLM-rendered "Suggested fix" block.
AI_ELIGIBLE_FINDING_TYPES: frozenset[str] = frozenset({
	"N+1 Query",
	"Framework N+1",
	"Slow Query",
	"Missing Index",
	"Full Table Scan",
	"Filesort",
	"Temporary Table",
	"Low Filter Ratio",
	"Redundant Call",
	"Hot Line",
})

# Per-provider protocol + sensible defaults. ``ai_base_url`` / ``ai_model``
# from Optimus Settings override these; the "OpenAI-compatible" provider
# REQUIRES both (no hosted default to fall back to).
_PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
	"Anthropic": {
		"protocol": "anthropic",
		"base_url": "https://api.anthropic.com",
		"model": "claude-sonnet-4-6",
		"needs_key": True,
	},
	"OpenAI": {
		"protocol": "openai",
		"base_url": "https://api.openai.com/v1",
		"model": "gpt-4.1-mini",
		"needs_key": True,
	},
	"Kimi (Moonshot)": {
		"protocol": "openai",
		"base_url": "https://api.moonshot.ai/v1",
		"model": "kimi-k2-0905-preview",
		"needs_key": True,
	},
	"DeepSeek": {
		# DeepSeek's API is OpenAI-compatible, so it reuses the OpenAI wire
		# path. Default to deepseek-chat (V3). deepseek-reasoner (R1) also works
		# and ignores a custom temperature instead of rejecting it, so the
		# temperature retry in _call_openai_chat never needs to fire for it.
		"protocol": "openai",
		"base_url": "https://api.deepseek.com/v1",
		"model": "deepseek-chat",
		"needs_key": True,
	},
	"OpenAI-compatible": {
		"protocol": "openai",
		"base_url": "",
		"model": "",
		# Local endpoints (Ollama / LM Studio / vLLM) usually need no key.
		"needs_key": False,
	},
	# v0.14.x: Aerele-managed AI provider. Architecturally identical to
	# the Anthropic / OpenAI entries just a hosted endpoint + an API
	# key the customer pastes into ``ai_api_key``. The token balance,
	# pre-call validation and metering all live on Aerele's separate
	# Frappe site (the URL below); Optimus is a dumb client. Aerele's
	# proxy fronts an OpenAI-shaped wire so ``_call_openai_chat`` routes
	# correctly without a new protocol handler. See
	# ``docs/AI-FIXING.md`` §10.
	#
	# TEMPORARILY DISABLED until Aerele billing + the managed LLM gateway
	# are production-ready. To re-enable: uncomment this entry AND add
	# "Aerele" back to the ai_provider Select options (plus its two
	# descriptions) in optimus_settings.json. The _aerele_call_metadata
	# wiring further down is left intact, ready to use.
	# "Aerele": {
	# 	"protocol": "openai",
	# 	"base_url": "https://api.aerele.in/optimus/v1",
	# 	"model": "claude-sonnet-4-6",  # Aerele picks the upstream model
	# 	"needs_key": True,
	# },
}
_DEFAULT_PROVIDER = "Anthropic"

# Fallback timeout when settings can't be read (pure-pytest path with no
# bench, or a settings cache miss during early bootstrap). v0.9.0+ the live
# value comes from cfg.ai_request_timeout_seconds (clamped 10–600s).
_HTTP_TIMEOUT = 60            # seconds; one shot, no retries
_MAX_OUTPUT_TOKENS = 2000
_SOURCE_LINES_BEFORE = 24     # how much code window the caller should gather
_SOURCE_LINES_AFTER = 24
_MAX_SOURCE_WINDOW_LINES = 80
_MAX_QUERY_CHARS = 2400
_MAX_USER_CONTENT_CHARS = 18000
# Low temperature we want the model to stick to the code it was shown, not
# get "creative". OpenAI's o-series reasoning models reject a non-default
# temperature, so it's omitted for those (see `_is_reasoning_model`).
_TEMPERATURE = 0.1

_ANTHROPIC_VERSION = "2023-06-01"

# Compact "what this finding type means + how it's usually fixed in Frappe"
# line, injected into the user message. Keeps the system prompt general and
# gives the model a strong, type-specific starting point.
_FINDING_TYPE_HINTS = {
	"N+1 Query": "A query repeats once per row of an outer loop. Fix: lift it out of the loop and batch one `frappe.get_all(<DocType>, filters={'name': ('in', names)}, fields=[...])` (or `frappe.db.get_values`) then build a dict keyed by the join column.",
	"Framework N+1": "Same N+1 pattern, but the loop is inside framework code (frappe/erpnext). Fix: change YOUR calling pattern so the framework isn't invoked per-row e.g. pass a list of names where the API accepts one, fetch needed fields up front, or avoid `get_doc` in a loop.",
	"Slow Query": "A single SQL statement is slow. Fix: add the right index (Frappe way: Customize Form → the field → tick 'Search Index'; raw `ALTER TABLE … ADD INDEX` only if you can't customize), or restructure the WHERE/ORDER BY so an existing index is usable, or reduce the rows touched (tighter filters, fewer columns).",
	"Missing Index": "A WHERE/JOIN/ORDER BY column has no usable index. Fix: add a Search Index (Customize Form → field → 'Search Index') prefer a composite index when several columns are filtered together; only fall back to `ALTER TABLE … ADD INDEX (...)` if customization isn't an option.",
	"Full Table Scan": "EXPLAIN shows `type=ALL`: the whole table is read. Fix: index the filtering column (Search Index), or make the WHERE sargable (no functions on the column, no leading-wildcard LIKE).",
	"Filesort": "EXPLAIN shows `Using filesort`: MariaDB sorts the result set in memory/on disk. Fix: a composite index ending in the ORDER BY column(s) so the read returns rows already ordered; or, if the sort isn't needed, drop the ORDER BY.",
	"Temporary Table": "EXPLAIN shows `Using temporary`: usually a GROUP BY / DISTINCT that can't use an index. Fix: a covering composite index on the grouped columns, or pre-aggregate, or drop an unnecessary DISTINCT.",
	"Low Filter Ratio": "EXPLAIN's `filtered` is low the index (if any) isn't selective; most examined rows are thrown away. Fix: index a more selective column, add a composite index matching the WHERE, or tighten the filter.",
	"Redundant Call": "The same `get_doc` / cache lookup / `has_permission` runs many times for the same arguments from one callsite. Fix: hoist it out of the loop, or memoize for the request stash on `frappe.local` (request-scoped) or `frappe.cache().get_value(key)` / `set_value(key, val, expires_in_sec=…)` for cross-request.",
	"Slow Hot Path": "A subtree of the call tree dominates the action's wall time. Fix: look at what that function does fetching data it doesn't need, doing per-row work that could be batched, recomputing something cacheable and remove/defer/batch it. `frappe.enqueue(...)` if it's work that doesn't need to block the response.",
	"Hook Bottleneck": "A `doc_events` / `before_*` / `after_*` hook is expensive and runs on every save/submit. Fix: make the hook do less (skip when nothing relevant changed check `doc.has_value_changed(...)`), or move heavy work to `frappe.enqueue(...)` so it doesn't block the save.",
	"Repeated Hot Frame": "The same function shows up many times in the sampled stacks it's called a lot. Fix: reduce call count (batch / cache) or make each call cheaper.",
	"Hot Line": "A single source line is the dominant time sink inside its function. Fix: optimize that line specifically hoist invariant work out of a loop, replace an O(n²) pattern, avoid a per-iteration DB/cache hit, or use a set/dict for membership tests.",
}

# Postgres phrasings for the four EXPLAIN-based hints. The rest of
# _FINDING_TYPE_HINTS is dialect-neutral; MariaDB uses it verbatim. On Postgres
# these swap the MariaDB EXPLAIN-column wording (type=ALL / Using filesort / …)
# for plan-node wording (Seq Scan / Sort node / HashAggregate). The fix advice
# is identical.
_POSTGRES_EXPLAIN_HINTS = {
	"Full Table Scan": "EXPLAIN shows a `Seq Scan`: the whole table is read. Fix: index the filtering column (Search Index), or make the WHERE sargable (no functions on the column, no leading-wildcard LIKE).",
	"Filesort": "EXPLAIN shows a `Sort` node no index provides the required order, so Postgres sorts in memory/on disk. Fix: a composite index ending in the ORDER BY column(s) so the read returns rows already ordered; or, if the sort isn't needed, drop the ORDER BY.",
	"Temporary Table": "EXPLAIN shows a `HashAggregate` / `Materialize`: usually a GROUP BY / DISTINCT that can't use an index. Fix: a covering composite index on the grouped columns, or pre-aggregate, or drop an unnecessary DISTINCT.",
	"Low Filter Ratio": "EXPLAIN's row-count estimate shows low selectivity most examined rows are thrown away. Fix: index a more selective column, add a composite index matching the WHERE, or tighten the filter.",
}


def _finding_type_hint(ftype):
	"""Per-finding-type hint for the LLM prompt. The four EXPLAIN-based hints are
	phrased for the active dialect (MariaDB EXPLAIN columns vs Postgres plan
	nodes); the rest are dialect-neutral."""
	if ftype in _POSTGRES_EXPLAIN_HINTS:
		try:
			from optimus.dbdialect import active_db_type
			if active_db_type() == "postgres":
				return _POSTGRES_EXPLAIN_HINTS[ftype]
		except Exception:
			pass
	return _FINDING_TYPE_HINTS.get(ftype)

_SYSTEM_PROMPT = (
	"You are a senior Frappe Framework / ERPNext engineer doing a precise code "
	"review of one finding from a performance profiler. Propose the smallest "
	"concrete, Frappe-idiomatic change that fixes the ROOT CAUSE not generic "
	"advice, not a rewrite.\n\n"

	"WRITE IDIOMATIC FRAPPE. Use `frappe.get_all` / `frappe.get_list` / "
	"`frappe.db.get_value` / `frappe.db.get_values` / `frappe.qb` (the query "
	"builder) never hand-built SQL strings and never an ORM call inside a "
	"loop. Per-request memoization goes on `frappe.local`; cross-request "
	"caching goes through `frappe.cache().get_value(key)` / "
	"`set_value(key, value, expires_in_sec=...)`. Background work that needn't "
	"block the response goes through `frappe.enqueue(...)`. Adding an index "
	"means: Customize Form → the field → tick **Search Index** (which `bench "
	"migrate` then creates) only fall back to a raw `ALTER TABLE ... ADD "
	"INDEX (...)` when customization genuinely isn't an option and prefer a "
	"single composite index over several single-column ones when the same "
	"columns are filtered together.\n\n"

	"NO RAW SQL IN YOUR PROPOSED FIX. `frappe.db.sql(\"SELECT ...\")` / "
	"`\"INSERT ...\"` / `\"UPDATE ...\"` / `\"DELETE ...\"` / `\"REPLACE ...\"` "
	"is forbidden in your `+` lines (proposed code) even when the `before` "
	"code being REPLACED was raw SQL. The replacement MUST use one of the "
	"framework APIs above: `frappe.get_all` for typical reads / lists; "
	"`frappe.qb` for joins, aggregations, dynamic conditions or anything the "
	"Document API can't express; `frappe.db.get_value` / `frappe.db.get_values` "
	"for single-row / dict-shape lookups. If you genuinely cannot map the SQL "
	"to a framework call without breaking semantics, say so plainly in "
	"**Diagnosis** and leave the SQL in place (recommend caching / hoisting / "
	"adding an index / changing the query shape instead). Narrow exception: "
	"DDL via raw SQL `ALTER TABLE ... ADD INDEX ...` / `CREATE INDEX ...` "
	" is acceptable when an index recommendation truly can't go through "
	"Customize Form's Search Index toggle.\n\n"

	"GROUND EVERYTHING IN THE CODE YOU WERE SHOWN DO NOT INVENT CODE. The "
	"only source you have is what appears under \"Source around the callsite\" "
	"in the user message (if anything appears there at all). Treat every other "
	"line of code as unknown to you. Hard rules:\n"
	"  • If your **Fix** shows a \"before\" snippet or `-` lines in a "
	"```diff``` block every one of those lines MUST be copied VERBATIM from "
	"the shown source: identical text and keep its line number. Do NOT "
	"reconstruct, paraphrase, summarise, or imagine what the code \"probably\" "
	"looks like. A `for … in …:` loop, a `frappe.get_doc(...)` call, a "
	"variable name if you weren't shown it, you don't get to write it as if "
	"you were.\n"
	"  • If the loop / call / WHERE / line this finding is actually about is "
	"NOT visible in the shown source (or no source was shown at all), then you "
	"do NOT have the offending code. In that case: in **Diagnosis** say so "
	"plainly (\"the offending code isn't in the window I was shown it's "
	"likely in `<name>`\") and in **Fix** give ONLY a short directional "
	"recommendation, explicitly framed as \"without seeing the code, the likely "
	"fix is …\". NO before/after snippet, NO diff, NO fabricated code block.\n"
	"  • Never present a guess as a verified fix. If you're not certain a "
	"symbol exists, don't use it or mark it clearly as an assumption.\n"
	"  • SQL substitution discipline: if your **Fix** replaces a raw SQL "
	"string with `frappe.get_all` / `frappe.get_list` / `frappe.db.get_value` "
	"/ `frappe.db.get_values` / `frappe.qb`, the new call MUST be "
	"semantically equivalent to the SQL it replaces same table(s), same "
	"WHERE / JOIN / GROUP BY / ORDER BY / LIMIT, same field list. If the SQL "
	"had no WHERE clause, the replacement gets no `filters=`. If the SQL had "
	"`LIMIT N`, the replacement gets `limit=N`. Do NOT invent filters by "
	"copying a variable that appears elsewhere in the function "
	"(e.g. `frappe.session.user`) and NEVER synthesise list shapes like "
	"`[some_var] * N` to fit an `('in', ...)` filter that is hallucination, "
	"not refactoring. If you cannot preserve semantics, say so plainly in "
	"**Diagnosis** and leave the SQL as-is (recommend caching / hoisting / "
	"adding an index instead).\n\n"

	"NEVER suggest indexing Frappe's standard metadata columns `name`, "
	"`idx`, `parent`, `parentfield`, `parenttype`, `creation`, `modified`, "
	"`modified_by`, `owner`, `docstatus`, `doctype`, `_user_tags`, "
	"`_comments`, `_assign`, `_liked_by`, `_seen`: nor any of Frappe's "
	"framework meta tables (`tabDocType`, `tabDocField`, `tabCustom Field`, "
	"`tabProperty Setter`, `tabSingles`, `tabSeries`, `tab__global_search`, "
	"workspace/dashboard config tables, …). Frappe writes the former on every "
	"save (or they're already indexed); `bench migrate` owns the latter's "
	"schema. If the only index you can think of targets one of those, say "
	"there's no good index-side fix and propose a query-shape change instead.\n\n"

	"OUTPUT Markdown, exactly these four headings, nothing before or after:\n"
	"**Diagnosis**: 1-2 sentences: the actual cause, referring to the shown "
	"source by line number when you can (e.g. \"the `frappe.get_doc(...)` on "
	"line 14 runs once per item N round-trips\"). If the offending code "
	"wasn't shown to you, say that here.\n"
	"**Fix**: the concrete change, using real Frappe APIs. Only if the "
	"offending code is in the source you were shown: present it as a unified "
	"diff in a ```diff fenced block (preferred it renders with before/after "
	"highlighting; `-` lines = the existing code copied verbatim from the "
	"source above, `+` lines = the replacement). Otherwise: NO snippet/diff "
	"just the directional recommendation (\"without seeing the code, the likely "
	"fix is …\"). For an index, give the Customize Form path AND (only as a "
	"fallback) the `ALTER TABLE` DDL that's a config change, not invented "
	"code, so it's fine without a source window.\n"
	"**Why it works**: 1-2 sentences tying the change to the cause.\n"
	"**Verify**: 1 line: how to confirm it worked (re-profile the same flow "
	"and check the relevant number dropped query count / wall time / EXPLAIN).\n\n"

	"Keep the whole answer focused roughly 150-350 words. Do not restate the "
	"finding's title or numbers back at the reader.\n\n"

	"EXAMPLE this is ONLY to show the heading shape and the verbatim-before "
	"discipline. It happens to be an N+1; that does NOT mean your finding is an "
	"N+1 most aren't. Match YOUR finding type and YOUR shown code and if "
	"your source window doesn't contain a loop like this one, do NOT produce a "
	"diff like this one:\n"
	"**Diagnosis**: `frappe.db.get_value('Item', d.item_code, 'stock_uom')` "
	"on line 12 runs once per row of `self.items`: that's the N+1.\n"
	"**Fix**\n"
	"```diff\n"
	"-for d in self.items:\n"
	"-    uom = frappe.db.get_value('Item', d.item_code, 'stock_uom')\n"
	"-    ...\n"
	"+uoms = {r.name: r.stock_uom for r in frappe.get_all(\n"
	"+    'Item', filters={'name': ('in', [d.item_code for d in self.items])},\n"
	"+    fields=['name', 'stock_uom'])}\n"
	"+for d in self.items:\n"
	"+    uom = uoms.get(d.item_code)\n"
	"+    ...\n"
	"```\n"
	"**Why it works**: one batched `frappe.get_all` replaces N per-row "
	"queries; the dict lookup is in-memory.\n"
	"**Verify**: re-record the same Save and confirm the `tabItem` query "
	"count for this action dropped from ~N to 1.\n\n"

	"SECOND EXAMPLE same heading shape, this time showing the "
	"SQL-equivalence rule: a raw SQL with NO WHERE clause maps to a "
	"`frappe.get_all` with NO `filters=`. Notice the replacement preserves "
	"exactly the original table, fields and LIMIT nothing is invented:\n"
	"**Diagnosis**: line 207 runs a raw `SELECT name, email FROM `tabUser` "
	"LIMIT 50` which can be replaced with the framework-idiomatic call.\n"
	"**Fix**\n"
	"```diff\n"
	"-users = frappe.db.sql(\"SELECT name, email FROM `tabUser` LIMIT 50\", as_dict=True)\n"
	"+users = frappe.get_all('User', fields=['name', 'email'], limit=50)\n"
	"```\n"
	"**Why it works**: `frappe.get_all` is the framework-idiomatic shape; "
	"same table, same fields, same LIMIT, so the result set is identical.\n"
	"**Verify**: diff the row count returned by the new call vs. the old "
	"`frappe.db.sql` and confirm they match.\n"
)


_STEPS_SYSTEM_PROMPT = (
	"You are a senior ERPNext / Frappe Framework functional + technical expert "
	" you know every standard ERPNext document flow cold and you know exactly "
	"which Desk UI gesture produces which HTTP call. Your job: write the "
	"\"Steps to Reproduce\" section of a performance report. You're given the "
	"ordered list of HTTP actions a user performed during a profiling session "
	"(a humanized label, the raw `cmd`/path, the DocType when known and how "
	"long each took). Infer what the user was actually DOING and rewrite it as "
	"clear, friendly steps a developer or QA could follow to reproduce the "
	"same flow in the Desk UI.\n\n"

	"WHAT THE RAW CALLS MEAN (use this to decode the trace):\n"
	"  • `frappe.desk.form.save.savedocs` / `frappe.client.save` / `.insert`: "
	"the user clicked **Save** on a form. If the action is \"Submit\" it was "
	"the **Submit** button; \"Cancel\" → **Cancel**; a new (`__islocal`) doc → "
	"they had clicked **New** first. `frappe.client.submit` / `.cancel` / "
	"`.delete` are the same buttons hit programmatically.\n"
	"  • `run_doc_method` / `runserverobj`: the user clicked a button on a "
	"form: a **Create ▸ <Target>** mapping (e.g. Sales Order → Delivery Note / "
	"Sales Invoice, Purchase Order → Purchase Receipt, Quotation → Sales "
	"Order), or a custom Action button. The humanized label tells you which "
	"(\"Make Delivery Note on Sales Order SO-0001\" → they clicked Create ▸ "
	"Delivery Note on that Sales Order).\n"
	"  • `frappe.model.workflow.apply_workflow`: the user clicked a **workflow "
	"action** button (Approve / Reject / Submit for Approval / …).\n"
	"  • `frappe.desk.search.search_link` / `frappe.client.get_list` from a "
	"form the user was typing into a Link field (picking a Customer, Item, "
	"etc.) that's part of \"fill in the form\", not its own step.\n"
	"  • `frappe.desk.reportview.get` / `frappe.client.get_count`: opening a "
	"**List view** of that DocType. `frappe.desk.query_report.run`: running a "
	"**Query/Script Report**. `frappe.desk.form.load.getdoc`: **opening an "
	"existing record**.\n\n"

	"ERPNEXT FLOWS YOU KNOW (recognise these chains and name them):\n"
	"  • Selling: Lead → Opportunity → Quotation → Sales Order → Delivery Note "
	"→ Sales Invoice → Payment Entry.\n"
	"  • Buying: Material Request → Request for Quotation → Supplier Quotation → "
	"Purchase Order → Purchase Receipt → Purchase Invoice → Payment Entry.\n"
	"  • Stock: Stock Entry (Material Receipt / Issue / Transfer / Manufacture), "
	"Stock Reconciliation, Pick List, Delivery Note, Purchase Receipt.\n"
	"  • Manufacturing: BOM → Work Order → Job Card → Stock Entry "
	"(Manufacture) → completion.\n"
	"  • Accounts: Journal Entry, Payment Entry, Sales/Purchase Invoice, "
	"Bank Reconciliation, Period Closing Voucher.\n"
	"  • HR/Payroll: Employee → Attendance / Leave Application → Salary "
	"Structure Assignment → Payroll Entry → Salary Slip.\n"
	"  • Projects: Project → Task → Timesheet → Sales Invoice.\n"
	"If the trace walks one of these, say so (\"Create a Sales Order from the "
	"Quotation, then make a Delivery Note from it\").\n\n"

	"RULES:\n"
	"  • Collapse mechanical multi-call sequences into ONE human step a form "
	"load + a few Link-field lookups + a save is just \"Create a Sales Invoice "
	"with a customer and at least one item, then Save\", not five steps. "
	"Background / polling calls (realtime permission checks, notification "
	"counts, list counters, asset loads, bare form-metadata loads) are noise "
	"ignore them entirely.\n"
	"  • Use Desk UI language: \"Go to the <DocType> list\", \"Click New\", "
	"\"Fill in <fields> and Save\", \"Submit it\", \"Open <DocType> <name>\", "
	"\"Click **Create ▸ <Target>**\", \"Click the <Action> button\", \"Run the "
	"<Report> report\". Name the DocType whenever you can tell what it was.\n"
	"  • Do NOT invent data you weren't given write \"with at least one item "
	"row\", not \"with item WIDGET-001\". If an action's purpose genuinely "
	"isn't clear from the label/cmd, describe it neutrally (\"Call the "
	"<method> endpoint on <DocType>\") rather than guessing a UI gesture.\n"
	"  • Keep it tight usually 2 to 6 steps. Don't restate timings; the "
	"report shows those separately. Don't add commentary about performance or "
	"what's slow.\n\n"

	"OUTPUT a Markdown ordered list of the steps to reproduce, then a blank "
	"line, then ONE sentence beginning \"**Summary:**\" that says what the "
	"session profiled (e.g. \"**Summary:** creating a Sales Order and then "
	"making a Delivery Note from it.\"). Nothing before the list, nothing "
	"after the summary line, no headings, no code fences."
)

_MAX_STEPS_ACTIONS = 60
_MAX_STEPS_USER_CHARS = 8000


_INDEX_SYSTEM_PROMPT = (
	"You are a senior Frappe Framework / ERPNext DBA reviewing index candidates "
	"for ONE database table flagged by a performance profiler. You're given the "
	"table, the columns the profiled session filtered / joined / ordered on (how "
	"often and which appeared together), a few of the actual queries and the "
	"table's CURRENT indexes (`SHOW INDEX` output). Recommend the SMALLEST set of "
	"indexes that actually helps almost always ONE composite, columns ordered "
	"equality-then-range-then-ORDER-BY, leftmost = the most selective / always-"
	"present one.\n\n"

	"RULES:\n"
	"  • If an existing index already covers a candidate as a leftmost prefix, do "
	"NOT recommend it say it's already covered.\n"
	"  • Never index Frappe's metadata columns (`name`, `creation`, `modified`, "
	"`modified_by`, `owner`, `parent`, `parentfield`, `parenttype`, `idx`, "
	"`docstatus`, …) they're written on every save or already indexed.\n"
	"  • Adding an index to a write-hot table (GL Entry, Stock Ledger Entry, Bin, "
	"Payment Ledger Entry, Serial and Batch Bundle, …) slows every submitted "
	"document in production only recommend it if a query that filters this way "
	"is genuinely slow and say so.\n"
	"  • Customize Form ▸ field ▸ Search Index makes only SINGLE-column indexes; a "
	"composite needs a patch with `frappe.db.add_index('<DocType>', "
	"['col_a', 'col_b'])`.\n\n"

	"OUTPUT Markdown, exactly these headings, nothing before or after:\n"
	"**Recommendation**: the one index to add (e.g. `(against_voucher_type, "
	"against_voucher_no)` on `GL Entry`), OR \"nothing the existing indexes "
	"already cover these read patterns\".\n"
	"**Why**: 1-2 sentences tying it to the queries / explaining the column order.\n"
	"**How to add**: the `frappe.db.add_index(\"<DocType>\", [\"col_a\", "
	"\"col_b\"])` patch line (for a single column you may instead say Customize "
	"Form ▸ field ▸ Search Index). Omit this heading entirely if the "
	"Recommendation is \"nothing\".\n"
	"**Skip**: one line per candidate column or combo you're NOT recommending and "
	"why (already covered by `<index name>` / a Frappe metadata column / not worth "
	"the write cost). If there's nothing to skip, write \"None\".\n\n"

	"Keep it tight roughly 120-300 words. Don't restate the table's read/write "
	"numbers back at the reader."
)

_MAX_INDEX_SAMPLE_QUERIES = 4
_MAX_INDEX_USER_CHARS = 10000


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# v0.6.x: per-section "use the LLM for X" toggle → the config attribute.
_AI_SECTION_FLAGS = {
	"findings": "ai_suggest_findings",
	"indexes": "ai_suggest_indexes",
	"humanize": "ai_humanize_steps",
}


def is_finding_type_excluded(finding_type: str | None) -> bool:
	"""Return True when ``finding_type`` is in ``cfg.ai_excluded_finding_types``.

	Exact case-sensitive match. Empty / unknown type, or any read error (no
	bench, settings cache wedged), returns False so an inert exclude never
	blocks by accident.
	"""
	if not finding_type or not isinstance(finding_type, str):
		return False
	try:
		from optimus.settings import get_config
		excluded = get_config().ai_excluded_finding_types
	except Exception:
		return False
	return finding_type in (excluded or ())


def _resolve_timeout_seconds() -> int:
	"""Return the configured HTTP timeout (seconds) for outbound LLM calls,
	clamped to ``[10, 600]`` and falling back to :data:`_HTTP_TIMEOUT` when
	settings can't be read.
	"""
	try:
		from optimus.settings import get_config
		v = get_config().ai_request_timeout_seconds
		return max(10, min(600, int(v or _HTTP_TIMEOUT)))
	except Exception:
		return _HTTP_TIMEOUT


def is_available(section: str | None = None) -> bool:
	"""True when AI fix suggestions are on and minimally configured:
	``ai_enabled`` set, a model resolvable for the chosen provider and an API
	key present unless the provider needs none (local endpoints).

	When ``section`` is ``"findings"`` / ``"indexes"`` / ``"humanize"``, also
	requires the matching per-section toggle. Fails soft: an unknown ``section``
	or an unreadable config attr does not block once ``ai_enabled`` has passed."""
	try:
		from optimus.settings import get_config
		cfg = get_config()
	except Exception:
		return False
	if not getattr(cfg, "ai_enabled", False):
		return False
	try:
		provider = _resolve_provider()
	except AiFixError:
		return False
	if not provider.get("model") or not provider.get("base_url"):
		return False
	if provider.get("needs_key") and not provider.get("has_key"):
		return False
	if section:
		flag = _AI_SECTION_FLAGS.get(section)
		if flag and not getattr(cfg, flag, True):
			return False
	return True


def _resolve_display_threshold_ms() -> float:
	"""The configured "render durations in seconds above (ms)" threshold, so the
	durations in AI-fix context read in the same unit as the report. Delegates to
	the single resolver in settings (lazy import keeps this module's pure prompt /
	HTTP layer importable without frappe)."""
	from optimus.settings import display_threshold_ms
	return display_threshold_ms()


def suggest_fix(finding: dict) -> dict:
	"""Ask the configured LLM for a fix for ``finding``.

	``finding`` is the shape produced by ``renderer._finding_to_dict`` plus an
	optional ``source_window`` (``[{lineno, content, is_target}]``) the caller
	gathered around the callsite.

	Returns ``{"suggestion": <markdown>, "model", "provider", "generated_at",
	"source_available"}`` (plus ``tokens`` when the provider reports usage).
	``source_available`` is False when the LLM got neither a source window nor a
	SQL statement, so the UI can mark the result directional. Raises
	``AiFixError`` on a config / network / auth / rate-limit problem or an empty
	response; when the finding's type is in ``ai_excluded_finding_types`` it
	raises immediately, before any request leaves the host.
	"""
	if is_finding_type_excluded(finding.get("finding_type")):
		raise AiFixError("excluded by ai_excluded_finding_types")
	provider = _resolve_provider()
	if not provider.get("model"):
		raise AiFixError(
			"No AI model is configured set 'Model' under Optimus Settings ▸ "
			"AI Fix Suggestions."
		)
	if not provider.get("base_url"):
		raise AiFixError(
			"No AI base URL is configured set 'Base URL' under Profiler "
			"Settings ▸ AI Fix Suggestions."
		)
	if provider.get("needs_key") and not provider.get("has_key"):
		raise AiFixError(
			"No API key is configured for this AI provider set it under "
			"Optimus Settings ▸ AI Fix Suggestions."
		)
	system, messages = _build_messages(finding, threshold_ms=_resolve_display_threshold_ms())

	usage: dict = {}
	if provider["protocol"] == "anthropic":
		text = _call_anthropic(
			provider["base_url"], _get_api_key(),
			provider["model"], system, messages, usage_out=usage,
		)
	else:
		text = _call_openai_chat(
			provider["base_url"], _get_api_key(),
			provider["model"], system, messages, usage_out=usage,
			metadata=_aerele_call_metadata(provider, finding.get("finding_type")),
		)

	text = (text or "").strip()
	if not text:
		raise AiFixError("The AI provider returned an empty response.")
	text = _flag_metadata_column_index_advice(text)
	text = _flag_raw_sql_in_fix(text)

	result = {
		"suggestion": text,
		"model": provider["model"],
		"provider": provider["name"],
		"generated_at": datetime.now(timezone.utc).isoformat(),
		"source_available": _had_concrete_context(finding),
	}
	# Token usage as the provider reported it (the Aerele managed proxy
	# forwards the upstream usage verbatim, so for that path it's the real
	# billed count). Omitted when the provider returns no usage block.
	if usage.get("total_tokens"):
		result["tokens"] = usage
	return result


def humanize_steps(
	actions: list[dict], *, session_title: str | None = None, usage_out: dict | None = None
) -> str:
	"""Ask the configured LLM to turn the recorded actions into a friendly
	"Steps to Reproduce" narrative (Markdown). ``actions`` is a list of
	``{label, cmd, path, method, doctype, duration_ms}`` dicts (best-effort
	missing keys are fine). Raises ``AiFixError`` on a config / network
	problem or an empty response."""
	if not actions:
		raise AiFixError("There are no recorded actions to summarise.")
	provider = _resolve_provider()
	if not provider.get("model") or not provider.get("base_url"):
		raise AiFixError(
			"AI is not fully configured set the provider, model and base URL "
			"under Optimus Settings ▸ AI Fix Suggestions."
		)
	if provider.get("needs_key") and not provider.get("has_key"):
		raise AiFixError("No API key is configured for this AI provider.")
	system, messages = _build_steps_messages(
		actions, session_title, threshold_ms=_resolve_display_threshold_ms()
	)
	if provider["protocol"] == "anthropic":
		text = _call_anthropic(
			provider["base_url"], _get_api_key(),
			provider["model"], system, messages, usage_out=usage_out,
		)
	else:
		text = _call_openai_chat(
			provider["base_url"], _get_api_key(),
			provider["model"], system, messages, usage_out=usage_out,
			metadata=_aerele_call_metadata(provider, "Steps to Reproduce"),
		)
	text = (text or "").strip()
	if not text:
		raise AiFixError("The AI provider returned an empty response.")
	return text


def suggest_index(table_payload: dict) -> dict:
	"""Ask the configured LLM to vet/refine an index recommendation for one
	table. ``table_payload`` keys: ``table`` / ``doctype`` / ``read_count`` /
	``write_count`` / ``is_write_hot`` / ``recommended_index`` (the heuristic
	pick) / ``candidates`` (column→clauses→hits) / ``framework_cols_filtered`` /
	``existing_indexes`` (``[{name, columns, unique}]`` from SHOW INDEX) /
	``sample_queries``. Returns ``{"suggestion": <markdown>, "model", "provider",
	"generated_at"}``. Raises ``AiFixError`` on a config / network problem or an
	empty response."""
	if not table_payload or not table_payload.get("table"):
		raise AiFixError("No table to analyse for an index suggestion.")
	provider = _resolve_provider()
	if not provider.get("model") or not provider.get("base_url"):
		raise AiFixError(
			"AI is not fully configured set the provider, model and base URL "
			"under Optimus Settings ▸ AI Fix Suggestions."
		)
	if provider.get("needs_key") and not provider.get("has_key"):
		raise AiFixError("No API key is configured for this AI provider.")
	system, messages = _build_index_messages(table_payload)
	usage: dict = {}
	if provider["protocol"] == "anthropic":
		text = _call_anthropic(
			provider["base_url"], _get_api_key(),
			provider["model"], system, messages, usage_out=usage,
		)
	else:
		text = _call_openai_chat(
			provider["base_url"], _get_api_key(),
			provider["model"], system, messages, usage_out=usage,
			metadata=_aerele_call_metadata(provider, "Table Index"),
		)
	text = (text or "").strip()
	if not text:
		raise AiFixError("The AI provider returned an empty response.")
	# Same guardrail as suggest_fix: if the model recommended indexing a Frappe
	# metadata column, append a correction note. Plus the raw-SQL guardrail
	# the index-suggestion path doesn't usually emit code, but a model can
	# still volunteer a ``frappe.db.sql("ALTER ...")`` fallback that should
	# be flagged (DDL verbs are excluded from the detector anyway, so this
	# guard fires only on the broader anti-pattern).
	text = _flag_metadata_column_index_advice(text)
	text = _flag_raw_sql_in_fix(text)
	result = {
		"suggestion": text,
		"model": provider["model"],
		"provider": provider["name"],
		"generated_at": datetime.now(timezone.utc).isoformat(),
	}
	if usage.get("total_tokens"):
		result["tokens"] = usage
	return result


def _had_concrete_context(finding: dict) -> bool:
	"""True when the LLM was given something concrete to reason about a
	source window / snippet, or a SQL statement. ``False`` means it only had
	the finding's title + numbers, so the suggestion is necessarily
	directional (and the UI should say so)."""
	detail = (finding.get("technical_detail") or {})
	callsite = detail.get("callsite") or {}
	if finding.get("source_window") or callsite.get("source_snippet"):
		return True
	if finding.get("phase2_hotline"):
		return True  # has the per-line numbers even if the source couldn't be read
	return bool(detail.get("normalized_query") or detail.get("example_queries"))


def test_connection() -> dict:
	"""Send a tiny probe to the configured provider. Returns
	``{"ok": bool, "message": str, "model": str}``: never raises (the
	failure detail goes in ``message``)."""
	try:
		provider = _resolve_provider()
	except AiFixError as e:
		return {"ok": False, "message": str(e), "model": ""}

	if not provider.get("model") or not provider.get("base_url"):
		return {
			"ok": False,
			"message": "Provider/model/base URL not fully configured.",
			"model": provider.get("model") or "",
		}
	if provider.get("needs_key") and not provider.get("has_key"):
		return {"ok": False, "message": "No API key configured.", "model": provider["model"]}

	messages = [{"role": "user", "content": "Reply with exactly: OK"}]
	usage: dict = {}
	try:
		if provider["protocol"] == "anthropic":
			text = _call_anthropic(
				provider["base_url"], _get_api_key(),
				provider["model"], "You are a connectivity probe. Reply with exactly: OK",
				messages, max_tokens=16, usage_out=usage,
			)
		else:
			text = _call_openai_chat(
				provider["base_url"], _get_api_key(),
				provider["model"], "You are a connectivity probe. Reply with exactly: OK",
				messages, max_tokens=16, usage_out=usage,
			)
	except AiFixError as e:
		return {"ok": False, "message": str(e), "model": provider["model"]}

	# A connectivity probe is a Settings test, not session work show its
	# count here, but it does NOT roll into a session's token total.
	_toks = usage.get("total_tokens") or 0
	return {
		"ok": True,
		"message": (
			f"Reachable. Model replied: {(text or '').strip()[:60]!r}"
			+ (f" ({_toks} tokens)" if _toks else "")
		),
		"model": provider["model"],
	}


# ---------------------------------------------------------------------------
# Config / provider resolution
# ---------------------------------------------------------------------------

def _current_key_or_empty() -> str:
	"""The stored ``Optimus Settings.ai_api_key``, stripped, or ``""`` when it
	is unset or cannot be decrypted. Never raises and never validates, so
	``log_ai_failure`` can scrub an echoed key even when ``_get_api_key``
	would reject it.

	SECURITY: the value only ever lives in a local named ``api_key`` (Frappe's
	traceback sanitizer and Sentry's denylist both redact that name) and in
	an ``_ApiKeyAuth``. Never put it in a dict, a header dict, a request body
	or an exception message.

	An RQ job timeout is never swallowed (the job must stop, and answering
	"" would send the request unauthenticated): it leaves as a fresh
	instance raised after the ``try``, so the decrypt frames it interrupted
	(which hold the key bytes) never reach ``execute_job``'s log."""
	interrupt = None
	try:
		from frappe.utils.password import get_decrypted_password

		api_key = get_decrypted_password(
			"Optimus Settings", "Optimus Settings", "ai_api_key",
			raise_exception=False,
		) or ""
	except _job_timeout_types() as e:
		interrupt = (type(e), e.args)
	except Exception:
		return ""
	if interrupt is not None:
		raise interrupt[0](*interrupt[1])
	return api_key.strip() if isinstance(api_key, str) else ""


def _get_api_key() -> str:
	"""The API key to send, stripped of surrounding whitespace (a pasted
	trailing newline), or ``""`` when none is stored.

	Raises ``AiFixError(kind="config")`` before any HTTP call when the key
	cannot be sent in an HTTP header: a character outside latin-1 (usually a
	pasted smart quote), or a control character such as an internal newline,
	tab or NUL (which would otherwise reach ``requests``/``http.client`` and
	surface the key in a ``ValueError`` message or in ``putheader`` locals).

	Both checks only ever set a local bool inside their ``except``/loop; the
	``AiFixError`` is raised after the ``try``, once no exception is being
	handled, so it has no ``__context__`` pointing at the
	``UnicodeEncodeError`` (whose ``object`` attribute holds the key).
	``from None`` also keeps ``__suppress_context__`` explicit."""
	api_key = _current_key_or_empty()
	if not api_key:
		return ""
	try:
		api_key.encode("latin-1")
		encodable = True
	except UnicodeEncodeError:
		encodable = False
	has_control_char = any(ch < "\x20" or ch == "\x7f" for ch in api_key)
	if not encodable or has_control_char:
		from frappe import _

		raise AiFixError(
			_("The AI API key in Optimus Settings contains a character that cannot be sent in an HTTP header (often a pasted smart quote or a stray control character such as a newline or tab). Paste the key again."),
			kind="config",
		) from None
	return api_key


class _ApiKeyAuth(requests.auth.AuthBase):
	"""Attaches the API key header at send time, so no headers dict ever holds
	the key. ``repr``/``str`` are masked because Frappe's with-context
	tracebacks, RQ failure logs and Sentry all print frame locals by repr."""

	__slots__ = ("_header", "_value", "_prefix")

	def __init__(self, header: str, value: str, prefix: str = ""):
		self._header = header
		self._value = value
		self._prefix = prefix

	def __call__(self, r):
		r.headers[self._header] = self._prefix + self._value
		return r

	def __repr__(self) -> str:
		return f"<_ApiKeyAuth {self._header}: ********>"

	__str__ = __repr__


def _resolve_provider() -> dict:
	"""Resolve the active provider config: protocol, base_url, model,
	needs_key, has_key and the provider display name. Raises
	``AiFixError`` on an unknown provider or a custom provider missing its
	required base_url/model.

	SECURITY: the dict carries ``has_key`` (bool), never the key itself: it is
	a local in most AI frames, and Frappe's traceback sanitizer
	(``frappe.utils._get_traceback_sanitizer``) only redacts a dict key named
	exactly ``password``, ``passwd``, ``secret``, ``token``, ``key`` or
	``pwd``; ``api_key`` is not one of them. Code that sends a request calls
	``_get_api_key()`` at the call site.
	"""
	from optimus.settings import get_config
	cfg = get_config()
	name = (getattr(cfg, "ai_provider", "") or _DEFAULT_PROVIDER).strip()
	if name not in _PROVIDER_DEFAULTS:
		raise AiFixError(f"Unknown AI provider {name!r}. Pick one in Optimus Settings.")

	defaults = _PROVIDER_DEFAULTS[name]
	# The Base URL override applies ONLY to bring-your-own providers (those
	# with no built-in default endpoint i.e. "OpenAI-compatible"). Hosted
	# providers (Anthropic / OpenAI / Kimi / DeepSeek) ALWAYS use their default: the
	# Settings field is hidden for them, so a previously-stored value must not
	# silently override and route calls to a dead host (that stale-value trap
	# caused a ConnectionError after the field was hidden for hosted providers).
	if defaults["base_url"]:
		base_url = defaults["base_url"]
	else:
		base_url = (getattr(cfg, "ai_base_url", "") or "").strip().rstrip("/")
	model = (getattr(cfg, "ai_model", "") or "").strip() or defaults["model"]

	# A key may be set for any provider: some OpenAI-compatible routers
	# (OpenRouter, Together, Groq) need one even though local endpoints don't.
	# Only its presence is recorded here.
	return {
		"name": name,
		"protocol": defaults["protocol"],
		"base_url": base_url,
		"model": model,
		"needs_key": bool(defaults["needs_key"]),
		"has_key": bool(_current_key_or_empty()),
	}


# ---------------------------------------------------------------------------
# Output guardrail never let an "index a metadata column" recommendation
# through, even if the model ignored the system prompt. Frappe metadata
# columns (`name`, `idx`, `parent`, `creation`, `modified`, `docstatus`, …)
# are written on every save (or already indexed), so indexing them is a
# write-cost trap; the profiler never suggests it anywhere including here.
# ---------------------------------------------------------------------------

# "add an index on <col>", "Search Index … <col>", "index the <col> column",
# "ADD INDEX (`<col>`)" captures the column token that follows the
# index-action phrase, skipping connector words ("on", "the", …). The hit is
# discarded if it's negated ("do NOT index …") see `_NEGATION_RE`.
_INDEX_ADVICE_RE = re.compile(
	r"(?:add\s+(?:an?\s+)?index|search\s+index|index)\b"
	r"[\s(]*(?:(?:on|the|a|an|for|to|of|column|field)\s+)*"
	r"[`'\"]?(?P<col>[A-Za-z_][\w]*)",
	re.IGNORECASE,
)
_NEGATION_RE = re.compile(r"(?:not|n['’]t|never|avoid|without|no need to|don['’]t)\W*$", re.IGNORECASE)


def _metadata_columns() -> frozenset:
	"""The Frappe standard-metadata column set, from the analyzer base
	module (single source of truth). Empty set if unimportable the
	guardrail then simply does nothing."""
	try:
		from optimus.analyzers.base import FRAPPE_METADATA_COLUMNS
		return FRAPPE_METADATA_COLUMNS
	except Exception:
		return frozenset()


# ---------------------------------------------------------------------------
# Output guardrail: raw `frappe.db.sql(...)` in suggested fix code
# ---------------------------------------------------------------------------
# The system prompt at the top of this module tells the LLM "never hand-built
# SQL strings" and lists ``frappe.get_all`` / ``frappe.get_list`` /
# ``frappe.db.get_value`` / ``frappe.db.get_values`` / ``frappe.qb`` as the
# idiomatic alternatives. The few-shot examples reinforce that. But a
# sufficiently confident model still occasionally leaks raw SQL into its
# proposed fix code and the system-prompt instruction alone is a soft
# nudge with no backstop.
#
# This guardrail mirrors ``_flag_metadata_column_index_advice``: detect the
# anti-pattern in the LLM's output, append a clearly-marked profiler note,
# never rewrite (markdown is fragile). The note is advisory, not blocking
# a fix that legitimately needs raw SQL (DDL, vendor-specific MariaDB
# extensions) can be acted on with the operator's judgement.

# ``frappe.db.sql(…, "SELECT …"…)``. The literal can be a regular string,
# f-string, or raw string; the verb that follows is case-insensitive. The
# verbs covered are the ones a model is most likely to suggest as a "fix"
# (DDL like CREATE / ALTER is intentionally outside the scope those are
# legit administrative paths and the prompt already rarely produces them).
_RAW_SQL_IN_FIX_RE = re.compile(
	# ``[a-z]{0,2}`` allows any string prefix (f / r / b / rb / br / fr …);
	# ``["\']{1,3}`` covers single- AND triple-quoted literals (the common shape
	# for a multi-line "fix" query). ``WITH`` catches CTE-led SELECTs.
	r'frappe\.db\.sql\s*\(\s*[a-z]{0,2}["\']{1,3}\s*'
	r'(?:WITH|SELECT|INSERT|UPDATE|DELETE|REPLACE)\b',
	re.IGNORECASE,
)

# Multi-line opener: ``frappe.db.sql("""`` (triple-quoted query whose verb is on
# a later line). A multi-line frappe.db.sql is essentially always a hand-built
# query, so flag the opener regardless of the (off-line) verb.
_RAW_SQL_OPENER_RE = re.compile(
	r'frappe\.db\.sql\s*\(\s*[a-z]{0,2}(?:"""|\'\'\')',
	re.IGNORECASE,
)

# Markdown code-fence detector. Group 1 captures the info-string
# (``diff`` / ``python`` / ``py`` / empty for un-tagged fences).
_CODE_FENCE_RE = re.compile(r'^```(\w*)\s*$')


def _flag_raw_sql_in_fix(text: str) -> str:
	"""If the model's proposed fix contains a raw ``frappe.db.sql(...)`` with a
	SELECT / INSERT / UPDATE / DELETE / REPLACE literal, append a correction
	note (never rewrites); returns the text unchanged when clean.

	Scope: only inside markdown code fences (prose mentions are ignored); inside
	a ``diff`` block only addition (``+``) lines count (removal lines are the
	before-code). Only ``frappe.db.sql`` is detected; DDL verbs (CREATE / ALTER /
	DROP) are excluded since raw DDL is sometimes the right answer.
	"""
	if not text:
		return text

	flagged = False
	in_fence = False
	fence_kind = ""
	for line in text.splitlines():
		fence_match = _CODE_FENCE_RE.match(line.strip())
		if fence_match:
			if not in_fence:
				in_fence = True
				fence_kind = (fence_match.group(1) or "").lower()
			else:
				in_fence = False
				fence_kind = ""
			continue
		if not in_fence:
			continue

		# Inside a code block. Diff blocks restrict scanning to addition
		# lines; non-diff blocks scan every line.
		if fence_kind == "diff":
			if not line.startswith("+") or line.startswith("+++"):
				continue
			# Strip the leading "+" so the regex sees actual code, not
			# the diff-marker prefix.
			line_to_scan = line[1:]
		else:
			line_to_scan = line

		# Two detectors: the verb-anchored one (single-line ``frappe.db.sql("SELECT
		# …")``) and a multi-line OPENER (``frappe.db.sql("""`` with the SQL verb
		# on a following line the common multi-line shape this line-by-line scan
		# would otherwise miss).
		if _RAW_SQL_IN_FIX_RE.search(line_to_scan) or _RAW_SQL_OPENER_RE.search(line_to_scan):
			flagged = True
			break

	if not flagged:
		return text

	return text.rstrip() + (
		"\n\n> **Profiler note:** the fix above includes a raw "
		"`frappe.db.sql(\"SELECT …\")` call. The recommended Frappe pattern "
		"is `frappe.get_all` / `frappe.get_list` / `frappe.db.get_value` / "
		"`frappe.db.get_values` (Document API for typical reads) or "
		"`frappe.qb` (query builder for joins / aggregations / dynamic "
		"conditions). Use raw SQL only when none of those API surfaces fit "
		"(rare e.g. DDL, vendor-specific MariaDB extensions)."
	)


def _flag_metadata_column_index_advice(text: str) -> str:
	"""If the model recommended indexing a Frappe metadata column, append a
	correction note. We don't rewrite the body (markdown is fragile) we
	add a clearly-marked profiler note so the reader doesn't act on it."""
	meta = _metadata_columns()
	if not meta or not text:
		return text
	hits = []
	for m in _INDEX_ADVICE_RE.finditer(text):
		col = m.group("col").strip("`'\"() ").lower()
		if col not in meta or col in hits:
			continue
		# Skip negated mentions ("do NOT index `modified`") no correction needed.
		if _NEGATION_RE.search(text[max(0, m.start() - 16):m.start()]):
			continue
		hits.append(col)
	if not hits:
		return text
	cols = ", ".join(f"`{c}`" for c in hits)
	plural = len(hits) > 1
	return text.rstrip() + (
		"\n\n> **Profiler note:** disregard any suggestion above to index "
		+ cols
		+ (" these are Frappe framework-managed columns" if plural
		   else " that is a Frappe framework-managed column")
		+ " (Frappe writes "
		+ ("them" if plural else "it")
		+ " on every save, or "
		+ ("they're" if plural else "it's")
		+ " already indexed). Index a business column from the WHERE / JOIN "
		"instead, or change the query shape."
	)


# ---------------------------------------------------------------------------
# Prompt construction (pure)
# ---------------------------------------------------------------------------

def _truncate(text: Any, limit: int) -> str:
	s = "" if text is None else str(text)
	if len(s) <= limit:
		return s
	return s[:limit] + "\n…(truncated)"


def _build_steps_messages(
	actions: list[dict], session_title: str | None, *, threshold_ms: float = 1000.0
) -> tuple[str, list[dict]]:
	"""Build ``(system_prompt, [user_message])`` for the Steps-to-Reproduce
	humanizer. Pure no Frappe, no I/O. ``actions`` items use the keys
	``label`` / ``cmd`` / ``path`` / ``method`` / ``doctype`` /
	``duration_ms`` (all optional). ``threshold_ms`` is the report's
	seconds-rollover threshold so the model sees durations in the report's unit."""
	lines: list[str] = []
	title = (str(session_title).strip() if session_title else "")
	if title:
		lines.append(f"Session title (what the user named this run): {title}")
		lines.append("")
	lines.append("Recorded actions, in order:")
	for i, a in enumerate(actions[:_MAX_STEPS_ACTIONS], 1):
		label = (a.get("label") or "").strip() or "(unnamed action)"
		bits: list[str] = []
		cmd = (a.get("cmd") or "").strip()
		if cmd:
			bits.append(f"cmd={cmd}")
		else:
			endpoint = " ".join(p for p in (
				(a.get("method") or "").strip(), (a.get("path") or "").strip(),
			) if p)
			if endpoint:
				bits.append(endpoint)
		doctype = (a.get("doctype") or "").strip()
		if doctype:
			bits.append(f"doctype={doctype}")
		dur = a.get("duration_ms")
		if dur:
			try:
				bits.append(humanize_duration_ms(float(dur), threshold_ms=threshold_ms))
			except (TypeError, ValueError):
				pass
		suffix = f"  ({'; '.join(bits)})" if bits else ""
		lines.append(f"{i}. {label}{suffix}")
	extra = len(actions) - _MAX_STEPS_ACTIONS
	if extra > 0:
		lines.append(f"… and {extra} more action(s).")
	content = _truncate("\n".join(lines), _MAX_STEPS_USER_CHARS)
	return _STEPS_SYSTEM_PROMPT, [{"role": "user", "content": content}]


def _build_index_messages(payload: dict) -> tuple[str, list[dict]]:
	"""Build ``(system_prompt, [user_message])`` for the per-table index
	suggestion. Pure no Frappe, no I/O. See ``suggest_index`` for the
	``payload`` shape."""
	t = payload.get("table") or "?"
	dt = (payload.get("doctype") or "").strip()
	parts: list[str] = []
	parts.append(f"Table: `{t}`" + (f"  (DocType: \"{dt}\")" if dt else ""))
	rc = int(payload.get("read_count") or 0)
	wc = int(payload.get("write_count") or 0)
	parts.append(f"This profiling session: {rc} read(s), {wc} write(s) on this table.")
	if payload.get("is_write_hot"):
		parts.append(
			"This is a write-hot core table in production it takes many "
			"INSERT/UPDATE rows per submitted document."
		)
	rec = payload.get("recommended_index") or {}
	if rec.get("columns"):
		parts.append(
			"Profiler's heuristic pick (most-used filter combination): ("
			+ ", ".join(rec["columns"])
			+ f") those columns were filtered together in {int(rec.get('together_count') or 0)} of {rc} read(s)."
		)
	cands = payload.get("candidates") or []
	if cands:
		parts.append(
			"Columns this session filtered / joined / ordered on (shown as column: clauses (count)):\n"
			+ "\n".join(
				f"  - {c.get('column')}: {', '.join(c.get('sources') or [])} ({int(c.get('hits') or 0)}×)"
				for c in cands
			)
		)
	fw = payload.get("framework_cols_filtered") or []
	if fw:
		parts.append("Also filtered on Frappe metadata columns (do NOT index): " + ", ".join(fw))
	ex = payload.get("existing_indexes") or []
	if ex:
		parts.append(
			"CURRENT indexes on this table (from `SHOW INDEX`):\n"
			+ "\n".join(
				f"  - {i.get('name')}: (" + ", ".join(i.get("columns") or []) + ")"
				+ (" UNIQUE" if i.get("unique") else "")
				for i in ex
			)
		)
	else:
		parts.append(
			f"CURRENT indexes on this table: not available be cautious about "
			f"redundancy; the operator should run `SHOW INDEX FROM `{t}`` to check."
		)
	sq = payload.get("sample_queries") or []
	if sq:
		shown = [_truncate(q, _MAX_QUERY_CHARS) for q in sq[:_MAX_INDEX_SAMPLE_QUERIES]]
		parts.append("A few of the actual read queries:\n```sql\n" + "\n---\n".join(shown) + "\n```")
	content = _truncate("\n\n".join(p for p in parts if p).strip(), _MAX_INDEX_USER_CHARS)
	return _INDEX_SYSTEM_PROMPT, [{"role": "user", "content": content}]


def _build_messages(finding: dict, *, threshold_ms: float = 1000.0) -> tuple[str, list[dict]]:
	"""Build ``(system_prompt, [user_message])`` from a finding dict.

	Pure no Frappe, no I/O. ``threshold_ms`` is the report's seconds-rollover
	threshold, so every duration handed to the model reads in the same unit as
	the report the operator is looking at. ``finding`` keys used: ``finding_type``,
	``severity``, ``title``, ``customer_description``, ``estimated_impact_ms``,
	``affected_count``, ``technical_detail`` (``callsite``, ``function``,
	``cumulative_ms``, ``action_wall_time_ms``, ``normalized_query``,
	``suggested_ddl``, ``explain_row``, ``fix_hint``, ``validation_note``,
	``example_queries``), ``source_window`` (``[{lineno, content, is_target}]``),
	and ``phase2_hotline`` (``{lineno, content, total_ms, hits}``: the hottest
	line from a Phase-2 line-profile pass over the finding's function).
	"""
	detail = finding.get("technical_detail") or {}
	callsite = detail.get("callsite") or {}

	parts: list[str] = []
	ftype = finding.get("finding_type") or "Unknown"
	parts.append(f"Finding type: {ftype}")
	type_hint = _finding_type_hint(ftype)
	if type_hint:
		parts.append(f"What this finding type means / how it's usually fixed in Frappe: {type_hint}")
	parts.append(f"Severity: {finding.get('severity') or 'Unknown'}")
	# The title / description are read from stored finding rows (may predate dur()
	# markers), so format them through format_durations (markers + prose fallback)
	# with the report's threshold, matching the report's findings path, so the model
	# reads "5.23s" just like the report shows. Lazy import keeps this frappe-free.
	from optimus.analyzers.base import format_durations
	if finding.get("title"):
		parts.append(f"Title: {format_durations(finding['title'], threshold_ms)}")
	if finding.get("customer_description"):
		parts.append(
			f"Description: {format_durations(finding['customer_description'], threshold_ms)}"
		)
	impact = finding.get("estimated_impact_ms")
	if impact:
		parts.append(f"Estimated impact: ~{humanize_duration_ms(float(impact), threshold_ms=threshold_ms)}")
	if finding.get("affected_count"):
		parts.append(f"Affected occurrences: {finding['affected_count']}")

	had_callsite = bool(callsite.get("filename") and callsite.get("lineno") is not None)
	if had_callsite:
		fn = f" ({callsite['function']})" if callsite.get("function") else ""
		parts.append(
			f"Callsite (the closest non-framework frame to the cost the "
			f"offending loop/call may be in a function this points into, not "
			f"necessarily AT this line): {callsite['filename']}:{callsite['lineno']}{fn}"
		)

	# For call-tree (hot-path) findings: name the hot function + its share of
	# the action's time, so the model knows exactly which function's body
	# (shown below) to look at.
	hot_fn = (detail.get("function") or "").strip()
	cum_ms = detail.get("cumulative_ms")
	wall_ms = detail.get("action_wall_time_ms")
	if hot_fn and cum_ms:
		share = ""
		try:
			if wall_ms:
				share = f", {round(float(cum_ms) / float(wall_ms) * 100)}% of this action's {humanize_duration_ms(float(wall_ms), threshold_ms=threshold_ms)} wall time"
		except (TypeError, ValueError, ZeroDivisionError):
			share = ""
		parts.append(
			f"Hot function (the call-tree subtree that dominates this action): "
			f"`{hot_fn}`: ~{humanize_duration_ms(float(cum_ms), threshold_ms=threshold_ms)}{share}. Its source is below; "
			"point at the specific lines/loop/call inside it that cost the time."
		)

	window = finding.get("source_window") or callsite.get("source_snippet") or []
	if window:
		lines_out: list[str] = []
		for sl in window[:_MAX_SOURCE_WINDOW_LINES]:
			marker = ">> " if sl.get("is_target") else "   "
			lines_out.append(f"{marker}{sl.get('lineno')}: {sl.get('content', '')}")
		parts.append(
			"Source around the callsite THIS IS THE ONLY CODE YOU HAVE; any "
			"\"before\" snippet / diff `-` line in your answer must be copied "
			"verbatim from here, with its line number. `>>` marks the callsite "
			"line. This is a window, not necessarily the whole function if the "
			"loop/call this finding is about isn't in these lines, say so and "
			"give a directional fix only (no diff):\n```python\n"
			+ "\n".join(lines_out) + "\n```"
		)
	elif had_callsite:
		parts.append(
			"Source around the callsite: NOT AVAILABLE the profiler couldn't "
			"read this file, so you have NO source code for this finding. Do not "
			"write a before/after snippet or a diff (you'd be inventing the "
			"\"before\"). Give a short directional fix only, framed as \"without "
			"seeing the code, the likely fix is …\"."
		)

	hot = finding.get("phase2_hotline") or {}
	if isinstance(hot, dict) and hot.get("lineno") is not None:
		hl_content = str(hot.get("content") or "").strip()
		hl_ms = hot.get("total_ms") or 0
		hl_hits = hot.get("hits") or 0
		parts.append(
			f"Line-profile (Phase 2) over this function found its hottest line is "
			f"line {hot['lineno']}"
			+ (f" `{hl_content}`" if hl_content else "")
			+ (f" ({humanize_duration_ms(float(hl_ms), threshold_ms=threshold_ms)}" + (f" over {int(hl_hits)} call(s)" if hl_hits else "") + ")"
			   if hl_ms else "")
			+ ". Start your fix there."
		)

	if detail.get("normalized_query"):
		parts.append("Query (normalized):\n```sql\n"
			+ _truncate(detail["normalized_query"], _MAX_QUERY_CHARS) + "\n```")
	if detail.get("suggested_ddl"):
		parts.append("Profiler's suggested DDL:\n```sql\n"
			+ _truncate(detail["suggested_ddl"], _MAX_QUERY_CHARS) + "\n```")
	if detail.get("explain_row"):
		parts.append(f"EXPLAIN row: {_truncate(detail['explain_row'], 800)}")
	if detail.get("fix_hint"):
		parts.append(f"Profiler's static fix hint: {detail['fix_hint']}")
	if detail.get("validation_note"):
		parts.append(f"Note: {detail['validation_note']}")
	examples = detail.get("example_queries") or []
	if examples:
		shown = [_truncate(q, _MAX_QUERY_CHARS) for q in examples[:2]]
		parts.append("Example affected queries:\n```sql\n" + "\n---\n".join(shown) + "\n```")

	content = "\n\n".join(p for p in parts if p).strip()
	content = _truncate(content, _MAX_USER_CONTENT_CHARS)
	return _SYSTEM_PROMPT, [{"role": "user", "content": content}]


_REASONING_MODEL_RE = re.compile(r"^o[0-9]")  # OpenAI o1/o3/o4… reject `temperature`


def _is_reasoning_model(model: str) -> bool:
	return bool(_REASONING_MODEL_RE.match((model or "").strip().lower()))


# ---------------------------------------------------------------------------
# HTTP layer (uses `requests`; `frappe` only for best-effort logging)
# ---------------------------------------------------------------------------

_LOGGED_ATTR = "_optimus_ai_logged"


def log_ai_failure(
	title: str,
	exc: BaseException | None = None,
	*,
	session_uuid: str | None = None,
	docname: str | None = None,
	**context,
) -> bool:
	"""Write one Error Log row for an AI-surface failure. This is the ONLY
	function on the AI surface allowed to call ``frappe.log_error``
	(``test_ai_log_audit.py`` enforces it).

	Call it OUTSIDE any ``except`` block: record the exception in the
	handler and log after the ``try``. ``frappe.log_error`` calls Sentry's
	``capture_exception``, which ships the ACTIVE exception's frame locals
	even when a message is passed (the audit enforces this too).

	The message is explicit: ``title``, the session, ``context`` as ``k=v``
	lines and the plain traceback of ``exc`` (code lines only: no frame
	locals, no exception chain), passed through
	``redaction.scrub_secrets`` with the live key as a literal. Frappe's own
	with-context traceback prints every frame's locals, which is how the API
	key and the prompt reached the Error Log before this fix.

	- ``reference_doctype`` / ``reference_name`` point at the Optimus Session:
	  ``docname`` when the caller has it (no lookup), else the session that
	  ``session_uuid`` resolves to.
	- The row is inserted directly, in the current transaction. If that
	  transaction is rolled back later (a ``frappe.throw`` in a web request,
	  a failing background job), a ``frappe.db.after_rollback`` callback
	  queues the same scrubbed row again (see ``_requeue_if_rolled_back``).
	- If scrubbing fails, the row keeps only the title and the error type:
	  an unscrubbed message is never written.
	- An exception is logged at most once: the HTTP layer logs its own
	  failures, so a caller that logs the same ``AiFixError`` again is a
	  no-op (no double rows). Only a row that was written marks it.
	- Returns True once ``frappe.log_error`` has returned, else False
	  (already logged, or the write failed). A failed write leaves one
	  warning line with the error type in the ``optimus`` log.
	- Never raises, except an RQ job timeout (the job must still stop),
	  which leaves as a fresh instance with no chain.
	"""
	logged = False
	failure_type = None
	interrupt = None
	try:
		if exc is not None and getattr(exc, _LOGGED_ATTR, False):
			return False
		import frappe

		lines = [title]
		if session_uuid:
			lines.append(f"session_uuid={session_uuid}")
		for k in sorted(context):
			lines.append(f"{k}={context[k]}")
		if exc is not None:
			lines.append("".join(traceback.format_exception(exc, chain=False)).rstrip())
		message = _scrubbed_message(title, lines, exc)
		# Sentry (attach_stacktrace) serialises this frame's locals with the
		# event: only the scrubbed message may be bound while logging.
		del lines

		if not docname and session_uuid:
			try:
				docname = frappe.db.get_value("Optimus Session", {"session_uuid": session_uuid}, "name")
			except _job_timeout_types():
				raise
			except Exception:
				docname = None
		reference_doctype = "Optimus Session" if docname else None
		reference_name = docname or None
		row = frappe.log_error(
			title=title,
			message=message,
			reference_doctype=reference_doctype,
			reference_name=reference_name,
		)
		logged = True
		_mark_logged(exc)
		_requeue_if_rolled_back(
			{
				"error": message, "method": title,
				"reference_doctype": reference_doctype, "reference_name": reference_name,
			},
			row,
		)
	except _job_timeout_types() as e:
		interrupt = (type(e), e.args)
	except Exception as e:
		failure_type = type(e).__name__
	if interrupt is not None:
		raise interrupt[0](*interrupt[1])
	if failure_type is not None:
		_note_unwritten_row(failure_type)
	return logged


def _scrubbed_message(title: str, lines: list[str], exc: BaseException | None) -> str:
	"""``lines`` joined and passed through ``redaction.scrub_secrets`` with
	the live key as a literal. If scrubbing fails, the message keeps only the
	title and the error type, never the unscrubbed text. An RQ job timeout
	leaves as a fresh instance (no scrubber frame, no chain)."""
	failed = ""
	interrupt = None
	try:
		from optimus.redaction import scrub_secrets

		api_key = _current_key_or_empty()
		return scrub_secrets("\n".join(lines), literals=(api_key,))
	except _job_timeout_types() as e:
		interrupt = (type(e), e.args)
	except Exception as e:
		failed = type(e).__name__
	if interrupt is not None:
		lines = None  # never ride on the timeout's traceback unscrubbed
		raise interrupt[0](*interrupt[1])
	kind = type(exc).__name__ if exc is not None else "none"
	return f"{title}\n(details withheld: scrubbing the message failed with {failed}; error type {kind})"


def _requeue_if_rolled_back(record: dict, row=None) -> None:
	"""Queue the Error Log row ``log_ai_failure`` just inserted again if its
	transaction is rolled back.

	``frappe.log_error`` inserts the row in the current transaction, so a
	later rollback takes it away: ``frappe.app`` rolls the request back after
	an exception (a ``frappe.throw`` after the log) and ``execute_job`` does
	the same for a failing job. A ``frappe.db.after_rollback`` callback then
	queues the same fields (``record``: the scrubbed message, the title, the
	references, plus the ``trace_id`` and ``metadata`` of the inserted row
	``row``) through ``frappe.deferred_insert``. ``commit()`` drops the
	callbacks, so a committed row is never queued twice; a savepoint rollback
	runs none. Nothing is registered in read-only mode: ``log_error`` has
	queued the row itself there.

	The callback never calls ``frappe.log_error`` (it may run inside an
	``except`` block, and it must not reach Sentry), never raises
	(``CallbackManager.run`` would pass the error to the rollback's caller)
	except an RQ job timeout, and holds only the scrubbed fields."""
	interrupt = None
	try:
		import frappe

		if getattr(frappe.flags, "read_only", False):
			return
		for field in ("trace_id", "metadata"):
			value = getattr(row, field, None)
			if isinstance(value, str) and value:
				record[field] = value

		def _requeue() -> None:
			requeue_interrupt = None
			try:
				from frappe.deferred_insert import deferred_insert

				deferred_insert("Error Log", [dict(record)])
			except _job_timeout_types() as e:
				requeue_interrupt = (type(e), e.args)
			except Exception:
				pass
			if requeue_interrupt is not None:
				raise requeue_interrupt[0](*requeue_interrupt[1])

		frappe.db.after_rollback.add(_requeue)
	except _job_timeout_types() as e:
		interrupt = (type(e), e.args)
	except Exception:
		pass
	if interrupt is not None:
		raise interrupt[0](*interrupt[1])


def _note_unwritten_row(error_type: str) -> None:
	"""Leave a trace when ``log_ai_failure`` could not write its row: one
	warning line in the ``optimus`` log naming the error TYPE only (its
	message could hold anything). Never raises, except an RQ job timeout."""
	interrupt = None
	try:
		import frappe

		frappe.logger("optimus").warning(
			f"optimus ai_fix: an AI failure could not be written to the Error Log ({error_type})"
		)
	except _job_timeout_types() as e:
		interrupt = (type(e), e.args)
	except Exception:
		pass
	if interrupt is not None:
		raise interrupt[0](*interrupt[1])


def _mark_logged(exc: BaseException | None) -> None:
	"""Flag ``exc`` so a later ``log_ai_failure(..., exc)`` is a no-op."""
	if exc is None:
		return
	interrupt = None
	try:
		setattr(exc, _LOGGED_ATTR, True)
	except _job_timeout_types() as e:
		interrupt = (type(e), e.args)
	except Exception:
		pass
	if interrupt is not None:
		raise interrupt[0](*interrupt[1])


def _log_http_error(
	provider: str, where: str, status: int | None, detail: str = "",
	*, exc: BaseException | None = None, provider_error: str = "",
) -> None:
	"""Log one HTTP-layer failure through ``log_ai_failure``: provider, call
	site, HTTP status, the provider's own error identifier when it sent one
	(``provider_error``, see ``_provider_error_code``) and a short detail
	(for a transport error, its type and message, scrubbed; for an
	unexpected error, its type and plain frames). Never the prompt, the
	source code, the headers or the response body. The session reference
	comes from the per-worker spend marker the caller set
	(``analyze._mark_ai_spend_session``), the same one
	``_record_session_spend`` reads. ``exc`` (the ``AiFixError`` about to be
	raised) is then marked logged, but only if the row was written, so the
	caller's own ``log_ai_failure`` for it writes no second row and a failed
	write still leaves the caller's."""
	session_uuid = None
	interrupt = None
	try:
		import frappe

		session_uuid = getattr(frappe.local, "_optimus_spend_session", None)
	except _job_timeout_types() as e:
		interrupt = (type(e), e.args)
	except Exception:
		session_uuid = None
	if interrupt is not None:
		raise interrupt[0](*interrupt[1])
	context = {"provider": provider, "where": where, "status": status, "detail": detail}
	if provider_error:
		context["provider_error"] = provider_error
	if log_ai_failure("optimus ai_fix", session_uuid=session_uuid, **context):
		_mark_logged(exc)


def _job_timeout_types() -> tuple[type[BaseException], ...]:
	"""RQ's job-timeout exception classes (subclasses of ``Exception``), or ``()``
	when rq is not importable (pure unit-test runs)."""
	try:
		from rq.timeouts import BaseTimeoutException
	except Exception:
		return ()
	return (BaseTimeoutException,)


def _response_detail(resp) -> str:
	"""The provider's own error body (capped), as a ': ...' suffix or '' when
	there is no readable body. Surfaces the specific reason ("model not found",
	"context too long", ...) so it reaches the operator.

	SECURITY: a provider can echo the API key in its error body, and this text
	reaches toasts, API responses and the title of Frappe's own error
	snapshot, so the body is scrubbed with the live key as a literal BEFORE it
	is cut to 300 characters (cutting first can split the key, and a partial
	key no longer matches the literal). Any failure returns ''; an RQ job
	timeout leaves as a fresh instance, with the raw body unbound."""
	body_text = ""
	interrupt = None
	try:
		body_text = (resp.text or "").strip()
		if not body_text:
			return ""
		from optimus.redaction import scrub_secrets

		return ": " + scrub_secrets(body_text[:65536], literals=(_current_key_or_empty(),))[:300]
	except _job_timeout_types() as e:
		interrupt = (type(e), e.args)
	except Exception:
		return ""
	body_text = ""  # the raw body may echo the key: never on the timeout's traceback
	raise interrupt[0](*interrupt[1])


# An identifier-shaped provider error code: nothing that could be prose, a
# prompt fragment, an address or a URL.
_PROVIDER_ERROR_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def _provider_error_code(resp) -> str:
	"""The provider's machine-readable reason for an HTTP error, for the Error
	Log row (whose detail never holds the body: it can echo the prompt), or
	'' when there is none.

	Read from the JSON body's ``error`` object: ``type`` and ``code`` (OpenAI
	and compatible servers) or ``type`` (Anthropic). A value is kept only when
	it is a string that fully matches ``[A-Za-z0-9_.:-]{1,64}`` and does not
	contain the stored key; both kept values are joined as ``type:code`` when
	that still fits 64 characters, else the first one is used. Any failure
	returns ''; an RQ job timeout leaves as a fresh instance, with the parsed
	body unbound."""
	data = error = value = None
	interrupt = None
	try:
		data = resp.json()
		error = data.get("error") if isinstance(data, dict) else None
		if not isinstance(error, dict):
			return ""
		from optimus.redaction import scrub_secrets

		api_key = _current_key_or_empty()
		parts: list[str] = []
		for field in ("type", "code"):
			value = error.get(field)
			if (
				isinstance(value, str)
				and _PROVIDER_ERROR_RE.fullmatch(value)
				and value not in parts
				and scrub_secrets(value, literals=(api_key,)) == value
			):
				parts.append(value)
		if not parts:
			return ""
		joined = ":".join(parts)
		return joined if len(joined) <= 64 else parts[0]
	except _job_timeout_types() as e:
		interrupt = (type(e), e.args)
	except Exception:
		return ""
	data = error = value = None  # the body may echo the key: never on the timeout's traceback
	raise interrupt[0](*interrupt[1])


def _http_post(
	url: str,
	headers: dict,
	body: dict,
	*,
	provider: str,
	where: str,
	timeout: int | None = None,
	auth: requests.auth.AuthBase | None = None,
) -> dict:
	"""POST JSON, return the parsed response dict. Maps transport / HTTP /
	decode errors to ``AiFixError`` with operator-friendly messages and logs
	each failure once (``_log_http_error``).

	SECURITY: ``auth`` (an ``_ApiKeyAuth``) attaches the key at send time, so
	``headers`` never holds it. Every failure is logged and raised OUTSIDE the
	``except`` blocks: while an ``except`` block runs, the original exception
	is the active one, Frappe's Sentry hook captures it with its
	requests / urllib3 frames (whose locals hold the prepared headers), and
	a ``raise`` there would chain it as ``__context__``. The catch-all only
	records plain values (the exception's type name and its frames as
	``file:line:function`` strings read off the traceback; for an RQ job
	timeout, its type and args): a ``UnicodeEncodeError`` from http.client
	carries the header value, so the error to raise is built after the
	``try``, where a failure while building it can neither chain that
	exception nor find it still bound in this frame.

	The catch-all takes ``BaseException``. An interrupt that is not an
	``Exception`` (``SystemExit`` from a gunicorn worker timeout,
	``KeyboardInterrupt``, a gevent ``Timeout``) is re-raised after the
	``try`` as the SAME instance (gevent matches its timeout by identity),
	with its traceback, ``__context__`` and ``__cause__`` cleared, and is not
	logged: otherwise it would leave with the requests / urllib3 frames,
	which Sentry's WSGI middleware ships with their locals."""
	timeout = timeout or _resolve_timeout_seconds()
	job_timeout_types = _job_timeout_types()
	failure: AiFixError | None = None
	unexpected_name: str | None = None
	unexpected_frames: list[str] = []
	interrupt_type: type[BaseException] | None = None
	interrupt_args: tuple = ()
	escaping: BaseException | None = None
	detail = ""
	resp = None
	try:
		resp = requests.post(url, headers=headers, json=body, timeout=timeout, auth=auth)
	except requests.exceptions.Timeout:
		failure = AiFixError(f"The AI provider didn't respond within {timeout}s.", kind="timeout")
		detail = "timeout"
	except requests.exceptions.RequestException as e:
		failure = AiFixError(f"Couldn't reach the AI provider: {type(e).__name__}.", kind="transport")
		detail = f"{type(e).__name__}: {e}"
	except BaseException as e:
		if isinstance(e, job_timeout_types):
			interrupt_type, interrupt_args = type(e), e.args
		elif not isinstance(e, Exception):
			escaping = e
		else:
			unexpected_name = type(e).__name__
			# file:line:function per frame, read straight off the traceback:
			# no source lookup (no I/O while this handler runs), no locals,
			# no message.
			unexpected_frames = [
				f"{frame.f_code.co_filename}:{lineno}:{frame.f_code.co_name}"
				for frame, lineno in traceback.walk_tb(e.__traceback__)
			]
	if escaping is not None:
		# Not ours to handle: it leaves unlogged, without the frames below
		# this one (their locals hold the prepared headers) and unchained.
		escaping.__traceback__ = None
		escaping.__context__ = None
		escaping.__cause__ = None
		escaping.__suppress_context__ = True
		raise escaping
	if interrupt_type is not None:
		# The RQ job hit its timeout while we were sending: it must still stop
		# the job, so re-raise the same type, but as a fresh instance with no
		# requests / urllib3 frames and no chain.
		raise interrupt_type(*interrupt_args)
	if unexpected_name is not None:
		from frappe import _

		failure = AiFixError(_("The AI request failed ({0}).").format(unexpected_name), kind="transport")
		# Where it happened, never what it said: plain frames, no message, no locals.
		detail = unexpected_name + "".join(f"\n  {frame}" for frame in unexpected_frames)
	if failure is not None:
		_log_http_error(provider, where, None, detail, exc=failure)
		raise failure

	status = resp.status_code
	if status in (401, 403):
		failure = AiFixError("The AI provider rejected the API key. Check it in Optimus Settings.", status_code=status)
	elif status == 404:
		# A 404 means the endpoint path or the model was not found. The Model
		# field is editable for every provider and a wrong model name returns
		# 404, so the message leads with that. It also always mentions a custom
		# ('OpenAI-compatible') Base URL missing the '/v1' segment, phrased as
		# "if you set a custom Base URL" so a hosted-provider operator (whose
		# Base URL is fixed and hidden) reads it as not their case. The
		# provider's own error body is surfaced either way.
		detail = f"url={url}"
		failure = AiFixError(
			f"The AI provider returned 404 (Not Found) for {url}. Check that the Model "
			"in Optimus Settings is a valid model name for this provider: a wrong model "
			"returns 404. If you set a custom Base URL, make sure it includes the '/v1' "
			"path segment (for example http://localhost:11434/v1 for Ollama)."
			+ _response_detail(resp),
			status_code=status,
		)
	elif status == 429:
		failure = AiFixError("The AI provider is rate-limiting requests. Try again shortly.", status_code=status)
	elif status >= 400:
		failure = AiFixError(f"The AI provider returned an error (HTTP {status}){_response_detail(resp)}", status_code=status)
	if failure is not None:
		_log_http_error(provider, where, status, detail, exc=failure, provider_error=_provider_error_code(resp))
		raise failure

	data = None
	try:
		data = resp.json()
	except Exception:
		detail = "non-JSON body"
		failure = AiFixError(
			"The AI provider returned an unexpected (non-JSON) response.",
			status_code=status, kind="bad_response",
		)
	if failure is None and not isinstance(data, dict):
		from frappe import _

		detail = f"JSON {type(data).__name__}, not an object"
		failure = AiFixError(
			_("The AI provider returned an unexpected response (not a JSON object)."),
			status_code=status, kind="bad_response",
		)
	if failure is not None:
		_log_http_error(provider, where, status, detail, exc=failure)
		raise failure
	return data


# Token counts land in Int columns (Optimus Session.ai_tokens_spent,
# ai_steps_tokens); a larger reported count is not a real one.
_MAX_TOKEN_COUNT = 2**31 - 1


def _token_count(value) -> int:
	"""A provider-reported token count as a non-negative int, or 0 when it is
	not one: not a number (``"abc"``, a container, NaN, infinity), a bool, a
	negative or an absurdly large value. Usage is informational, so an odd
	usage block must never fail a reply that already carries a suggestion.
	Never raises, except an RQ job timeout (re-raised fresh)."""
	if isinstance(value, bool):
		return 0
	interrupt = None
	try:
		count = int(value)
	except _job_timeout_types() as e:
		interrupt = (type(e), e.args)
	except Exception:
		return 0
	if interrupt is not None:
		raise interrupt[0](*interrupt[1])
	return count if 0 <= count <= _MAX_TOKEN_COUNT else 0


def _usage_block(data) -> dict:
	"""``data["usage"]`` when it is a dict, else ``{}``."""
	usage = data.get("usage") if isinstance(data, dict) else None
	return usage if isinstance(usage, dict) else {}


def _usage_from_openai(data: dict | None) -> dict:
	"""Normalised token usage from an OpenAI-shaped response (also what the
	Aerele managed proxy + Ollama/LM Studio/vLLM return). Missing or
	malformed fields → 0 (see ``_token_count``); ``total`` falls back to
	prompt+completion when the upstream omits it. Never raises."""
	u = _usage_block(data)
	prompt = _token_count(u.get("prompt_tokens"))
	completion = _token_count(u.get("completion_tokens"))
	total = _token_count(u.get("total_tokens")) or _token_count(prompt + completion)
	return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def _usage_from_anthropic(data: dict | None) -> dict:
	"""Normalised token usage from an Anthropic Messages response
	(``usage.input_tokens`` / ``usage.output_tokens``). Missing or malformed
	fields → 0 (see ``_token_count``). Never raises."""
	u = _usage_block(data)
	prompt = _token_count(u.get("input_tokens"))
	completion = _token_count(u.get("output_tokens"))
	return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": _token_count(prompt + completion)}


def _record_session_spend(total_tokens) -> None:
	"""Best-effort: add this call's tokens to the active session's cumulative
	``Optimus Session.ai_tokens_spent``. The session uuid comes from
	``frappe.local._optimus_spend_session`` (set by the caller before any AI
	call); ``None`` (e.g. the settings probe) is a no-op."""
	try:
		import frappe

		su = getattr(frappe.local, "_optimus_spend_session", None)
		n = int(total_tokens or 0)
		if su and n > 0:
			frappe.db.sql(
				"update `tabOptimus Session` "
				"set ai_tokens_spent = coalesce(ai_tokens_spent, 0) + %s "
				"where session_uuid = %s",
				(n, su),
			)
	except Exception:
		pass


def _aerele_call_metadata(provider, finding_type=None) -> dict | None:
	"""Metadata attaching an Aerele managed-proxy request to the originating
	Optimus Session, so the Aerele billing portal can attribute each AI call.

	Returns ``None`` for every non-Aerele provider (so no unknown body fields
	reach OpenAI / Anthropic) and on any failure (the call then proceeds
	unattributed). The session uuid comes from
	``frappe.local._optimus_spend_session``; the docname is resolved from it."""
	if not provider or provider.get("name") != "Aerele":
		return None
	try:
		import frappe

		uuid = getattr(frappe.local, "_optimus_spend_session", None)
		if not uuid:
			return None
		meta = {"optimus_session_uuid": uuid}
		docname = frappe.db.get_value("Optimus Session", {"session_uuid": uuid}, "name")
		if docname:
			meta["optimus_session"] = docname
		if finding_type:
			meta["optimus_finding_type"] = finding_type
		return meta
	except Exception:
		return None


def _call_anthropic(
	base_url: str, api_key: str, model: str, system: str, messages: list[dict],
	*, max_tokens: int = _MAX_OUTPUT_TOKENS, usage_out: dict | None = None,
) -> str:
	url = base_url.rstrip("/") + "/v1/messages"
	headers = {
		"content-type": "application/json",
		"anthropic-version": _ANTHROPIC_VERSION,
	}
	auth = _ApiKeyAuth("x-api-key", api_key) if api_key else None
	body = {
		"model": model,
		"max_tokens": max_tokens,
		"temperature": _TEMPERATURE,
		"system": system,
		"messages": messages,
	}
	data = _http_post(url, headers, body, provider="anthropic", where="messages", auth=auth)
	if usage_out is not None:
		usage_out.update(_usage_from_anthropic(data))
		_record_session_spend(usage_out.get("total_tokens"))
	try:
		blocks = data.get("content") or []
		for b in blocks:
			if isinstance(b, dict) and b.get("type") == "text":
				return b.get("text") or ""
		# Fall back to the first block's text if no explicit type.
		if blocks and isinstance(blocks[0], dict):
			return blocks[0].get("text") or ""
	except Exception:
		pass
	raise AiFixError("The AI provider's response didn't contain any text.")


def _call_openai_chat(
	base_url: str, api_key: str, model: str, system: str, messages: list[dict],
	*, max_tokens: int = _MAX_OUTPUT_TOKENS, usage_out: dict | None = None,
	metadata: dict | None = None,
) -> str:
	url = base_url.rstrip("/") + "/chat/completions"
	headers = {"content-type": "application/json"}
	auth = _ApiKeyAuth("authorization", api_key, prefix="Bearer ") if api_key else None
	body = {
		"model": model,
		"max_tokens": max_tokens,
		"messages": [{"role": "system", "content": system}, *messages],
	}
	sent_temperature = not _is_reasoning_model(model)
	if sent_temperature:
		body["temperature"] = _TEMPERATURE
	# Aerele-only: attribute this call to the originating Optimus Session.
	if metadata:
		body["metadata"] = metadata
	retry_without_temperature = False
	try:
		data = _http_post(url, headers, body, provider="openai", where="chat/completions", auth=auth)
	except AiFixError as e:
		# Some reasoning models reject a non-default `temperature` with a
		# request-validation error. OpenAI o-series are pre-filtered by
		# `_is_reasoning_model`, but others e.g. Moonshot/Kimi "thinking"
		# variants only allow the default and say so ("invalid temperature:
		# only 1 is allowed for this model"). We can't enumerate every such
		# model, so retry once without `temperature` (letting the model use its
		# own default). Gate on a request-validation status (400 or 422; some
		# OpenAI-compatible gateways use 422) so a body that mentions the word
		# for another reason (e.g. a 404 listing valid params) can't trigger a
		# needless second call.
		# The retry runs after the try: a request sent inside this block would
		# log its own failure while this error is the active exception.
		if sent_temperature and getattr(e, "status_code", None) in (400, 422) and "temperature" in str(e).lower():
			retry_without_temperature = True
		else:
			raise
	if retry_without_temperature:
		body.pop("temperature", None)
		data = _http_post(url, headers, body, provider="openai", where="chat/completions", auth=auth)
	if usage_out is not None:
		usage_out.update(_usage_from_openai(data))
		_record_session_spend(usage_out.get("total_tokens"))
	try:
		choices = data.get("choices") or []
		if choices:
			msg = choices[0].get("message") or {}
			content = msg.get("content")
			if isinstance(content, str):
				return content
			# Some servers return content as a list of parts.
			if isinstance(content, list):
				return "".join(
					p.get("text", "") for p in content if isinstance(p, dict)
				)
	except Exception:
		pass
	raise AiFixError("The AI provider's response didn't contain any text.")
