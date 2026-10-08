# AI Fix Suggestions: data flow & privacy

This document inventories **exactly** what data leaves your host when Optimus's AI fix suggestion feature is enabled, where it goes and how to keep everything on-box with a local LLM. It's written for operators making the consent decision and for reviewers (compliance / security / a dev shop receiving a profile) who need to audit the wire.

If you're picking up Optimus for the first time: every AI feature here is **off by default**. The rest of this doc only matters once an operator explicitly turns one on.

---

## 1. TL;DR: default OFF

Optimus ships with `ai_enabled = 0` in `Optimus Settings` — the master gate. No request leaves your host until a System Manager flips that master toggle AND configures a provider (or points at a local LLM). (`ai_auto_suggest` defaults to on, but it is inert while the master gate is off.) With AI off:

- The analyze pipeline runs to completion as it always did.
- Findings are rendered with the profiler's own deterministic fix hints.
- Nothing is sent to any LLM endpoint, hosted or otherwise.

When AI is enabled, three knobs gate every outbound call:

| Knob (Optimus Settings → AI) | Default | What it does |
|---|---|---|
| `ai_enabled` | OFF | Master gate. OFF → zero LLM calls ever. |
| `ai_auto_suggest` | ON | Batch mode (takes effect only once `ai_enabled` is on). ON → analyze.run sends the top-N eligible findings during the background analyze pass. OFF → AI runs only when the operator uses the session's **Refresh AI suggestions** action. |
| `ai_excluded_finding_types` | empty | One finding type per line (v0.9.0+). Listed types are skipped in **both** auto-suggest and on-demand the request body is never built and never sent. |

With the master `ai_enabled=OFF` nothing is sent regardless. Once AI is enabled, three axes of consent remain: feature-level (the master), event-level (`ai_auto_suggest` — on by default so suggestions are built into the report; set it OFF to require a session refresh) and type-level (the exclusion list).

---

## 2. What data leaves the host

There are three outbound request shapes. Each table lists every distinct field that crosses the network boundary, the typical size and the source.

### 2.1 Finding fix suggestion (`/v1/messages` or `/chat/completions`)

Triggered by the session's **Refresh AI suggestions** action or auto-suggest at analyze time. Built by `ai_fix._build_fix_request` in `optimus/ai_fix.py`.

**System prompt** (static, about 6.1 KB, about 1,850 conservatively estimated tokens): Frappe framework rules for the proposed code, caching and data-layer idioms, grounding rules and the output format (Diagnosis / Fix / Why it works / Verify). It is byte-identical for every finding, so providers can reuse its prefix. Anthropic requests include one cache breakpoint; caching still depends on the model's minimum prefix size.

