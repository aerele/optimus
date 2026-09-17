# Flow review — ci-reorg-jarvis-style — round 2
Reviewer: Opus (strict-reviewer)
Date: 2026-08-16
Scope: The CI "system" is GitHub Actions orchestration for `ci.yml` + `integration.yml`. Two layers: (A) job-internal execution — the commands each runner runs; (B) GitHub orchestration — trigger dedup, concurrency cancellation, required-check reporting, branch-protection enforcement.

## What could and could not be executed
- Layer A (job commands) — EXECUTED locally via the ACTUAL uv install path (round 1 only ran the pip equivalents). Throwaway uv venv, Python 3.14.3, deps matching the workflow install lists byte-for-byte. Method C (direct driving) from flow-review.md. All green.
- Layer B (GitHub orchestration) — NOT RUN, and confirmed unexecutable this round: `gh api .../actions/workflows/ci.yml` → HTTP 404 (workflow never pushed to the default branch); `gh run list --branch ci/jarvis-style-reorg` → zero runs; `gh api .../branches/main/protection` → 403 (PAT lacks Administration). The change is uncommitted and the branch is unpushed; the reviewer must not push/commit to force a run. These scenarios — the entire reason this change exists — remain unexecuted. Per flow-review.md Step 3, unexecuted scenarios covering planned edge cases force RED.

## Break attempts
| Scenario | Expected | Actual | Result |
|---|---|---|---|
| `CI / lint` via uv (`uv pip install 'ruff>=0.6,<1'` → `ruff check optimus/`) | clean, exit 0 | ruff 0.16.3, All checks passed, exit 0 | PASS |
| `CI / tests` via uv, real `set -eo pipefail \| tee` pipeline | suite passes, non-zero surfaces | 1821 passed, 10 skipped, exit 0 | PASS |
| pipefail masking (r1, re-cited): `false \| tee` under bash -e no pipefail | masks as green | exit 0 (confirms why `set -eo pipefail` is load-bearing) | PASS (defense proven) |
| pipefail defense: `set -eo pipefail; false \| tee` | failure surfaces | exit 1 | PASS |
| `CI / coverage` via uv | suite passes, coverage.xml produced, red only on suite failure | 1821 passed, TOTAL 85%, coverage.xml (956KB); awk table extraction works; exit 0 | PASS |
| Coverage-as-real-gate: `continue-on-error`/`--cov-fail-under` present? | absent | both absent; red only if pytest fails | PASS |
| uv build isolation: line_profiler C-ext builds without the deleted "build prerequisites" step | installs cleanly | line_profiler==5.0.2 installed via uv, exit 0 | PASS |
| Edge case 9: uv installs into the interpreter pytest runs under | deps importable by the venv python | `import pytest,hypothesis,faker,pdfplumber,sql_metadata,jsonschema,line_profiler` OK under the same python | PASS |
| Edge case 10 (cold cache): from-scratch uv install succeeds | full build works with empty cache | clean venv install + all gates green | PASS |
| Missing `pytest-cov` in tests job | tests green without it; coverage installs it | confirmed both | PASS |
| `CI / pip-audit` via uv (`pip-audit --skip-editable`) | vulns exit non-zero; editable optimus skipped | No known vulnerabilities; optimus skipped as editable; exit 0 | PASS |
| YAML validity + check-name mapping | jobs render as `CI / <name>`, contexts match plan's 5 | both parse; ci jobs lint/tests/coverage/pip-audit → `CI / *`; integration → `Integration / real-bench integration` | PASS |
| **Double-run dedup (edge case 2):** feature-branch push w/ open PR fires ONLY pull_request; merge to main fires ONLY push | one run per commit | GitHub Actions not triggerable from review env; workflow 404 on remote, zero runs | **NOT RUN** |
| **Concurrency cancel (edge case 5):** 2nd commit to a PR branch cancels the in-flight run | prior run cancelled, not duplicated | not triggerable from review env | **NOT RUN** |
| **Doc-only PR reporting (edge case 1):** PR touching only `.md` still reports all 5 required contexts green | no "waiting for status" deadlock | not triggerable from review env | **NOT RUN** |
| **Protection enforcement (edge cases 4/7/8):** direct push to main rejected; PR w/ failing check blocked; protection applied only after ci.yml reported once | push rejected; merge blocked; no all-PR deadlock | protection returns 403 (not applied; PAT can't set it); not enforceable from review env | **NOT RUN** |

## Assessment
Layer A is fully green — and this round it is green on the REAL uv commands the runner executes, not the pip stand-ins round 1 used. The pipefail defense that keeps the merge gate honest is proven, and edge cases 9/10 (uv interpreter targeting + cold-cache build) are now executed. That is necessary but not sufficient.

Layer B — trigger dedup, concurrency cancellation, required-check-always-reports, and branch-protection enforcement — is the whole point of this change and is entirely unexecuted. flow-review.md is explicit: a flow review is invalid unless the system was actually exercised, and unexecuted scenarios covering planned edge cases force RED. The plan defers these to "documented for the user since Actions can't be triggered from here"; the reviewer contract does not accept documentation in place of execution for flow scenarios. Nothing about executability changed since round 1 (branch unpushed, no PR, protection 403), so the flow verdict is unchanged.

## Required to clear the flow gate (developer/user must execute on GitHub, then report back for round 3)
1. Push the branch and open a normal PR into `main` → confirm exactly one run of each `CI / *` check, no `(push)` twin; all green. (edge case 2)
2. Push a second commit to that PR branch → confirm the prior in-flight run is cancelled, not duplicated. (edge case 5)
3. Open a doc-only PR (touch only a `.md`) → confirm all 5 required contexts still report green, no "waiting for status" deadlock. (edge case 1)
4. Apply branch protection with the T4 `gh api` command (admin token — current PAT is 403) ONLY after `ci.yml` has reported each check once; then attempt a direct push to `main` (expect reject) and a PR with a deliberately failing check (expect merge blocked). Confirm the 5 required contexts + strict/max settings match the plan. (edge cases 4, 7, 8)

VERDICT: RED
