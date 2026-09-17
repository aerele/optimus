# Code review — ci-reorg-jarvis-style — round 1
Reviewer: Opus (strict-reviewer)
Date: 2026-08-16
Scope: `.github/workflows/ci.yml` (added), `.github/workflows/integration.yml` (modified), `.github/workflows/tests.yml` (deleted). Plan: `.claude/workflow/plans/2026-08-16-ci-reorg-jarvis-style.md` (STATUS: APPROVED).

## Findings
| # | Severity | Location | What breaks | Required fix |
|---|----------|----------|-------------|--------------|
| 1 | MINOR | integration.yml:10 | Comment "This workflow does NOT replace ``tests.yml``; the two run in parallel" is now false — `tests.yml` is deleted in this same change set. Misleads future maintainers into thinking a separate pytest workflow still exists. | Update to reference `ci.yml` (the two run in parallel = `CI` + `Integration`). |
| 2 | MINOR | integration.yml:4 | Comment "Slower than the pure-pytest workflow (``tests.yml``)" references the deleted file. | Point at `ci.yml`. |
| 3 | MINOR | integration.yml:73-81 | Comment claims "the pure-pytest ``tests.yml`` workflow runs against 3.12 + 3.13 + 3.14 unchanged." Doubly wrong: (a) `tests.yml` is deleted; (b) even the deleted file ran a single-version matrix `['3.14']`, never 3.12/3.13. Reader may believe multi-version coverage exists when it does not. | Rewrite to reference `ci.yml` and state the real single-version (3.14) fact. |
| 4 | MINOR (out-of-diff, pre-existing) | integration.yml:186-196 | "Publish integration summary" greps `integration-install.log` and `integration-lifecycle.log`, but the module loop (line 166) writes `integration-<module>.log` (e.g. `integration-test_install_smoke.log`). Those two filenames never exist, so the integration summary tab is always empty. Not introduced by this diff (the diff only removed `paths-ignore`), but it is in a file this change touches. | Not blocking this change; fix the log-name mismatch in a follow-up (loop over the actual `INTEGRATION_MODULES` names, or glob `integration-*.log`). |
| 5 | MINOR (out-of-diff) | .github/helper/install.sh:128 | Comment "Mirror the install line ``tests.yml`` uses" references the deleted file. | Repoint to `ci.yml` when that file is next touched. |
| 6 | MINOR (informational) | ci.yml:170-172 | `--cov=optimus` measures the whole package, but only `optimus/tests/` runs here; the `optimus/tests_integration/*` modules therefore report 0% and drag the TOTAL down (85% observed; real unit-covered figure is higher). Cosmetic only — no `--cov-fail-under`, so it never gates. | Optional: add `--cov` omit/`.coveragerc` excluding `tests_integration/` before any future threshold lands. |

Zero BLOCKER, zero MAJOR. All findings MINOR (MINORs never block alone).

## Plan conformance
- T1 (author `ci.yml`): DONE. `name: CI`; triggers `pull_request:['**']` + `push:[main]` + `workflow_dispatch`; concurrency `${{ github.workflow }}-${{ github.ref }}` cancel-in-progress; `permissions: contents: read`; jobs render as `CI / lint`, `CI / tests`, `CI / coverage`, `CI / pip-audit` (YAML parsed, job keys confirmed). Matrix dropped. `set -eo pipefail` on both pytest pipelines. Coverage has no `--cov-fail-under`. Nothing smuggled beyond the plan.
- T2 (delete `tests.yml`): DONE. `git status` shows `D`; no remaining workflow named `Tests` (only `CI` and `Integration` in `.github/workflows/`).
- T3 (paths-ignore vs required checks): DONE. `ci.yml` has no `paths-ignore`; `integration.yml` `paths-ignore` blocks removed (diff confirmed).
- T4 (branch-protection deliverable): NOT IN CHANGE SET. The `gh api` command + UI checklist is a chat handoff to the user; only the plan (lines 68-79, 117-122) specifies it. Its design is correct and complete (5 exact contexts, strict/max settings, post-run sequencing note). The artifact itself is not reviewable here — see edge cases 7-8.