**User message** (per-finding, sized to the model's context window; 18,000 budget-unit cap):

Every value captured from your site (title, callsite, source, SQL, EXPLAIN, hot line) is wrapped in a `<data-NONCE kind="...">` block with a per-request nonce and source and SQL fences longer than any captured backtick run; the system prompt tells the model to treat those blocks as data only.

| Field | Source | Typical size | Notes |
|---|---|---|---|
| `finding_type` | Optimus Finding | ~15 chars | One of the 4 eligible types (§ 5). |
| `severity` | Optimus Finding | 4–6 chars | High / Medium / Low. |
| `title` | Optimus Finding | 60–200 chars | Profiler-generated label (e.g. "`frappe.get_doc` on line 12 loops 18×"). |
| `estimated_impact_ms` | Optimus Finding | ~5 chars | Profiler's impact estimate. |
| `affected_count` | Optimus Finding | ~3 chars | Occurrence count. |
| `callsite.filename` | technical_detail_json | 40–80 chars | Relative path under your bench, e.g. `apps/myapp/myapp/forms/invoice.py`. |
| `callsite.lineno` | technical_detail_json | 3–5 chars | Line number. |
| `callsite.function` | technical_detail_json | 20–60 chars | Function name from your code. |
| **`source_window`** | `analyze._ai_grounding_window` | **1-2 KB typical** | The whole enclosing function, including decorators, when it fits in 80 lines; otherwise 24 lines before and after the callsite. The request budget can shorten it further. Verbatim comments, strings and names are included, with a 200-character cap per source line. **This is the largest field in a typical request.** |
| loop facts | `ai_grounding.loop_facts_from_tree` over the whole source file's AST | bounded by the request budget | N+1 Query, Redundant Call and Hot Line only: the loops around the callsite (innermost first, comprehensions and `while` included), which variables of each loop its call uses, whether its result is used and the writes the profiler can name (subscript and formatted-SQL writes among them). They are computed after the budget trims the window and cover only lines the trimmed prompt still shows. Identifiers only, no values, inside a data block (fenced). A line in no loop of its function gets a caller hint, for N+1 Query and Redundant Call, that a caller may loop. |
| index advice | `index_recipes.advise_finding` | 200-1,600 chars | Slow Query only: the profiler's deterministic index advice (route, DocType, columns and explanation), inside a data block. |
| `phase2_hotline` | Phase 2 line-profile | 100–400 chars | When available, the hottest single line from line-profiling (line number, content, total ms, hit count). |
| `technical_detail.function` | technical_detail_json | 30–80 chars | Hot function name (for Slow-Hot-Path-type findings). |
| `technical_detail.cumulative_ms` | technical_detail_json | ~5 chars | Time in that function. |
| `technical_detail.action_wall_time_ms` | technical_detail_json | ~5 chars | Action's total wall time. |
| `technical_detail.normalized_query` | technical_detail_json | 100–2400 chars | **Capped 2400.** Normalized SQL table names, column names, WHERE clause structure preserved; literals replaced by `?` (then redacted again by `optimus.redaction` if they match sensitive column names). |
| `technical_detail.explain_row` | technical_detail_json | 100–800 chars | `EXPLAIN` output, capped type / rows / key / Extra etc. |
| `technical_detail.validation_note` | technical_detail_json | 0–300 chars | Caveats. |
| **`technical_detail.example_queries`** | live recording (Redis) | **0–4800 chars** | Up to 2 of the slowest **raw** SQL queries from this action's recording, each capped at 2400 chars. These are real production queries table names, column names, WHERE values are preserved (after `password = '…'`-style literal redaction at capture time, see `optimus.redaction`). |

The user message is assembled to fit the provider's context window (section 4.2): optional parts (example queries, EXPLAIN row, the normalized query and loop facts) are dropped whole, the least useful first, and only then does the source window shrink around the target line. Nothing is cut inside a block.

Oversized title and callsite text are clipped before wrapping. A source line is kept verbatim or omitted with a no-source notice; omitted lines cannot ground a proposed diff.

### 2.2 Humanize "Steps to Reproduce" (`/v1/messages` or `/chat/completions`)

Triggered by `ai_humanize_steps`. Built by `_build_steps_messages` in `optimus/ai_fix.py`.

**System prompt** (static, ~2.9 KB): ERPNext workflow knowledge, Frappe API decoding rules, collapse rules, output spec (ordered Markdown list + one-sentence summary).

**User message** (per-session, ~1–8 KB, **8 KB hard cap**):

| Field per action | Source | Typical size | Notes |
|---|---|---|---|
| `label` | `per_action.humanized_label(recording)` | 20–100 chars | E.g. "Save Sales Order SO-0001". |
| `cmd` | recording.cmd | 20–80 chars | E.g. "frappe.desk.form.save.savedocs". |
| `path` | recording.path | 30–100 chars | URL path, when applicable. |
| `method` | recording.method | 4–8 chars | HTTP verb. |
| `doctype` | extracted from form_dict | 20–60 chars | E.g. "Sales Invoice". |
| `duration_ms` | recording.duration | ~5 chars | Wall time. |

Up to `_MAX_STEPS_ACTIONS = 60` actions, sent inside one data block and limited to 8,000 chars or the context budget, whichever is smaller. `_is_reproducer_noise` pre-filters polling, form-load and asset requests.

### 2.3 Index suggestion (removed)

Index advice for Missing Index, Full Table Scan, Filesort, Temporary Table and Low Filter Ratio findings, and for the per-table index cards, is built by Optimus itself (`optimus/renderer/index_recipes.py`, one advisor for both, so a finding and a card never disagree) from the DocType metadata, the real column types and the table's existing indexes. This path sends nothing to the AI provider; a Slow Query prompt carries the same advice as data.

**Evidence comes first.** Before any code, the advisor checks the lead column of the recipe against the table's existing indexes, its Search Index flag, its Unique flag and the `key` in the finding's EXPLAIN row. A trailing `creation` or `modified` column is exempt: Frappe indexes it, but that never turns a recipe into "no code". An existing index that already starts with the whole recipe column list also gives no code.

- **No code** when the lead column already leads an index (under any name), has Search Index ticked, is unique, or EXPLAIN names an index on it; when the column does not exist (or differs in case), is not a field, is a MariaDB reserved word, cannot be indexed (JSON), is a leading text column on Postgres, or when the key would pass 3072 bytes on MariaDB or a Postgres btree row would pass about 2704 bytes (a long varchar). A query longer than 4 KB is not parsed. The report says why, and for an existing index names the likely query shape that cannot use it.
- **Shapes that give no code** because an index on the column could not be used: an `OR` between conditions, a `LIKE` with a leading wildcard, a function around the column (`IFNULL`, `YEAR`, `DATE`, the rewrite of `!=` into `IFNULL(col, ?) <> ?`), a `CASE` expression, arithmetic, or the column compared with another column. A query Optimus cannot read (cut at 500 characters, a UNION, a derived table) gets "Optimus could not read this query" text and the EXPLAIN advice, never a claim that an index would not help. About a dozen scanner gaps are recorded as residuals; each fails closed (no code) and never produces a wrong migration.
- **Column order** is equality columns, then one range column, then the sort or group column. For Filesort and Temporary Table findings a sort over a range filter on another column recommends (equality columns, sort column) and says the range filter cannot use that index. This applies only when every sort or group item is a bare column of the target table in the same direction; an `IN` or `IS NULL OR =` filter still sorts and does not claim "already sorted". `creation` and `modified` never lead.
- **Tick Search Index** for one non-text column of a field you control on MariaDB: your app's DocType (an app in Tracked Apps; with Tracked Apps empty no installed app counts as yours), a Custom Field, or a DocType created in the UI. Frappe's schema sync then owns the index.
- **`ensure_indexes()`** for everything else: composites, a text column (a 255-character prefix on MariaDB), another app's field, and any index on Postgres. Save the module below in your app and register it on `after_install`, `after_sync` and `after_migrate` in hooks.py. `after_sync` runs right after the install's fixture sync, so a fixture-shipped Custom Field is indexed on a fresh install too. If the file already exists, add only the new entry to its `INDEXES` list. Each entry is committed on its own (the previous work is committed first, so a failed entry rolls back only itself), checks the table, its columns and the index first, and a failure writes an Error Log row and moves on, so it never blocks `bench migrate`. Entries carry a `db` stamp (`mariadb` or `postgres`) and are skipped, with one Error Log row, on the other database. For another app's single column the entry sets a `search_index` Property Setter once and syncs the table, so Frappe's schema sync keeps that index. Index names are `idx_<doctype>_<hash>`: at most 53 characters and unique across the schema (Postgres index names are schema-wide).
- **When advice cannot be built**, the finding or card says "Optimus could not build index advice for this finding (or table)", the failure is counted and written once to the bench log, and the export carries the same note. It never blocks the render.

```python
# your_app/your_app/optimus_indexes.py
# In your_app/hooks.py run it after install, right after the install's fixture sync and after
# every migrate (add it to these lists when hooks.py already defines them):
#   after_install = ["your_app.optimus_indexes.ensure_indexes"]
#   after_sync = ["your_app.optimus_indexes.ensure_indexes"]
#   after_migrate = ["your_app.optimus_indexes.ensure_indexes"]
import contextlib

import frappe

# An entry with "db" runs only on that database (frappe.db.db_type).
INDEXES = [
	{"doctype": "Sales Invoice", "columns": ["customer", "posting_date"], "db": "mariadb", "index_name": "idx_sales_invoice_1d3b8654"},
	{"doctype": "Sales Invoice", "search_index_field": "po_no", "db": "mariadb"},
]


def ensure_indexes():
	"""Create each index in INDEXES once. One failed entry never stops the others."""
	for entry in INDEXES:
		try:
			# commit what ran before this entry, so the rollback below undoes only this entry
			frappe.db.commit()
			_ensure_index(entry)
			frappe.db.commit()
		except Exception:
			with contextlib.suppress(Exception):
				frappe.db.rollback()
				frappe.log_error(title=_title(entry, "was not created"))


def _title(entry, what):
	key = entry.get("index_name") or entry.get("search_index_field")
	return f"Index for {entry['doctype']} {what}: {key}"[:140]


def _ensure_index(entry):
	doctype = entry["doctype"]
	if entry.get("db", frappe.db.db_type) != frappe.db.db_type:
		title = _title(entry, f"skipped on {frappe.db.db_type}, the entry is for {entry['db']}")
		if not frappe.db.exists("Error Log", {"method": title}):
			frappe.log_error(title=title)
		return
	if not frappe.db.table_exists(doctype, cached=False):
		return
	field = entry.get("search_index_field")
	if field:
		if not frappe.db.has_column(doctype, field):
			return
		if not frappe.db.exists(
			"Property Setter",
			{"doc_type": doctype, "field_name": field, "property": "search_index", "value": "1"},
		):
			from frappe.custom.doctype.property_setter.property_setter import make_property_setter

			make_property_setter(doctype, field, "search_index", 1, "Check", validate_fields_for_doctype=False)
		if not frappe.db.has_index(f"tab{doctype}", f"{field}_index"):
			frappe.db.updatedb(doctype)
		return
	columns = entry["columns"]
	if not all(frappe.db.has_column(doctype, column.split("(", 1)[0]) for column in columns):
		return
	if frappe.db.has_index(f"tab{doctype}", entry["index_name"]):
		return
	frappe.db.add_index(doctype, columns, index_name=entry["index_name"])
```

On Postgres, building an index blocks writes to the table, so add indexes on write-hot tables in a maintenance window; Frappe's schema sync can also drop a Search Index named after a column of a composite index on another table until that table syncs again (a Frappe issue).

### 2.4 Connectivity probe (Optimus Settings → AI → "Test connection" button)

Smallest payload (`ai_fix.test_connection`). System: ~80 chars. User content: ~40 chars (`Reply with exactly: OK`). Total request ~150 bytes. Used only to verify the provider URL + key.

---

## 3. What does NOT leave the host

These items are **never** sent in any AI request body:

- **Sensitive SQL literals.** `password = '...'`, `api_key = '...'`, `token = '...'` and the 12 other patterns in `optimus.redaction.DEFAULT_SENSITIVE_SQL_COLUMNS` are replaced with `<REDACTED>` at capture time, so even the `example_queries` field sees the redacted form. Operators can extend this list via `sensitive_sql_columns` in Optimus Settings.
- **HTTP form bodies.** `form_dict` keys matching `password` / `api_key` / `token` / etc. are redacted at capture (`optimus.redaction.DEFAULT_SENSITIVE_KEYS`) and form bodies themselves are never included in AI payloads only summary fields like `cmd` / `doctype` reach the humanize-steps path.
A guardrail repair request is a separate completion: it reads the current key once
when sent. Same-origin redirects and the temperature retry reuse that completion's
auth object without decrypting again.

- **Your API keys.** The provider API key is stored in an encrypted Password field and decrypted only when a request is sent (`frappe.utils.password.get_decrypted_password`), once per call (one SELECT on `__Auth`). For a provider that needs a key it must be plain printable ASCII: a key with any other character (a space inside it, a pasted smart quote or no-break space, a control character) is refused before any request is made, with a message that names the usual causes (a pasted smart quote, a stray space, a no-break space, a control character), and such a key is refused when Optimus Settings is saved, with the same message. The OpenAI-compatible provider needs no key (Ollama, LM Studio, vLLM): there such a key is neither sent nor refused, and the request goes without one; a key it can send is still sent (a router such as OpenRouter needs one). In Optimus's code it sits only in local variables named `api_key` or `secret` (names Frappe's traceback sanitizer and Sentry redact), for a moment in the `literals` parameter of `redaction.scrub_secrets` (which moves the key into `secret` before it scrubs anything), and in a masked `requests` auth object (`_ApiKeyAuth`). It travels only in the HTTP header (`x-api-key` or `Authorization: Bearer ...`), which that object sets on the HTTP library's own prepared request, whose headers hold it while the request is sent (the response keeps that request, and neither one's `repr` shows its headers). The HTTP library never follows a redirect: it drops only a header named `Authorization` when it follows one to another host, so the `x-api-key` header would have been sent on to the redirect target. Optimus follows at most three 307 or 308 redirects itself, only to the same host and port (with the same scheme, or http to https on that host), sending the same request and key again; any other 3xx reply (a 301, 302 or 303, another host or port, a downgrade, a fourth redirect) is reported as an unexpected response instead, which says that a Base URL that redirects (301, 302 or 303, or a 307 or 308 to another host) must be set to the final URL it redirects to, and the address a redirect points to is never logged or shown. Apart from those it is never part of a settings dict, a header dict, the prompt or an exception message. A provider's error reply is scrubbed before it is shown, of the key the request was sent with (the only key the provider received, so an echo is masked even when the key in Settings was changed while the request ran), or of the key stored in Optimus Settings when the request carried none, each in its raw and its JSON-escaped form. A 404 message names the request URL with any credentials in it masked (a `user:password@` typed into a custom Base URL, or the key), or only "(the configured Base URL)" when the URL cannot be scrubbed. In each of the failure scenarios the canary test (`optimus/tests/test_ai_secret_canary.py`) models, it appears in no Error Log row, traceback or Sentry event (earlier releases could log it: see the API key advisory in `CHANGELOG.md`), and it is never returned to the client. The OpenAI-compatible provider with `needs_key=False` (local endpoints) sends no auth header at all when no key is set.
- **Recording UUIDs.** Internal Redis keys.
- **Your full DB schema.** Only tables observed in this profile's recordings are mentioned by name.
- **Other sessions / users / findings.** Each suggestion is scoped to one finding (or table or session). A repair request includes that finding's first answer and the broken-rule list. No cross-session context.
- **The signed pickle blobs in Redis.** These hold the raw recording state mid-pipeline; the AI payload reads only finalized analyzer output.

