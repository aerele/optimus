# Contributing to Optimus

Optimus is the Aerele-maintained Frappe performance profiler. It
ships as an installable Frappe v16 app (not a SaaS, no separate
frontend bundler). This guide covers the day-to-day contribution
flow.

## Local setup

```bash
# In your Frappe bench:
bench get-app https://github.com/Aerele-RnD/optimus.git
bench --site <yoursite> install-app optimus
bench --site <yoursite> migrate

# Optimus 0.7.x is fresh-deploy-only. If you have the legacy
# ``frappe_profiler`` installed, uninstall it first:
bench --site <yoursite> uninstall-app frappe_profiler
```

## Test gate

Every PR must pass the full test suite and `ruff check`:

```bash
# Full suite (~6 seconds, 1450+ tests):
cd <bench-root> && env/bin/python -m pytest apps/optimus/optimus/tests/ -q

# Lint:
env/bin/python -m ruff check apps/optimus/optimus/

# Coverage (needs pytest-cov; run from the app dir so pyproject.toml is read):
cd <bench-root>/apps/optimus && ../../env/bin/python -m pytest optimus/tests/ --cov --cov-report=term-missing:skip-covered
```

The `test_pdf_export.py` 3-test module requires a writable
`logs/cssutils.log` to run; CI ignores it via
`--ignore=apps/optimus/optimus/tests/test_pdf_export.py`. Adjust as
needed for your bench setup.

## Integration tests (real bench)

The unit suite above runs in seconds against a Frappe stub. The
integration suite at `optimus/tests_integration/` runs against a real
bench real MariaDB, real Redis, real RQ and catches regressions in
the inter-component handoff the unit stubs can't reach.

In CI: `.github/workflows/integration.yml` provisions a fresh Frappe
v16 bench via `.github/helper/install.sh`, installs optimus and runs
the integration modules. It runs on pull requests to `main`, on push
to `main`, daily at 04:00 UTC and on manual dispatch.

Locally:

```bash
cd <bench-root>
bench --site <your-site> run-tests --app optimus \
    --module optimus.tests_integration.test_install_smoke

bench --site <your-site> run-tests --app optimus \
    --module optimus.tests_integration.test_recording_lifecycle_e2e
```

Adding a new integration test: read `optimus/tests_integration/README.md`
for the harness pattern, the deferred-test extraction roadmap and the
"no flakiness" rule. Integration tests must justify the bench cost
prefer a unit test when feasible.

## Pre-commit

Install the hook once per clone:

```bash
pip install pre-commit
pre-commit install
```

`.pre-commit-config.yaml` runs `ruff check --fix` on every commit.
Format-checking is intentionally NOT included - a one-shot reformat
is a separate, deliberate commit.

## Branch + commit conventions

- **Branch names** follow `<type>/<short-description>`:
  `feat/sticky-nav`, `fix/em-dash-sweep`, `chore/rate-limit`.
- **Commit messages** follow Conventional Commits with a `v0.7.x`
  scope hint:
  ```
  feat: v0.7.x sticky nav-pills + GitHub-Light syntax theme

  - Drop K.1 / K.3 / K.4 reverts; keep K.0 (highlighting) + K.2.
  - Adjust hot-line opacity for the new bg.
  - 1450 tests green; ruff clean.
  ```
- **No `Co-Authored-By: Claude`** trailer.
- One sprint per commit when changes are coherent; split commits
  per concern when they're cross-cutting.

## Memory + plans

Maintainers keep a local memory + plan folder (outside this
repo) to track work across sessions. External contributors don't
need to interact with these; they're maintainer artifacts.

## Frozen surfaces

These areas are deliberately stable and should NOT be modified
without a separate design discussion:

- **Capture pipeline** (`capture.py`,
  `infra_capture.py`, `analyze.py`) - bug fixes only. New
  behaviour belongs at render time (the `renderer/` package,
  `report_context.py`).
- **DocType field schemas** - additive only; column removals
  require a versioned patch in `optimus/patches/v_*`.
- **Public API surface** in `api.py` - whitelisted endpoints are
  load-bearing for the floating widget and external CLIs.

## The local subscription CLI transport (Codex and Claude)

The local subscription CLIs (Codex and Claude) are a transport, not a provider. The Subscription CLI field (`ai_subscription_cli`, "Codex CLI" or "Claude CLI") picks the backend: `_cli_backend(cfg)` maps it to a `_CliBackend` (`_CodexBackend` or `_ClaudeBackend` in `ai_fix.py`), reading a never-saved field as Codex on the live and the snapshot side alike. `_provider_config` puts its `backend` key in the provider identity. A backend is data (binary name, install shapes, copy, env switches) plus the callables that really differ (login check, argv, reply reader, error classifier); the gate, sandbox, run, scrub, truncation, deadline, bulk cap and the Codex auth retry stay in the shared `_call_local_cli`. Both backends use your real terminal login (`~/.codex` or `~/.claude`), keep no token of their own and are dev-only behind the same two flags. Adding a third CLI means a new backend class, its Select option (kept equal to `_CLI_BACKENDS`, pinned by a test) and its tests. The Claude backend reports failures in its JSON result with any exit code, so its `read_reply` raises a classified `AiFixError` and never falls back to raw stdout.

`_provider_config` returns a synthetic `protocol == "subscription"` provider that is deliberately not in `_PROVIDER_DEFAULTS` or the `ai_provider` Select. It is chosen through `ai_connection_type`, routed by `_dispatch_call` to `_call_local_cli`, and gated by `developer_mode` plus the `optimus_allow_subscription_cli` site flag. This is the one exception to "never add per-provider branches": keep its branch inside provider resolution, availability and the connectivity test, the dispatch layer, `_call_local_cli` and the helpers they call, and the Settings validator. The one other place that knows about it is the generic bulk admission bound, `ai_fix.bulk_batch_cap`, called by `ai_jobs.start_refresh` and `ai_refresh_view.refresh_plan`: it is the shared cap mechanism (it clamps every bulk run to `SUBSCRIPTION_BULK_MAX_FINDINGS` in this mode), not a provider branch.

## Areas that welcome contribution

- `renderer/` UX polish (panels, layout, accessibility).
- New analyzers in `analyzers/` (each is a pure function that
  consumes the analyze-context and emits findings).
- Test coverage gaps - especially around rare data shapes.
- Documentation, CHANGELOG entries, README clarifications.

## Reporting security issues

See [SECURITY.md](SECURITY.md) for the responsible-disclosure flow.
**Do not file public issues for security bugs.**
