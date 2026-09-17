# Plan: Jarvis-style CI reorg + main branch protection
STATUS: APPROVED
Date: 2026-08-16
Owner: Fable (team leader)

## Goal
Replace the duplicate-running `Tests` workflow with a single, efficient `CI`
workflow whose checks render as `CI / lint`, `CI / tests`, `CI / coverage`,
`CI / pip-audit` — no `(push)`/`(pull_request)` twins — and add a strict
protection rule on `main` that requires those checks plus the existing
`Integration / real-bench integration` check.

## Context
Today `tests.yml` (`name: Tests`) triggers on BOTH `push: branches: ['**']`
and `pull_request: branches: ['**']`. On any branch with an open PR both
events fire for the same commit, and the `concurrency` group is keyed on
`github.ref` — which differs between a push (`refs/heads/<b>`) and a PR
(`refs/pull/N/merge`) — so `cancel-in-progress` does NOT dedupe them. Result:
every job (`pytest`, `ruff`, `pip-audit`) runs twice. That is the inefficiency
in Image 1 (7 checks = 3 duplicated jobs + integration).

Target style (Jarvis, Image 2): a single `CI` workflow, PR-triggered, with
clean `CI / <job>` names and a `coverage` job. Optimus differences that change
the translation: no frontend (`package.json` absent) → no `frontend-*` jobs;
pytest suite runs in ~43s → sharding into 4 would add setup overhead and run
SLOWER, so no sharding.

`main` is currently unprotected (`protected: false`) and there are no rulesets,
so renaming checks breaks no existing required-status-check gate. Adding
protection is net-new.

Alternatives considered:
- *Keep two events, dedupe via concurrency*: rejected — the ref differs between
  push and PR, so a single concurrency group can't collapse them cleanly. The
  branch-scoped trigger split (`push: [main]` only) is the idiomatic fix.
- *Shard tests like Jarvis*: rejected — 43s suite, sharding is counterproductive.
- *Fold integration into CI*: rejected by user — integration has a distinct
  trigger surface (nightly cron, push/PR to main only) and heavy services.

## Architecture / approach
**Add `.github/workflows/ci.yml`** (`name: CI`); **delete `tests.yml`**;
**edit `integration.yml`** only to resolve the paths-ignore/required-check
conflict (see Edge case 1). Integration otherwise stays its own workflow.

`ci.yml`:
- Triggers: `pull_request` (branches `['**']`) + `push` (branches `[main]`) +
  `workflow_dispatch`. This is the dedup fix: a feature branch with an open PR
  runs only `pull_request`; a merge to `main` runs only `push`.
- `concurrency: { group: ${{ github.workflow }}-${{ github.ref }},
  cancel-in-progress: true }` (kept).