---

## 4. Where the request goes

`Optimus Settings → AI → Provider`:

| Provider | Protocol | Default base URL | Key needed? | Notes |
|---|---|---|---|---|
| `Anthropic` | Messages | `https://api.anthropic.com` | Yes | Default model: `claude-sonnet-4-6`. |
| `OpenAI` | Chat completions | `https://api.openai.com/v1` | Yes | Default model: `gpt-4.1-mini`. |
| `Kimi (Moonshot)` | Chat completions | `https://api.moonshot.ai/v1` | Yes | Default model: `kimi-k2-0905-preview`. |
| `DeepSeek` | Chat completions | `https://api.deepseek.com/v1` | Yes | Default model: `deepseek-chat` (V3). `deepseek-reasoner` (R1) also works. |
| `Aerele` | Chat completions | `https://api.aerele.in/optimus/v1` | Yes | Managed service buy a fixed token pack up front. See § 10. |
| `OpenAI-compatible` | Chat completions | (you set it) | No (configurable) | Use this for local LLMs and any other OpenAI-shaped server. |

`ai_model` overrides the default model and `ai_api_key` supplies the credential. `ai_base_url` overrides the endpoint only for OpenAI-compatible providers. The HTTP timeout is `ai_request_timeout_seconds` (v0.9.0+, default 60s, clamped 10–600s).

