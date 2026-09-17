# Flow review — ci-reorg-jarvis-style — round 1
Reviewer: Opus (strict-reviewer)
Date: 2026-08-16
Scope: The CI "system" under review is GitHub Actions orchestration for `ci.yml` + `integration.yml`. Two layers: (A) job-internal execution — the commands each runner runs; (B) GitHub orchestration — trigger dedup, concurrency cancellation, required-check reporting, branch-protection enforcement.

## What could and could not be executed
- Layer A (job commands) — EXECUTED locally in a clean CI-equivalent venv (Python 3.14.2, deps matching the workflow install lists). Method C (direct driving) from flow-review.md. All green.
- Layer B (GitHub orchestration) — NOT RUN. GitHub Actions cannot be triggered from this review environment, and the reviewer must not push/commit the uncommitted change to force a run. These scenarios — the entire reason this change exists — remain unexecuted. Per flow-review.md Step 3, unexecuted scenarios covering planned edge cases force RED.

## Break attempts
| Scenario | Expected | Actual | Result |
|---|---|---|---|
| Run `CI / lint` command (`ruff check optimus/`) | clean lint, exit 0 | All checks passed, exit 0 | PASS |
| Run `CI / tests` command with the real `set -eo pipefail \| tee` pipeline | suite passes, non-zero surfaces | 1821 passed, 10 skipped, exit 0 | PASS |
| pipefail masking attack: `false \| tee` under GitHub default shell (`bash -e`, no pipefail) | should mask failure as green | exit 0 (masks) — confirms why `set -eo pipefail` is load-bearing | PASS (defense proven) |
| pipefail defense: `set -eo pipefail; false \| tee` | failure surfaces | exit 1 | PASS |
| Run `CI / coverage` command | suite passes, `coverage.xml` produced, red only on suite failure | 1821 passed, TOTAL 85%, `coverage.xml` (956KB) written; awk summary extraction produced the per-file table + TOTAL | PASS |
| Coverage-as-real-gate: is `continue-on-error`/`--cov-fail-under` present? | absent (must be able to go red on suite fail, never on a number) | both absent; job goes red only if pytest fails | PASS |
| Missing `pytest-cov` in tests job | tests job must not need it; coverage job installs it | tests ran green without pytest-cov; coverage ran with it | PASS |
| Run `CI / pip-audit` command (`pip-audit --skip-editable`) | vulns exit non-zero; editable optimus skipped | No known vulnerabilities; optimus skipped ("distribution marked as editable"); exit 0 | PASS |
| Hard-import collection attack: any test top-imports a package NOT in the CI install set → collection error | all such imports covered or skip gracefully | jsonschema/hypothesis are installed by CI; `rq` tests skip via importorskip; no uncovered hard import | PASS |
| YAML validity of both workflows | parse clean, jobs render as `CI / <name>` | parsed; jobs `[lint, tests, coverage, pip-audit]`, `[integration]`; contexts map to plan's required 5 | PASS |
| **Double-run dedup (edge case 2):** feature-branch push with open PR fires ONLY `pull_request`; merge to main fires ONLY `push` | exactly one run per commit | Cannot trigger GitHub Actions from review env | **NOT RUN** |
| **Concurrency cancel (edge case 5):** push a 2nd commit to a PR branch cancels the in-flight run | prior run cancelled, not duplicated | Cannot trigger GitHub Actions from review env | **NOT RUN** |
| **Doc-only PR reporting (edge case 1):** PR touching only `.md` still reports all 5 required contexts green | no "Expected — waiting for status" deadlock | Cannot trigger GitHub Actions from review env | **NOT RUN** |
| **Protection enforcement (edge cases 4/7/8):** direct push to `main` rejected; PR with a failing check blocked; protection applied only after `ci.yml` reported once | push rejected; merge blocked; no all-PR deadlock | Protection not applied (T4 deliverable outside change set); cannot enforce from review env | **NOT RUN** |

## Assessment
Layer A is fully green: every command a runner actually executes passes on this tree, and the pipefail defense that keeps the merge gate honest is proven effective, not just present. That is necessary but not sufficient.

Layer B — trigger dedup, concurrency cancellation, required-check-always-reports, and branch-protection enforcement — is the whole point of this change and is entirely unexecuted. flow-review.md is explicit: a flow review is invalid unless the system was actually exercised, and unexecuted scenarios covering planned edge cases force RED. The plan itself defers these to "documented for the user since Actions can't be triggered from here"; the reviewer contract does not accept documentation in place of execution for flow scenarios.

## Required to clear the flow gate (developer/user must execute on GitHub, then report back for round 2)
1. Open a normal PR into `main` → confirm exactly one run of each `CI / *` check, no `(push)` twin; all green. (edge case 2)
2. Push a second commit to that PR branch → confirm the prior in-flight run is cancelled, not duplicated. (edge case 5)
3. Open a doc-only PR (touch only a `.md`) → confirm all 5 required contexts still report green, no "waiting for status" deadlock. (edge case 1)
4. Apply branch protection with the T4 `gh api` command ONLY after `ci.yml` has reported each check once; then attempt a direct push to `main` (expect reject) and a PR with a deliberately failing check (expect merge blocked). Confirm the 5 required contexts and strict/max settings match the plan. (edge cases 4, 7, 8)

VERDICT: RED
