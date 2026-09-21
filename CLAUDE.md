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

Integration tests need a real Frappe v16 bench and site:

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

- **AI fix suggestions** (`ai_fix.py`): an optional LLM client behind the "Suggest a fix" action, kept fully data-driven. `_PROVIDER_DEFAULTS` maps each provider display name to its protocol, base_url and model; `_resolve_provider` reads the `ai_provider` Select from Optimus Settings and uses that string directly as the dict key; `_call_openai_chat` and `_call_anthropic` are the only two protocol handlers. Everything else is generic, so adding a provider is data only.

For the deeper data-flow and hook-ordering detail read the docstrings in `analyze.py` and `renderer/README.md`.

## Conventions

- Keep analyzers pure functions over recording dicts, with no Frappe or DB access, so they stay fixture-testable.
- Adding an AI provider is data only: extend `_PROVIDER_DEFAULTS` and the `ai_provider` Select in `optimus/optimus/doctype/optimus_settings/optimus_settings.json` together (they must match exactly, guarded by `test_ai_aerele_provider.py`), and never add per-provider branches in code.
- A new finding type goes in the enum in `optimus/optimus/doctype/optimus_finding/optimus_finding.json` with a doctype-reloading patch under `optimus/patches/v0_X_Y/`.
