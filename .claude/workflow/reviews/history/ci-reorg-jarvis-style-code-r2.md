# Code review — ci-reorg-jarvis-style — round 2
Reviewer: Opus (strict-reviewer)
Date: 2026-08-16
Scope: `.github/workflows/ci.yml` (added), `.github/workflows/integration.yml` (modified), `.github/workflows/tests.yml` (deleted). Plan: `.claude/workflow/plans/2026-08-16-ci-reorg-jarvis-style.md` (STATUS: APPROVED). Reviewed against the WORKING TREE (final `uv`-based state; the staged copy of `ci.yml` is an earlier pip draft, +40/-22 behind the working tree).

## Round-1 finding disposition
| # (r1) | Severity | Status | Evidence |
|---|---|---|---|
| 1 | MINOR | FIXED | integration.yml:10 now reads "does NOT replace ``ci.yml``". |
| 2 | MINOR | FIXED | integration.yml:3-4 now "pure-pytest ``CI`` workflow (``ci.yml``)". |
| 3 | MINOR | FIXED | integration.yml:76-80 rewritten; no longer claims 3.12+3.13+3.14, correctly states single-version 3.14. |
| 4 | MINOR (out-of-diff) | PERSISTS | integration.yml:187 summary still greps `integration-install.log`/`integration-lifecycle.log`; module loop (line 165) writes `integration-<module>.log` and install.sh writes no `.log` — those two names never exist, so the Integration summary tab is always empty. Summary step untouched by this diff. |
| 5 | MINOR (out-of-diff) | PERSISTS | .github/helper/install.sh:128 still references the deleted ``tests.yml``. File untouched by this diff. |
| 6 | MINOR (informational) | PERSISTS | `--cov=optimus` measures the whole package; `tests_integration/*` reports 0% and drags TOTAL to 85%. No `--cov-fail-under`, so never gates. |

## Findings (round 2)
| # | Severity | Location | What breaks | Required fix |
|---|----------|----------|-------------|--------------|
| 1 | MINOR (out-of-diff) | integration.yml:187 | Integration summary greps two filenames (`integration-install.log`, `integration-lifecycle.log`) that are never created → summary tab always empty. Does NOT hide failures (job exit code + failure-artifact logs still surface them); cosmetic only. | Loop over `INTEGRATION_MODULES` names or glob `integration-*.log`. Out of scope for this change. |
| 2 | MINOR (out-of-diff) | .github/helper/install.sh:128 | Comment references deleted `tests.yml`. | Repoint to `ci.yml` when that file is next touched. |
| 3 | MINOR (informational) | ci.yml:170-172 | Coverage TOTAL depressed (85%) by whole-package measurement including `tests_integration/`. | Optional `.coveragerc` omit before any future threshold lands. |

Zero BLOCKER, zero MAJOR. All findings MINOR (MINORs never block alone).