For an explicit data-residency choice, use `OpenAI-compatible` pointed at a process you run yourself see § 6.

---

Every fix suggestion is verified before it is stored (`optimus/ai_guardrails.py`): the diff's `-` and context lines must match the source Optimus sent, and every block the report would show as code (any section, any indent, including indented code blocks and `~~~` fences) is parsed and checked against the Frappe rules the prompt teaches; code quoted verbatim from the shown source counts as context.

Adding a whitelist decorator or editing an argument on a later signature line triggers the type-hint check, including positional-only and keyword-only arguments. Adding a child-row mutation inside an existing loop also triggers its check. An unrelated body edit does not turn an unchanged signature or mutation into a new violation.

### 4.1 Guardrail tiers

The rules come in tiers.

- **Block rules** mean the code is fabricated, unsafe or wrong: code not copied from the shown source, new raw SQL or any DDL, formatted SQL, a removed permission check or `ignore_permissions` / `allow_guest`, `eval` / `exec`, pickle or marshal loads, shell commands, switching to Administrator, a manual commit, a process-wide cache, `enqueue` without `enqueue_after_commit=True`, the four headings, and every Frappe rule a pinned semgrep rule checks: untranslated messages, untyped whitelisted arguments, a positional `order` in `orderby`, `get_value` on a Single DocType, controller writes that are never saved, child rows changed while iterating, module or request-proxy state, and an unchecked `has_permission`. Optimus sends the model one follow-up turn listing every broken block rule and keeps the rewrite only if it has the four headings and breaks fewer block rules. Code that still breaks a block rule is removed, and a profiler note names each broken rule in plain English (the first two, then how many more). A heading problem alone never removes code.
- **Advise rules** are conventions no pinned semgrep rule checks: no dynamic imports, and no index led by a Frappe metadata column. They never cost a follow-up turn and never remove code: one profiler note lists them.
- **Notes** tell you about the answer: Customize Form has no index option, the model saw only part of the prompt, an image or an off-site link was removed, captured-data markers were echoed.

