# Real-bench integration tests

This directory uses the real Frappe v16 runner, MariaDB, Redis and workers.
The separate `optimus/tests/` suite uses controlled fakes when a bench is absent.
`.github/workflows/integration.yml` provisions a disposable bench using
`.github/helper/install.sh` and invokes the modules below individually. No real
AI provider is contacted by this workflow.

## Current CI coverage

| Module | Boundary exercised |
| --- | --- |
| `test_install_smoke` | Installation, declared DocTypes and readable Settings. |
| `test_recording_lifecycle_e2e` | Start, capture, stop and persisted profiling results through the real bench. |
| `test_atomic_lua_merge_concurrent` | Concurrent real Redis/Lua recording and status merge, first-writer values and job identity preservation. |
| `test_regenerate_reports_idempotent` | Attached report generation, deterministic repeated rendering and updated inputs. |
| `test_phase2_tool_orphan_recovery` | Monitoring-tool ownership and cleanup; real Redis capture admission, losing start, exact-generation stop, bounded counters, TTLs and eviction. |
| `test_safe_report_self_contained_on_real_bench` | Attached Safe HTML contains no external assets, scripts or bench-local references. |
| `test_janitor_sweeps_actually_delete` | Terminal-session retention and attached File deletion, with active sessions preserved. |

AI Quality separately runs `optimus/tests/test_ai_refresh_sql.py` against
MariaDB and PostgreSQL disposable tables. These are real isolation, lock and
transaction tests through a narrow adapter, not real Frappe document or
permission tests. Unit and real-RQ tests cover interruption, duplicate delivery,
uncertain usage, stale ownership, rollback and bounded retry interleavings.
The [acceptance checklist](../../docs/AI-ACCEPTANCE.md) distinguishes these
results from the pending end-user and deployment checks.

## Running tests safely

Use the CI-provisioned disposable bench for this suite. Some fixtures delete all
sessions belonging to the current test user; they are unsafe on a populated site.
Do not point this suite at a production site or assume a transaction rollback
undoes worker commits, File writes or Redis changes.

For this remediation's local acceptance, only `optimus.local` and
`optimus-pg.local` are approved. Back up first, use fake credentials and isolated
fixture identities, and delete exactly the created rows/files/keys. Starting
workers or services needs the owner's authorization. Never switch the owner's
checkout, run its migration or assume it imports a worktree. A worktree-based
probe must insert that path before imports and assert every loaded Optimus
module resolves there. Do not publish recording identities or row text.

CI runs this command on its freshly provisioned `test_site`:

```sh
bench --site test_site run-tests --app optimus \
  --module optimus.tests_integration.test_install_smoke
```

Repeat with a module listed above. The workflow records a separate log for each
and fails if any fails. It does not retry a failed test to produce green output.
Use the workflow's installed Python/Frappe versions when reproducing CI; local
framework differences can change collection and runner behavior.

## Fixture and runner distinction

`conftest.py` contains pytest fixtures. Frappe's unittest-based `bench run-tests`
does not execute pytest autouse fixtures. Each Frappe test class must arrange
its own setup/teardown. In particular, do not rely on `cleanup_session` to undo
a worker's committed writes. Its helper deletes every current-user Session,
so it is not acceptable cleanup for the owner's test sites.

New tests should use exact fixture identities and `try/finally` cleanup, preserve
unrelated state, and show which assertions require real Redis/SQL/Frappe rather
than a fake. Never invoke migration inside an initialized test process: its
teardown can destroy the runner's Frappe context. Test upgrade/rollback in a
separate authorized deployment rehearsal.

## Deferred integration coverage

| Check | Current limit and completion trigger |
| --- | --- |
| File hook matrix on Frappe 15 and 16 | Unit hook-loop tests are not a real installed-app permission test. Exercise owner, System Manager, stranger and read-sharee under the actual hook order before claiming this coverage. The accepted direct `/private/files` download gap is outside that hook; never assert it blocks that route. |
| Session owner/share/User Permission matrix | Unit gates are covered. Real owner, read-sharee, write-sharee, manager, stranger and User Permission restrictions still require isolated users and exact cleanup. |
| Upgrade/rollback on both databases | Fresh CI installation is not a populated-site upgrade or rollback proof. Verify schema/default preservation, scheduler hooks, mixed-process restrictions and backup restore in a deployment rehearsal. |
| Full background-user journey | Scripted interleavings do not replace browser/worker acceptance with provider loss, worker death, cancellation, permission changes, resume and report recovery. |
| Durable index recipes | Verify metadata choices survive schema synchronization on both databases before approving recipe acceptance. |

These gaps are tracked in [AI acceptance](../../docs/AI-ACCEPTANCE.md), not silently
waived by a green workflow. Add tests to an existing appropriate module when
possible. New workflow modules require review of the integration workflow's
frozen scope. Quarantine a confirmed flaky test with an issue and a clear
reason, then fix or remove it; do not mask it with unconditional retries.
