# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Optimus is a flow-aware performance profiler for Frappe and ERPNext. A user records a request flow from a floating Desk widget, the capture layer collects SQL, Python call trees, server resources and frontend timings, an analyze pipeline turns that into findings, and the renderer emits one self-contained HTML report.

## Commands

Unit tests are decoupled from Frappe (a `conftest.py` stubs `frappe`), so no site is needed:

```
python -m pytest optimus/tests/                                                     # full unit suite
python -m pytest optimus/tests/test_ai_fix.py::TestOpenAiCall::test_extracts_choice_content   # single test
```

Test-only deps are `pytest`, `hypothesis` and `jsonschema` (pip install them if a run errors on import). A few PDF-export tests write a cssutils log to `../logs/`, so create that directory if a run fails on a missing log file.

Integration tests require a disposable Frappe v16 bench and test site. Read
`optimus/tests_integration/README.md` first: its pytest fixtures do not run under
Frappe's unittest runner, and broad cleanup is unsafe on a populated site.
The separate SQL adapter tests exercise MariaDB and PostgreSQL transactions,
not the real Frappe permission engine.

For JS changes run `node --check optimus/public/js/<file>.js` (`tests/test_frontend_assets.py` guards this). Bump `__version__` in `optimus/__init__.py` for any user-visible change: it is the asset cache-buster.

The report is a Jinja template (`optimus/templates/report.html`) rendered at runtime, so template or CSS edits need no asset build. They take effect on the next render (a new analyze pass, or the session's "Regenerate Reports").

## Architecture

The flow is capture then analyze then render.

- **Capture** (`capture.py`, `session.py`, `infra_capture.py`, `redaction.py`): reuses Frappe's built-in `frappe.recorder` for SQL instead of forking it. Per-user activation is done by hook ordering in `before_request`; background jobs inherit the session through a wrapped `frappe.enqueue` that injects `_profiler_session_id`, which the worker's `before_job` hook pops. Sensitive values are redacted at capture time. Recordings live in Redis (`redis_keys.py`, `redis_schema.py`).

- **Analyze** (`analyze.py`, `analyzers/`, `line_profile/`): each analyzer is a pure `analyze(recordings, context) -> AnalyzerResult` function that is testable from JSON fixtures with no DB access. `_BUILTIN_ANALYZERS` in `analyze.py` is the registry; a site-config value or a `hooks.py` `optimus_analyzers` entry can add more. Phase-2 line-level profiling lives in `line_profile/`.

- **Render** (`renderer/`, `templates/report.html`): `report.html` is the single source of truth for BOTH the Safe (redacted) and Raw report modes; there is no second template. `renderer/_internal.py` is the orchestrator and per-concern submodules (`renderer/line_drilldown.py`, `renderer/source.py`, `renderer/syntax.py` and others) build the sections. `tests/test_renderer_structure_snapshot.py` locks the DOM structure (ids, class tokens, tag counts) as a template-contract canary; on a deliberate template change regenerate it with `REGENERATE_RENDERER_SNAPSHOT=1 python -m pytest optimus/tests/test_renderer_structure_snapshot.py`.

- **AI completion** (`ai_fix.py`, `ai_prompts.py`, `ai_budget.py`, `ai_guardrails.py`): provider configuration and bound credentials, bounded HTTP/parameter retries, prompt budgets, reported usage, whole-answer verification and scrubbed failures. The two wire handlers are `_call_openai_chat` and `_call_anthropic`. `_PROVIDER_DEFAULTS` is the provider registry; Aerele is disabled. Adding a provider must preserve URL, credential, context and logging controls.

- **AI background work** (`ai_jobs.py`, `ai_refresh_store.py`): core profiling and its report commit as Ready before optional AI admission. SQL Run/Attempt/Control records own progress, reservations, send intent and atomic result/usage accounting. Redis/RQ only deliver work. Claim and persistence fence duplicate or stale workers; no HTTP call runs under journal row locks. Unknown provider outcomes become uncertain, never an automatic billed retry. `ai_jobs.refresh_plan` and worker selection share the eligibility and input checks. `report_refresh.py` renders outside locks, then atomically replaces the report only after a fresh ownership check. Regenerate Reports makes no AI calls.

- **Phase 2 jobs** (`line_profile/jobs.py`, `line_profile/capture.py`): resolve picks/source before capture admission locks; store the immutable capturing actor and exact Redis generation. SQL journals queued analysis and bounded attempts. Optional AI and an active Phase 2 pass exclude one another. Failed/expired input requires explicit recovery or a new capture, not successful empty findings.

- **Trust boundaries** (`recording_bundle.py`, `ai_privacy.py`, `renderer/source_resolution.py`, `server_script_source.py`, `error_log_mask.py`, `maintenance.py`): bounded JSON-only persisted recordings; default-private SQL/Steps prompts; canonical source allowlists and Server Script permissions; the site-wide Error Log mask and historical scrub. Privacy does not anonymize source code or schema. See `SECURITY.md` for the accepted direct-download and DNS/telemetry limitations.

### Shared AI internals

These underscore names in `ai_fix.py` are intentional cross-module contracts,
not whitelisted APIs. Preserve their callers and regression tests when changing them:

- `_InterruptGuard`, `_job_timeout_types`: scrub escaping interrupts, preserving RQ control flow, reported usage and the already-logged marker.
- `_with_usage_on_failure`: retain reported cost when post-response processing fails.
- `_current_key_or_empty`: guarded key retrieval for secret masking, never diagnostics.
- `_key_is_sendable`, `_unsendable_key_message`: shared Settings and dispatch validation.
- `_PROVIDER_DEFAULTS`, `_DEFAULT_PROVIDER`, `_DEFAULT_PORTS`: provider selection and normalized endpoint identity in Settings and migration checks.
- `_allow_key_over_http`: strict operator consent for non-loopback HTTP credentials.
- `_resolve_timeout_seconds`: shared request and worker budgeting.
- `_SOURCE_LINES_BEFORE`, `_SOURCE_LINES_AFTER`: consistent grounding windows.

The AST documentation check resolves module aliases and direct imports so a new
cross-module dependency must be documented. The runtime logging audit remains
separate; documenting a helper does not exempt it from that audit.

For data flow and hook ordering read `analyze.py` and `renderer/README.md`.
Use `docs/AI-FIXING.md` for payloads and guardrails, `docs/AI-REFRESH.md` for
operations, and `docs/AI-ACCEPTANCE.md` for evidence and release gates. Model
acceptance must not be inferred from unit tests or a green AI Quality workflow.

## Conventions

- Keep analyzers pure functions over recording dicts, with no Frappe or DB access, so they stay fixture-testable.
- Adding an AI provider is data only: extend `_PROVIDER_DEFAULTS` and the `ai_provider` Select in `optimus/optimus/doctype/optimus_settings/optimus_settings.json` together (they must match exactly, guarded by `test_ai_aerele_provider.py`), and never add per-provider branches in code.
- A new finding type goes in the enum in `optimus/optimus/doctype/optimus_finding/optimus_finding.json` with a doctype-reloading patch under `optimus/patches/v0_X_Y/`.