## Plan conformance
- T1 (`ci.yml`): DONE. `name: CI`; triggers `pull_request:['**']` + `push:[main]` + `workflow_dispatch`; concurrency `${{ github.workflow }}-${{ github.ref }}` cancel-in-progress; `permissions: contents: read`; jobs render as `CI / lint`, `CI / tests`, `CI / coverage`, `CI / pip-audit` (job `name:` = lint/tests/coverage/pip-audit → GitHub context `CI / <name>`, exact match to plan's required contexts). Matrix dropped. `set -eo pipefail` on both pytest pipelines (ci.yml:107, 168). Coverage has no `--cov-fail-under`, no `continue-on-error`. Nothing smuggled beyond the plan.
- T2 (delete `tests.yml`): DONE. `git status` shows `D`; only `CI` + `Integration` workflows remain.
- T3 (paths-ignore vs required checks): DONE. `ci.yml` has no `paths-ignore`; `integration.yml` paths-ignore blocks removed (diff confirmed lines 18-21).
- T4 (branch-protection deliverable): NOT IN CHANGE SET (chat handoff). Confirmed still un-appliable from here: `gh api repos/aerele/optimus/branches/main/protection` → 403 (PAT lacks Administration), exactly as plan edge case 8 predicted. Design is correct (5 exact contexts + strict/max) but the artifact is not reviewable in this tree — see edge cases 7-8.
- T5 (uv conversion — AMENDMENT): DONE and now EXECUTED (round 1 never ran the uv path). Every job: `astral-sh/setup-uv@v6` with `enable-cache: true`, `cache-dependency-glob: 'pyproject.toml'`, placed AFTER `actions/setup-python@v5`; all installs via `uv pip install --system`. Validated in a throwaway uv venv on Python 3.14.3 (see execution table).

## Edge-case verification (all 10)
| Plan edge case | Handling site | Test / evidence | Verified |
|---|---|---|---|
| 1. paths-ignore vs required checks | ci.yml: no paths-ignore; integration.yml paths-ignore removed | Config: docs-only PR into main triggers both workflows → all 5 required contexts report | YES (config); runtime reporting is a flow scenario (NOT RUN — flow file) |
| 2. Double-run regression | Trigger split `push:[main]` + `pull_request:['**']`; pull_request default types exclude `closed` | Config trace: feature-branch push w/ open PR → only pull_request; PR merge → main push fires once, closed PR does not re-fire | YES (config); runtime dedup is a flow scenario (NOT RUN) |
| 3. pipefail swallow | `set -eo pipefail` ci.yml:107 (tests), ci.yml:168 (coverage) | r1 executed: `bash -e -c 'false\|tee'`→0 (masks); `set -eo pipefail; false\|tee`→1. Defense proven | YES |
| 4. Coverage a real gate | coverage job: no `continue-on-error`, no `--cov-fail-under` (ci.yml:151-194) | Executed coverage cmd → exit 0 on pass; goes red only if suite fails | YES |
| 5. Concurrency cancels | group `${{ github.workflow }}-${{ github.ref }}` cancel-in-progress (ci.yml:29-31) | Config; plan accepts later-commit-wins | YES (config); runtime cancel is a flow scenario (NOT RUN) |
| 6. Missing pytest-cov | coverage installs `pytest-cov` (ci.yml:160); tests does not (ci.yml:99) | Executed: tests ran green without it; coverage ran with it | YES |
| 7. Sequencing of protection | T4 deliverable (not in change set) | Plan documents "apply AFTER ci.yml reported once"; protection 403 confirms not yet applied | UNVERIFIED — deliverable outside change set; developer must confirm handoff carries the sequencing note |
| 8. Token permission | T4 deliverable (not in change set) | `gh api …/protection` → 403 confirms PAT cannot set protection; plan offers dual admin-token/UI path | UNVERIFIED — deliverable outside change set; developer must confirm both paths handed over |
| 9. uv targets right interpreter | setup-python BEFORE setup-uv in all 4 jobs; `uv pip install --system` | EXECUTED: after uv install, `python -c "import pytest,hypothesis,faker,pdfplumber,sql_metadata,jsonschema,line_profiler"` succeeds under the same venv python that runs pytest | YES |
| 10. uv cache correctness | `enable-cache: true` + `cache-dependency-glob: 'pyproject.toml'` (all 4 jobs) | Cold build EXECUTED from scratch → succeeds (edge case's "cold cache must still succeed"). Stale-wheel-on-dep-change relies on uv's content-addressed wheel cache (documented) + pyproject-hashed actions cache key | YES (cold-build executed; cache-hit-then-dep-change is a runner behavior, config correct) |

Note: round 1's edge-case table stopped at #8 and never verified the uv amendment's #9/#10 — closed this round.

## Local gate-command execution (ACTUAL uv path, throwaway venv, Python 3.14.3)
| Gate | Command (as the workflow runs it) | Result |
|---|---|---|
| install | `uv pip install --system -e .` | line_profiler==5.0.2 + 11 deps installed, exit 0 (build isolation, no pip/wheel/setuptools bootstrap needed) |
| CI / lint | `uv pip install 'ruff>=0.6,<1'` → `ruff check optimus/` | ruff 0.16.3 satisfies range; All checks passed (exit 0) |
| CI / tests | `set -eo pipefail; python -m pytest optimus/tests/ -rA \| tee` | 1821 passed, 10 skipped, exit 0 |
| CI / coverage | `set -eo pipefail; python -m pytest optimus/tests/ --cov=optimus --cov-report=term-missing --cov-report=xml \| tee` | 1821 passed, TOTAL 85%, coverage.xml (956KB) written; awk summary extraction verified; exit 0 |
| CI / pip-audit | `uv pip install --system pip-audit` → `pip-audit --skip-editable` | No known vulnerabilities; editable optimus skipped; exit 0 |

This is the key delta from round 1, which validated the pip commands of the deleted tests.yml — not the uv commands the workflow actually runs. The uv path is now proven green. rq-dependent tests skip gracefully; no uncovered hard import causes a collection error under the CI dependency set.

## Attack pass (playbook applied to CI config)
- Trigger matrix: no-PR feature-branch push → zero runs (by design); feature-branch-with-PR → only pull_request; merge to main → only push (pull_request default types exclude `closed`). No double-run. Contexts `CI / {lint,tests,coverage,pip-audit}` + `Integration / real-bench integration` exactly match the plan's 5 required checks.
- Cross-workflow concurrency: `group` includes `${{ github.workflow }}` → `CI` and `Integration` groups never collide.
- Permissions: least privilege (`contents: read`); no secrets; no privilege-escalation surface; `workflow_dispatch` takes no inputs.
- Artifact/summary steps: guarded `if: always()`/`if: failure()`, `|| true` on grep/awk, `if-no-files-found: warn`. No step failure leaks.
- Build supply chain: uv build isolation replaces the deleted "build prerequisites" step; line_profiler C-ext resolves to a 3.14 wheel (no source build needed on this platform); ubuntu-latest has gcc if a source build is ever required.
- Cost regression: doc-only PRs into main now spin the full ~25-min bench (paths-ignore removed from integration.yml). Deliberate, plan-approved trade-off (edge case 1) to keep the required check honest. Accepted, not a finding.

VERDICT: GREEN
