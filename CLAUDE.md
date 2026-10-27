# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Optimus is a flow-aware performance profiler for Frappe and ERPNext. A user records a request flow from a floating Desk widget, the capture layer collects SQL, Python call trees, server resources and frontend timings, an analyze pipeline turns that into findings, and the renderer emits one self-contained HTML report.

## Commands

Unit tests are decoupled from Frappe (a `conftest.py` stubs `frappe`), so no site is needed:

```
python -m pytest optimus/tests/                                                     # full unit suite
python -m pytest optimus/tests/test_ai_fix.py::TestOpenAiCall::test_extracts_choice_content   # single test
```

Test-only deps are `pytest`, `hypothesis`, `Faker`, `pdfplumber`, `pypdf`, `sql_metadata` and `jsonschema`; the AI quality job also installs its pinned RQ and Semgrep dependencies. Use the CI dependency layout when reproducing failures. A few PDF-export tests write a cssutils log to `../logs/`, so create that directory if a run fails on a missing log file.

Integration tests need a disposable real Frappe v15 or v16 bench and site:

```
bench --site <site> run-tests --app optimus
```

For JS changes run `node --check optimus/public/js/<file>.js` (`tests/test_frontend_assets.py` guards this). Bump `__version__` in `optimus/__init__.py` for any user-visible change: it is the asset cache-buster.

The report is a Jinja template (`optimus/templates/report.html`) rendered at runtime, so template or CSS edits need no asset build. They take effect on the next render (a new analyze pass, or the session's "Regenerate Reports").

## Architecture

The flow is capture then analyze then render.

- **Capture** (`capture.py`, `session.py`, `infra_capture.py`, `redaction.py`): reuses Frappe's built-in `frappe.recorder` for SQL instead of forking it. Per-user activation is done by hook ordering in `before_request`; background jobs inherit the session through a wrapped `frappe.enqueue` that injects `_profiler_session_id`, which the worker's `before_job` hook pops. Sensitive values are redacted at capture time. Recordings live in Redis (`redis_keys.py`, `redis_schema.py`).

- **Analyze** (`analyze.py`, `analyzers/`, `line_profile/`): each analyzer is a pure `analyze(recordings, context) -> AnalyzerResult` function that is testable from JSON fixtures with no DB access. `_BUILTIN_ANALYZERS` in `analyze.py` is the registry; a site-config value or a `hooks.py` `optimus_analyzers` entry can add more. Phase-2 line-level profiling lives in `line_profile/`.

- **Render** (`renderer/`, `templates/report.html`): `report.html` is the single source of truth for BOTH the Safe (redacted) and Raw report modes; there is no second template. `renderer/_internal.py` is the orchestrator and per-concern submodules (`line_drilldown.py`, `source.py`, `syntax.py` and others) build the sections. `tests/test_renderer_structure_snapshot.py` locks the DOM structure (ids, class tokens, tag counts) as a template-contract canary; on a deliberate template change regenerate it with `REGENERATE_RENDERER_SNAPSHOT=1 python -m pytest optimus/tests/test_renderer_structure_snapshot.py`.

- **AI fix suggestions** (`ai_fix.py`, `ai_jobs.py`, `ai_refresh_store.py`): optional queued work after analysis or a manual refresh. SQL owns admission, claims, progress, attempts and usage; RQ delivers committed intents. Unknown provider outcomes are retained as uncertain and are not automatically repeated. Code is never auto-applied; Steps notes can be rewritten while user edits are protected. `_PROVIDER_DEFAULTS` contains provider data and reviewed model limits; unknown model IDs use a bounded fallback, not a model allowlist. `_call_openai_chat` and `_call_anthropic` implement wire-protocol differences. Adding a provider is data only; budget, privacy and orchestration policies must remain generic.

The AI quality workflow already runs real database concurrency tests on MariaDB
10.6, MariaDB 11.8 and PostgreSQL 16. Local SQL tests require a dedicated fake
`optimus_test` service; select it with `OPTIMUS_TEST_SQL` and
`OPTIMUS_TEST_SQL_PORT`, then run `optimus/tests/test_ai_refresh_sql.py`.
Never point these tests at an application database.

For the deeper data-flow and hook-ordering detail read the docstrings in `analyze.py` and `renderer/README.md`.

## Conventions

- Keep analyzers pure functions over recording dicts, with no Frappe or DB access, so they stay fixture-testable.
- Adding an AI provider is data only: extend `_PROVIDER_DEFAULTS` and the `ai_provider` Select in `optimus/optimus/doctype/optimus_settings/optimus_settings.json` together (they must match exactly, guarded by `test_ai_aerele_provider.py`), and never add per-provider branches in code.
- A new finding type goes in the enum in `optimus/optimus/doctype/optimus_finding/optimus_finding.json` with a doctype-reloading patch under `optimus/patches/v0_X_Y/`.