- `permissions: { contents: read }`.
- Jobs (each renders as `CI / <job-name>`):
  - `lint` — `ruff check optimus/` (logic unchanged from today's `lint` job).
  - `tests` — pytest on Python 3.14 with the matrix DROPPED (single version) so
    the check name is `CI / tests` not `pytest (Python 3.14)`. Preserves the
    load-bearing bits: `set -eo pipefail` before the `pytest … | tee` pipeline,
    the `-rA` summary written to `$GITHUB_STEP_SUMMARY`, and the failure-log
    artifact upload. Same dependency install as today.
  - `coverage` — NEW, SEPARATE job. Installs the same test deps plus
    `pytest-cov`; runs `pytest optimus/tests/ --cov=optimus
    --cov-report=term-missing --cov-report=xml` (same `set -eo pipefail`
    discipline); writes the coverage table to `$GITHUB_STEP_SUMMARY`; uploads
    `coverage.xml` as an artifact. INFORMATIONAL: no `--cov-fail-under`, so the
    job goes red only if the suite itself fails, never on a coverage number. A
    threshold is a deliberate follow-up once we see the baseline.
  - `pip-audit` — `pip-audit --skip-editable` (logic unchanged).

**Branch protection (Strict / max)** applied to `main` AFTER `ci.yml` has run
once (a required check that has never reported blocks every PR). Delivered as
(a) a ready-to-run `gh api` command for an admin-scoped token and (b) a
GitHub-UI checklist — because the current fine-grained PAT is denied the
Administration permission (`403` on read; write will also fail). Settings:
- `required_status_checks.strict = true`, contexts = `CI / lint`, `CI / tests`,
  `CI / coverage`, `CI / pip-audit`, `Integration / real-bench integration`.
- `required_pull_request_reviews`: 1 approval, `dismiss_stale_reviews = true`.
- `required_conversation_resolution = true`.
- `required_linear_history = true`.
- `enforce_admins = true`.
- `allow_force_pushes = false`, `allow_deletions = false`.

## Task breakdown
| ID | Task | Weight | Assignee | Depends on | Acceptance criteria |
|----|------|--------|----------|------------|---------------------|
| T1 | Author `.github/workflows/ci.yml` (lint, tests, coverage, pip-audit; PR-all + push-main triggers; concurrency; permissions) | Heavy | Lead (Fable) | — | Valid YAML (actionlint clean if available); jobs render as `CI / <name>`; `set -eo pipefail` present on both pytest pipelines; coverage has no fail-under; matrix removed from tests |
| T2 | Delete `.github/workflows/tests.yml` | Light | Lead (Fable) | T1 | File removed; no workflow named `Tests` remains |
| T3 | Resolve paths-ignore vs required-checks (Edge case 1): make required checks always report on every PR | Heavy | Lead (Fable) | T1 | A doc-only PR still reports all required contexts (green), so protection can't deadlock it |
| T4 | Produce branch-protection deliverable: `gh api` command + UI checklist (strict/max) for the user to apply post-merge | Light | Lead (Fable) | T1 | Command lists the exact 5 contexts + all strict/max settings; sequencing note included |
| T5 | Speed up installs with `uv` (AMENDMENT — see below) across all four `ci.yml` jobs, with a warm dependency cache | Heavy | Lead (Fable) | T1 | Every job installs via `uv pip install --system` after `astral-sh/setup-uv` with `enable-cache: true`; deps resolve on 3.14; commands validated locally in a throwaway venv; lint/tests/coverage/audit still pass |

## Amendment 2026-08-16 — install-speed, sharding rejected
User asked to "split the testcases into multiple chunks so it completes faster."
Measured reality: the `pytest` job wall-clock was ~43s, of which the test run
is only ~6-10s (1858 tests in 6s locally); the remaining ~33s is dependency
install (`line_profiler`'s C-extension, etc.). Every shard pays that ~33s
install in full, so 4 shards ≈ ~38s wall-clock vs 43s for 4× the runner cost —
a ~5s gain. **Sharding rejected** as ineffective for this suite size.
**pytest-xdist deferred**: at 6s the worker-spawn overhead is a wash, and the
conftest-stub suite's parallel-safety is unproven; revisit if the suite grows.
**Chosen instead:** convert all `ci.yml` installs from `python -m pip` to `uv`
(`astral-sh/setup-uv@v6`, `enable-cache: true`, `uv pip install --system`),
which attacks the ~33s install bottleneck directly and caches the built
`line_profiler` wheel across runs. This is the real, across-the-board speedup.

All tasks are Lead-implemented: the change set is small, interconnected, and
every line is load-bearing CI config where a wrong trigger or a dropped
`pipefail` silently breaks the merge gate — not delegation-friendly.

## Edge cases and failure modes (reviewer will verify each one)
1. **paths-ignore vs required checks (design-level).** With strict/max
   protection requiring `CI / *` and `Integration / …`, a PR that only touches
   `paths-ignore` files (`**/*.md`, `docs/**`, `LICENSE`, `.gitignore`) would
   NOT trigger the workflow, so the required checks never report and the PR is
   blocked forever ("Expected — waiting for status"). Required behavior: every
   required check must report on every PR. Chosen resolution (pending user
   confirmation — see Open questions): **remove `paths-ignore` from `ci.yml`
   and from `integration.yml`** so all required checks always run. The fast
   jobs make this cheap; integration already runs only on main-targeted PRs.
2. **Double-run regression.** A feature-branch push with an open PR must fire
   ONLY `pull_request`; a merge to `main` must fire ONLY `push`. Verified by
   the trigger split (`push: [main]`, `pull_request: ['**']`).
3. **pipefail swallow.** Without `set -eo pipefail`, `pytest … | tee` returns
   tee's exit 0 and a failing suite reports green. Both the `tests` and
   `coverage` jobs must keep `set -eo pipefail`.
4. **Coverage job as a real gate.** It is a required check, so it must be able
   to go red — therefore NOT `continue-on-error`. "Informational" means only
   "no coverage threshold", not "never fails": it still fails if pytest fails.
5. **Concurrency on main.** `cancel-in-progress` may cancel an in-flight `main`
   build when two merges land quickly. Acceptable and standard; the later
   commit's run is the authoritative one.
6. **Missing `pytest-cov`.** The coverage job must install `pytest-cov`
   explicitly; the base test job does not need it.
7. **Sequencing of protection.** Protection must be applied only after `ci.yml`
   has produced each required check at least once, else all PRs deadlock. The
   deliverable states this explicitly.
8. **Token permission.** The current PAT cannot set protection (403). The
   deliverable must offer both an admin-token `gh api` path and a UI path;
   no silent assumption that automation will apply it.
9. **uv targets the right interpreter (T5).** `uv pip install --system` must
   install into the `actions/setup-python` 3.14 interpreter, not a uv-managed
   or system-default Python. Keep `setup-python` before `setup-uv`; verify the
   deps land where `python -m pytest` will find them.
10. **uv cache correctness (T5).** `enable-cache: true` keyed on `pyproject.toml`
    must not serve a stale wheel when deps change; the cache holds the built
    `line_profiler` wheel, so a rebuild only happens on cache miss. A cold cache
    must still succeed (falls back to a full build).
- Categories N/A: no runtime input validation / auth boundaries / DB partial
  failure here — this is CI config, not application request-handling code.

## Test plan
- **Static validation:** `actionlint .github/workflows/ci.yml` if the tool is
  installed; otherwise a Python `yaml.safe_load` parse of both workflows to
  confirm well-formedness.
- **Local command validation:** run `ruff check optimus/`, `pytest
  optimus/tests/`, and `pytest optimus/tests/ --cov=optimus
  --cov-report=term-missing` locally to confirm the exact commands the jobs run
  actually pass on this tree before committing.
- **Flow review scenarios (post-merge, GitHub-side — documented for the user
  since Actions can't be triggered from here):**
  1. Open a normal PR → exactly one run of each `CI / *` check appears, no
     `(push)` twin; all green.
  2. Push a second commit to that PR branch → the prior in-flight run is
     cancelled, not duplicated.
  3. Doc-only PR (touch only a `.md`) → required checks still report green
     (validates Edge case 1's resolution).
  4. After protection: attempt a direct push to `main` → rejected; a PR with a
     failing check → merge blocked.

## Open questions
1. **paths-ignore removal (Edge case 1).** RESOLVED 2026-08-16 — user chose
   option (a): remove `paths-ignore` from BOTH `ci.yml` and `integration.yml`
   so every required check always reports on every PR. This deliberately edits
   `integration.yml` (the one exception to "leave it untouched"); no other
   change to that workflow.

## Definition of done
- All tasks meet acceptance criteria
- Code review VERDICT: GREEN
- Flow review VERDICT: GREEN
- Committed only after both greens; PR raised only after flow review passed on
  the final state being pushed
- Branch-protection deliverable handed to the user with the post-merge
  sequencing note