## Edge-case verification
| Plan edge case | Handling site | Test / evidence | Verified |
|---|---|---|---|
| 1. paths-ignore vs required checks | ci.yml has no `paths-ignore` (lines 21-26); integration.yml `paths-ignore` removed (diff) | Config inspection; both workflows YAML-valid; every required check will trigger on every PR into main | YES (config); runtime reporting is a flow scenario (NOT RUN — see flow file) |
| 2. Double-run regression | Trigger split: `push:[main]` + `pull_request:['**']` (ci.yml:21-26) | Config inspection; PR-merge closes the PR so only `push` fires on main, only `pull_request` fires on feature branches | YES (config); runtime dedup is a flow scenario (NOT RUN) |
| 3. pipefail swallow | `set -eo pipefail` at ci.yml:108 (tests) and ci.yml:169 (coverage) | Executed: `bash -e -c 'false \| tee'` → exit 0 (masks); `bash -c 'set -eo pipefail; false \| tee'` → exit 1 (fails). Defense proven effective. | YES |
| 4. Coverage a real gate | coverage job has NO `continue-on-error` and NO `--cov-fail-under` (ci.yml:136-194) | Executed coverage command → exits non-zero only if suite fails; config inspection confirms it can go red | YES |
| 5. Concurrency cancels on main | `concurrency` group `${{ github.workflow }}-${{ github.ref }}`, cancel-in-progress (ci.yml:30-32) | Config inspection; plan accepts later-commit-wins | YES (config); runtime cancel is a flow scenario (NOT RUN) |
| 6. Missing pytest-cov | coverage job installs `pytest-cov` (ci.yml:160); tests job does not (ci.yml:99) | Executed both commands in a clean venv; coverage ran with pytest-cov, tests ran without it | YES |
| 7. Sequencing of protection | T4 deliverable (plan lines 68, 117-119) — NOT in change set | Plan documents "apply AFTER `ci.yml` has run once"; deliverable artifact not present to verify | UNVERIFIED — deliverable outside change set; developer must confirm handoff carries the sequencing note |
| 8. Token permission | T4 deliverable (plan lines 120-122) — NOT in change set | Plan documents dual path (admin `gh api` + UI checklist); deliverable artifact not present to verify | UNVERIFIED — deliverable outside change set; developer must confirm both paths were handed over |

## Local gate-command execution (CI-equivalent clean venv, Python 3.14.2)
| Gate | Command | Result |
|---|---|---|
| CI / lint | `ruff check optimus/` | All checks passed (exit 0); ruff 0.14.10 satisfies `>=0.6,<1` |
| CI / tests | `set -eo pipefail; pytest optimus/tests/ -v --tb=long -rA \| tee pytest.log` | 1821 passed, 10 skipped, no errors |
| CI / coverage | `set -eo pipefail; pytest optimus/tests/ --cov=optimus --cov-report=term-missing --cov-report=xml \| tee coverage.log` | 1821 passed, TOTAL 85%, `coverage.xml` written (956KB); awk summary extraction verified |
| CI / pip-audit | `pip-audit --skip-editable` | No known vulnerabilities; editable `optimus` correctly skipped; exit 0 |

Deps installed matched the workflow install lists exactly (byte-identical to the deleted `tests.yml` install line for the tests job; coverage adds `pytest-cov`). `rq`-dependent tests skip gracefully by design; no hard imports of un-installed packages cause collection errors under the CI dependency set.

## Attack pass (playbook applied to CI config)
- Trigger matrix: no-PR feature branch pushes produce no CI run (by design — PR is the gate); feature-branch-with-PR → only `pull_request`; merge to main → only `push`. No accidental double-run in config.
- Cross-workflow concurrency collision: `group` includes `${{ github.workflow }}` so `CI` and `Integration` groups never collide. OK.
- Permissions: least privilege (`contents: read`); no secrets referenced; no privilege escalation surface. OK.
- Artifact/summary steps: all guarded with `if: always()`/`if: failure()` and `|| true`; missing-file paths handled by `if-no-files-found: warn`. No step failure leaks. OK.
- Cost regression: removing `paths-ignore` from `integration.yml` means doc-only PRs into main now spin the full 25-min bench + MariaDB/Redis. Deliberate, plan-approved trade-off (edge case 1) to keep the required check honest — accepted, not a finding.

VERDICT: GREEN