An answer cut off at the output limit is never re-asked and its code is removed. A reply longer than 16,384 characters (16 per token of the largest budget; only a server that ignores `max_tokens` sends one) is cut there and treated the same way, so the checks stay fast. The follow-up turn is on by default; set `optimus_ai_reask` to `false` in site_config to skip it on a slow local model.

### 4.2 Context window and answer size

Each provider has a context window in `_PROVIDER_DEFAULTS` (`context_tokens`): Anthropic 200,000, OpenAI 128,000, Kimi 128,000, DeepSeek 64,000, OpenAI-compatible 4,096. For OpenAI-compatible servers set **Optimus Settings > AI > Context window (tokens)** to the window your server really has (0 uses Optimus's 4,096-token budget). Optimus reserves a fifth of the window for the answer (between 512 and 1,024 tokens), sizes the user message to the rest, and refuses with a clear message when the window cannot hold the prompt. Sizes count every non-ASCII character (Chinese, Japanese, Arabic, Hebrew, accented letters) as a whole token, to reduce underestimation for those scripts; token counts remain estimates. Offline corpus checks fit the first request inside 4,096 tokens. A follow-up is sent only when the first call's reported usage leaves room; live model quality still requires evaluation.

The code guardrails apply to finding fixes in every rendered block, regardless of its fence label or section. Static checks do not establish that a suggestion fixes the performance problem. Review its semantics before applying it.

## 5. Eligible finding types

These four finding types are the only ones for which Optimus builds an AI payload. Infrastructure, frontend and call-tree findings lack enough code or SQL context. Index findings receive deterministic recipes; Framework N+1 findings refer to framework code the app cannot change. A Hot Line is also skipped when it sits in framework code, when Phase 1 named the function that holds its time, or when Phase 2 measured at least 1,000 microseconds per hit (`per_hit_us`) on a line that calls a non-builtin (`ai_grounding.HOT_LINE_CALLEE_US`). The report shows an explanation instead. If the gate itself raises, it fails closed: no AI call, and a neutral note that the check could not run.

- Hot Line
- N+1 Query
- Redundant Call
- Slow Query

Redundant Call findings recorded before the callsite correction are excluded from AI suggestions. Newly analyzed findings carry a `callsite_walk: outermost_first` stamp. Re-record the flow to obtain a corrected finding. Stored suggestions remain in the database. Regeneration hides index, Framework N+1 and gated Hot Line suggestions. An older Redundant Call suggestion stays visible with a re-recording caveat. Every displayed suggestion from an earlier prompt version is marked outdated.

The exact set lives in `optimus/ai_fix.py::AI_ELIGIBLE_FINDING_TYPES`. A test (`test_ai_privacy.py::TestDocStaysFresh`) compares this section's list against the frozenset to make sure the doc never drifts from the code.

### 5.1 Per-type opt-out (`ai_excluded_finding_types`)

`Optimus Settings → AI → Privacy & Operations → Excluded finding types` is a multi-line list. Each line names one of the four types above (exact match, case-sensitive). Lines starting with `#` are comments. Listed types are skipped in **both** auto-suggest and on-demand calls no payload is built, no request is sent, Refresh AI suggestions counts them as excluded in its toast, and the analyze-time step adds an analyzer note.

Use this when the SQL or source for a particular finding category embeds business logic you don't want flowing to a hosted provider:

```text
# We're fine sending N+1 patterns to Anthropic, but our pricing-rule SQL
# embeds margin formulas: skip Slow Query and N+1 Query.
Slow Query
N+1 Query
```

The list is empty by default. The exclusion list is **additive**: types not listed continue to flow normally.

### 5.2 Errors, Refresh AI suggestions and the analyze-time step

`ai_fix.AiFixError` carries a `kind`: `config` (Optimus Settings cannot serve the call: AI off, no model or key, a context window too small), `not_eligible` (the gate or the per-type exclusion refused the finding and no request was built), `transport`, `timeout`, `bad_response` and `unknown`. **Only `not_eligible` counts as a skip** (`AI_SKIP_KINDS`); every other kind, `config` included, is a failure that is logged to the Error Log and counted.

`optimus.api.refill_ai_suggestions` returns, in its `fixes` result, `added`, `failed`, `skipped_time`, `skipped`, `gated` (findings that get advice or a note from Optimus, or no AI suggestion by design), `excluded` (AI-eligible types excluded in Optimus Settings) and `skipped_ineligible`. The old `indexes` result is removed: `optimus.api.ai_capabilities` always reports `indexes: false`. Missing and outdated suggestions are refreshed first, so repeated refreshes make every eligible finding current.

The analyze-time step touches the single-flight flag (the Redis key that stops two analyses from overlapping) before every AI call, and only while it still holds that flag, so it never takes over another session's flag. Each call's timeout is capped at 240 seconds, below the flag's 300-second lifetime, so two heartbeats are never further apart than the flag lives.

---

## 6. Keep data on-box: local-LLM recipes

To keep finding context entirely on your bench host, run an OpenAI-compatible server locally and configure Optimus to point at it. No external network call leaves your machine. Three known-good stacks:

### 6.1 Ollama

```bash
# Pull a code-aware model. 8B fits on a 12 GB GPU; for 16 GB+ use 14B+.
ollama pull qwen2.5-coder:7b   # or llama3.1:8b, deepseek-coder-v2:16b

# Start the daemon (defaults to 127.0.0.1:11434).
ollama serve
```

Optimus Settings → AI:

- Provider: `OpenAI-compatible`
- Base URL: `http://localhost:11434/v1`
- Model: `qwen2.5-coder:7b` (the exact tag you pulled)
- API Key: leave blank
- Context window (tokens): leave 0 for Optimus's 4,096-token budget; check your server's effective window. For better answers start Ollama with a larger window (`OLLAMA_CONTEXT_LENGTH=8192 ollama serve`, or `PARAMETER num_ctx 8192` in a Modelfile) and enter the same number here. Ollama's OpenAI endpoint cannot change the window per request.
- `ai_request_timeout_seconds`: `180` (first-token cold-start can exceed 60s)

First call after restart pays the model-load cost (often 5–30s on CPU; <1s on a warm GPU). Subsequent calls are fast.

### 6.2 LM Studio

Open LM Studio, load a model, switch to the **Local Server** tab, **Start Server**. It listens on `http://localhost:1234/v1` by default.

Optimus Settings → AI:

- Provider: `OpenAI-compatible`
- Base URL: `http://localhost:1234/v1`
- Model: the model identifier shown in LM Studio (often a path-style name like `bartowski/Qwen2.5-Coder-7B-Instruct-GGUF`)
- API Key: leave blank
- `ai_request_timeout_seconds`: `180`

### 6.3 vLLM

```bash
# Production-grade local serving. Requires a GPU.
vllm serve Qwen/Qwen2.5-Coder-7B-Instruct \
    --host 0.0.0.0 --port 8000
```

Optimus Settings → AI:

- Provider: `OpenAI-compatible`
- Base URL: `http://localhost:8000/v1`
- Model: `Qwen/Qwen2.5-Coder-7B-Instruct`
- API Key: leave blank
- `ai_request_timeout_seconds`: `120` (vLLM is the fastest of the three once warm)

### 6.4 Picking a starting timeout

| Stack | Cold start (no GPU) | Cold start (GPU) | Warm call |
|---|---|---|---|
| Ollama (7-8B) | 10–30s | 1–3s | 1–5s |
| LM Studio (7B) | 5–20s | 1–2s | 1–4s |
| vLLM (7B) | n/a (GPU only) | <1s | <1s |

Default `ai_request_timeout_seconds = 60` is fine for hosted providers (Anthropic / OpenAI typically respond in 2–10s). For local stacks, **start at 180** and tune down once you've measured your warm-call P99. The setting is clamped to `[10, 600]` seconds.

---

### 6.5 Troubleshooting

- **"The model's context window (N tokens) is too small for the Optimus prompt."** The window cannot hold the system prompt plus a minimal answer (about 3,100 tokens). Raise the server's window and the Context window (tokens) setting together.
- **A suggestion ends with "the model saw only part of the prompt".** The server reported far fewer prompt tokens than Optimus sent, which is what Ollama does when its real `num_ctx` is smaller than the prompt (it drops tokens silently). Set `OLLAMA_CONTEXT_LENGTH` (or `num_ctx`) and the Context window (tokens) setting to the same value.
- **"the suggested code was removed because it was cut off at the output limit".** The answer hit its token budget. A larger context window raises the budget (up to 1,024 tokens).
- **"the suggested code was removed because it broke these Frappe rules: ..."** The model's code broke a block rule it was told about and the one follow-up turn did not fix it; the note quotes each rule (the first two, then "And N more"; section 4.1 lists them all). Review the diagnosis and treat the fix as a direction.
- **"change the suggested code before you apply it: ..."** The code is kept, but it breaks a convention the profiler only advises on (a dynamic import, index advice led by a metadata column). Apply the listed changes when you copy the code.
- **HTTP 400 mentioning the context length.** Same fix as the first item.

## 7. Threat model

What this design protects against:

- **Accidental egress.** With `ai_enabled = OFF` (default) no request body is ever built there's no code path that exfiltrates finding data.
- **Click-to-send.** With `ai_auto_suggest = OFF` the LLM only sees a finding when the operator explicitly clicks the per-finding button; every send is then a deliberate, attributable action. Auto-suggest is on by default, so once AI is enabled the top-N eligible findings are sent during the analyze pass unless you turn it off.
- **Category-level opt-out.** `ai_excluded_finding_types` lets you keep specific categories (e.g. Slow Query, where raw SQL flows verbatim) out of the wire entirely.
- **Network-residency.** The OpenAI-compatible provider + a local LLM keeps everything on your host. You can verify with `tcpdump` / `lsof` / `netstat` that no outbound socket opens during an AI call.

What this design does **not** protect against:

- **A compromised LLM provider.** If you're using Anthropic / OpenAI / a third-party, your finding context is at the mercy of their logging, retention and abuse-monitoring policies. Read each provider's data-use policy.
- **On-disk caching by the LLM client.** Local servers (Ollama, LM Studio, vLLM) may log requests to disk depending on their flags. Check their docs and configure logging off if you're paranoid.
- **Backups and audit logs.** The AI suggestion (the response text) is persisted to `Optimus Finding.llm_fix_json`. Your DB backups include it. If a fix suggestion contains a paraphrase of sensitive code/SQL, it'll be in those backups.
- **AI failure rows in the Error Log.** Optimus's own AI failures are written by `ai_fix.log_ai_failure`, the only function on the AI surface that writes an Error Log row: one row per failure, linked to the Optimus Session, with an explicit message scrubbed of secrets by `redaction.scrub_secrets`. For an HTTP failure it names the provider, the call site, the status and the provider's error code (`provider_error=`, only when it is made of lowercase words: letters joined by `_`, `.`, `:` or `-`, at most 64 characters), never the prompt, the reply body or any frame's local variables. That holds when this row cannot be written and the caller logs the error instead: the caller's row shows the status, the call site and `provider_error=` in place of the error's message, which can quote the reply. When such a row may be missing (its write failed; after a rollback, its existence could not be checked or it could not be queued again; or the rollback callback could not be registered), or a hook that runs after the insert failed (a broken Error Log notification, say: the row is then written, and a caller that logs the same error again writes a second row), one line naming only the error type goes to the `optimus` log (`logs/optimus.log`) at error level, the lowest level Frappe's loggers keep on a production site. These rows are in your backups like any other Error Log row. Frappe's own error snapshots (a server error, a background job that fails or times out, any error in developer mode) still print frame locals, which can include prompt text and a custom Base URL typed with credentials in it; see `SECURITY.md`. An Error Log `before_insert` hook (`optimus.error_log_mask`) masks the stored key, the key shapes `scrub_secrets` knows (among them an `x-api-key: <key>` header line and a header value quoted in an "Invalid header value" error, so a rotated key in those forms is masked too) and the bare header value lines in every Error Log row from Optimus's AI code (an `optimus/ai_fix.py` or `frappe_profiler/ai_fix.py` frame in its error, title or metadata) or holding the stored key, as Frappe inserts it, the rows Frappe inserts from its deferred-insert queue in Redis included; every other row, another app's included, is stored exactly as it was. It normally reads the stored key once per Error Log insert when one is stored (no key is read on a site where none is stored), cuts each text field of a row it masks to 65536 characters, and never raises (except an RQ job timeout, which still stops the job). A row from Optimus's AI code is withheld whenever it cannot be masked ("Optimus withheld this error text: it could not be masked. See logs/optimus.log for the reason."). After an in-place upgrade, a process still running the previous release withholds a row from the AI code and masks only the stored key in other rows, and only once it reads the new hooks. While Optimus profiles a flow, the per-table and per-action breakdowns, index suggestions, the N+1 and slowest-query findings and the session's query count and query time leave these reads out, as Optimus's own queries. When the caller logs an AI failure the HTTP layer already logged, its context (title and `k=v` lines, scrubbed) is appended to the row already written. The scrub's `residual` count checks the stored key first, then known provider key shapes: 0 means no current key and no known provider-shaped key is left in the rows it reads. `SECURITY.md` ("API key handling" and "Known limitations") says when the hook takes effect after an upgrade, and why an image-based or rolling deployment must stop or replace every old process before the migrate.

An unexpected hook failure triggers one independent stored-key read with
Frappe alone. A row holding that key, including its JSON-escaped or repr-escaped form, is withheld even without an AI frame. A row from the AI code is
withheld whenever it cannot be masked; unrelated rows remain unchanged. Quoted
header values cut off before their closing quote are masked through the end of
that line, including keys that have since been rotated.

---

## 8. For the dev shop receiving a profile

The "safe report" HTML file Optimus produces (the dev-shop interchange format) **does not** call any LLM at render or open time. When the operator sends you a profile:

- The report is fully self-contained no CDN, no remote fetch on open.
- AI fix suggestions, if any, are **baked into the report** at analyze time. The HTML embeds the suggestion text as static markup; opening the report locally never triggers an AI call.
- The dev shop doesn't need an API key / provider configured to read the report they need it only if they want to **regenerate** suggestions on their own bench.

The JSON export (`optimus.api.export_session`) carries no raw DDL and no `ai_index`. Each index-family finding gains `index_advice` (`route`, `doctype`, `table`, `columns`, `index_name`, `text`, `code`), its `technical_detail.fix_hint` is the same text the report shows, and a table's `recommended_index` carries `requested_columns` (the columns the analyzer asked for, before the advisor changed them). The permission check is unchanged.

This means: if you're worried about a profile shared with a third party leaking your code to their LLM provider, the answer is "the profile itself doesn't." But it also means: AI suggestions baked into the report carry the same content the LLM produced review those before sharing if they paraphrase sensitive logic.

---

## 9. Where the code lives

| Concern | File | Symbol |
|---|---|---|
| Index advice (findings and table cards) | `optimus/renderer/index_recipes.py`, `optimus/renderer/recipe_enrichment.py` | `advise`, `advise_finding`, `advise_table`, `ensure_indexes_code`, `make_evidence_lookup` |
| AI grounding and eligibility | `optimus/ai_grounding.py` | `grounding_window`, `loop_facts_from_tree`, `format_loop_facts`, `hot_line_gate` |
| Best-effort calls and job timeouts | `optimus/safe_call.py` | `best_effort`, `InterruptGuard` |
| Eligible-types frozenset | `optimus/ai_fix.py` | `AI_ELIGIBLE_FINDING_TYPES` |
| Provider matrix | `optimus/ai_fix.py` | `_PROVIDER_DEFAULTS` |
| Payload builders | `optimus/ai_fix.py` | `_build_fix_request` (`_build_messages` wrapper), `_build_steps_messages` |
| Prompt text | `optimus/ai_prompts.py` | `SYSTEM_PROMPT`, `FINDING_TYPE_HINTS`, `RULE_TEXT`, `PROMPT_VERSION` |
| Answer verification | `optimus/ai_guardrails.py` | `verify_fix`, `reask_message`, `apply_fallback` |
| Context budget | `optimus/ai_budget.py` | `user_char_budget`, `data_block`, `assemble`, `reask_fits` |
| HTTP layer | `optimus/ai_fix.py` | `_http_post` |
| API key on the request (masked `repr`; the key never enters a header dict of Optimus's) | `optimus/ai_fix.py` | `_ApiKeyAuth` |
| AI failure rows (the only Error Log writer on the AI surface) | `optimus/ai_fix.py` | `log_ai_failure` |
| Secret scrubbing of log text | `optimus/redaction.py` | `scrub_secrets` |
| Cleaning keys out of old Error Log rows | `optimus/maintenance.py` | `scrub_error_log_secrets`, `purge_ai_error_logs` |
| Masking the key in Error Log rows as Frappe inserts them | `optimus/error_log_mask.py` | `mask_error_log` (the Error Log `before_insert` hook) |
| Per-type exclusion gate | `optimus/ai_fix.py` | `is_finding_type_excluded` |
| On-demand entry points | `optimus/api.py` | `refill_ai_suggestions` (the "Refresh AI suggestions" button), `test_ai_connection` (Optimus Settings), `ai_capabilities` (which AI buttons the form shows) |
| Auto-suggest entry point | `optimus/analyze.py` | `_enrich_findings_with_ai_suggestions` |
| Settings dataclass | `optimus/settings.py` | `OptimusConfig` |

---

## 10. Aerele Managed Provider (v0.14.x+)

`Aerele` is a hosted option for customers who don't want to bring their own Anthropic / OpenAI key. The customer purchases a **fixed token pack** (e.g. "10,000 tokens for ₹X") up front; AI fix calls draw from that pack until it's exhausted, at which point the customer buys another pack. There is no subscription, no monthly reset and no overage when the pack runs out, calls are refused until a new pack is purchased.

**Architecturally the Optimus side is identical to the Anthropic / OpenAI / Kimi / DeepSeek entries:** the operator picks `Aerele` as the provider, pastes the key Aerele issued into **API Key** and every call hits Aerele's URL. There is no Optimus-side bookkeeping no balance cache, no pre-call gate, no Refresh button, no daily sync. **All token accounting and pack validation happens on Aerele's separate Frappe site** (the URL in the provider matrix above). The bench is a dumb client.

### 10.1 Onboarding

1. Sign up at [aerele.in/optimus/signup](https://aerele.in/optimus/signup) and purchase a token pack at [aerele.in/optimus/billing](https://aerele.in/optimus/billing).
2. In Optimus Settings ▸ AI Fix Suggestions:
   - Set **Provider** to `Aerele`.
   - Paste the issued key into **API Key**.
   - Save.

That is the entire integration. `ai_base_url` and `ai_model` use Aerele's defaults (`https://api.aerele.in/optimus/v1` + the upstream model Aerele has provisioned for the customer); leave them blank unless Aerele tells you otherwise.

### 10.2 Where the token pack lives

The customer manages their pack entirely on `aerele.in`: sign-ups, purchases, remaining-balance display, usage history. Optimus never sees, displays, or caches the remaining balance. Each AI call is validated server-side on every request by Aerele's Frappe site; pack-exhausted and rate-limit refusals surface through `_http_post`'s existing 4xx handling with the response body's error text.

When a pack runs out, the next AI fix attempt from Optimus surfaces Aerele's "pack exhausted purchase a new one" message as an inline `AiFixError` alert. The operator then visits aerele.in, buys another pack and the existing API key automatically draws from the new pack no Optimus re-configuration needed.

### 10.3 What additionally leaves the host

Compared to the per-pathway data inventory in § 2, picking `Aerele` adds nothing structural over what the other hosted providers already send:

- The customer's Aerele API key as `Authorization: Bearer <key>` on every call to `api.aerele.in`.
- The same OpenAI-shaped finding / steps payload from sections 2.1 and 2.2.

What is **NOT** sent (matches § 3):

- The bench's `encryption_key` or any other site secret beyond the Aerele key itself.
- Cross-session correlation IDs, recording UUIDs, schema, or DocType names beyond what the finding-specific payload already includes.
- Heartbeat / pack-status / usage-counting calls. Aerele tracks consumption from the actual `/chat/completions` traffic; the bench never pings out otherwise.
