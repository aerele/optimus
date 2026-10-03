# AI remediation: deployment and acceptance

**Release acceptance: INCOMPLETE.** This document records implemented behavior,
reproducible checks and outstanding evidence. It is not permission to release.
A green CI or AI Quality workflow does not evaluate a live model or establish
safe deployment on an existing site.

## Implemented feature set

The stack preserves the key-safe logging boundary, historical Error Log scrub,
site-wide masking hook, version-aware File hook, sanitized AI HTML, session
permission gates and per-user rate limits. Deterministic index recipes replace
index AI. Finding AI is limited to eligible Hot Line, N+1 Query, Redundant Call
and Slow Query findings with the required source and callsite checks.

The follow-ups add classified failures, bounded parameter repair, shared
selection and usage handling; SQL-journaled background refresh; asynchronous
API/Desk wiring; and recording, provider, privacy, source and capture security.
The core profile and report are saved as Ready before optional AI is queued.
AI failure, cancellation or a missing worker cannot roll back that saved result.
Phase 2 analysis also uses queued generations with explicit retry limits.

Use [AI data flow](AI-FIXING.md) for what is sent and guardrail behavior,
[background operations](AI-REFRESH.md) for states, budgets and recovery,
[configuration](../README.md#configuration-optimus-settings-doctype) for defaults,
and [security boundaries](../SECURITY.md) for residual risks. These guides
replace the old synchronous-refresh and Redis-only progress assumptions.

## Dependency and merge checkpoint

The owner merges. The integration branch is `dev/ai-suggestion-fixes`; its
aggregate PR targets `develop`. Do not merge the aggregate before acceptance.
The already merged security, file hook, sanitizer, eval-tooling, session gate and
guardrail work must remain present. The current follow-up order is:

1. Deterministic recipes and eligibility, #71.
2. Reliability helpers, #72.
3. SQL background refresh engine, #73.
4. API/Desk refresh wiring, #74.
5. Security hardening, #75.
6. Documentation and acceptance, after its outstanding gates close.

Each draft includes its predecessors for testing. Merge one, rebase the next on
the updated integration branch, resolve its version/changelog once, and repeat
all required checks on the resulting head. Do not cherry-pick an engine caller
without its schema, guards and recovery hooks. Before the aggregate merge,
reconcile the independent changes already on develop, including report/print
and ownership metadata. Preserve those changes, then test the composed tree.
Version numbers in intermediate drafts are provisional until their final rebase.

## Automated evidence recorded on 2026-10-03

These are observations for the stated heads, not results for an eventual merge.
Full local suites used forward and reverse file order. Stub runs blocked Frappe
and RQ; pinned AI runs used the declared Semgrep rules and real RQ dependency.

| Scope | Head | Evidence |
| --- | --- | --- |
| Reliability | `3772683` | Forward/reverse: 4217 passed, 15 skipped. Stub: 4143 passed, 53 skipped. Pinned AI: 724 passed, 5 skipped. 24 targeted mutations detected. |
| Engine | `5e8cfd1` | Forward/reverse: 4428 passed, 22 skipped. Stub: 4342 passed, 72 skipped. Pinned AI: 805 passed, 5 skipped. 30 mutations detected; seven real SQL cases per backend. |
| Wiring | `8782839` | Forward/reverse: 4582 passed, 28 skipped. Stub: 4477 passed, 97 skipped. Pinned AI: 916 passed, 5 skipped. 40 mutations detected; 13 real SQL cases per backend. |
| Security | `b5dfb94` | Forward/reverse: 5023 passed, 32 skipped. Stub: 4875 passed, 144 skipped. Pinned AI: 1376 passed, 5 skipped. 140 mutations detected; 17 real SQL cases per backend and 30 real-bench integration tests across seven modules. |
| Temporary security + develop composition | `b5dfb94` with `15e9d22` | 5046 passed, 32 skipped in a disposable bench-path worktree. This was an uncommitted validation merge, not the release merge or a live deployment. |

Ruff, relevant JavaScript syntax, diff/scope checks and isolated pinned Semgrep
were clean for the security candidate. Its prepublication runs succeeded:
[CI](https://github.com/aerele/optimus/actions/runs/37131341374),
[AI Quality](https://github.com/aerele/optimus/actions/runs/37131344095) and
[Integration](https://github.com/aerele/optimus/actions/runs/37131346362).
The SQL adapter tests establish real locking/isolation behavior on MariaDB and
PostgreSQL. The integration workflow establishes the covered Frappe v16/Redis
handoffs. Neither substitutes for the missing checks below. Skips, framework
stubs and fake-provider tests are not successful live-model cases.

## Outstanding acceptance evidence

| Gate | State | Required evidence |
| --- | --- | --- |
| Owner-confirmed labels | PENDING | Confirm the 15-case corpus labels and reasons; DRAFT labels cannot establish acceptance. |
| BEFORE comparison | PENDING | Locate or reproduce the baseline using pinned historical code, source, model and configuration; record that a reconstruction is not a historical run. |
| Final context 0 | PENDING | Full reference-model run with default context settings, confirmed labels and a complete pinned scan. |
| Final context 4096 | PENDING | Full reference-model run with the server and Optimus context aligned to 4096, confirmed labels and baseline comparison. |
| Intermediate evaluation history | PENDING | Recover the planned intermediate evidence or explicitly record its absence; do not invent a four-run history from final-only results. |
| Recipe schema-sync durability | PENDING | Isolated MariaDB and PostgreSQL checks of the metadata/patch recipes before and after schema synchronization. |
| End-user permission matrix | PENDING | Real Frappe owner/share/User Permission and File hook cases on supported versions, with the accepted direct-download limitation kept explicit. |
| Upgrade and rollback | PENDING | Authorized existing-site deployment and backup-restore rehearsals on both databases, plus the browser/worker recovery journey below. |
| Final composed-tree checks | PENDING | CI, AI Quality and Integration on the final rebased aggregate head, with all diff and version conflicts reviewed. |
| Counts-only security re-check | PENDING | Repeat the scrub done-value check after final deployment and obtain owner-side historical rotated-key counts without exposing keys or rows. |
| Owner release sign-off | PENDING | Owner accepts evidence, residuals and deployment plan, merges, then publishes the advisory release. |

Key rotation has been completed by the owner. The direct private-file download
gap was explicitly accepted and is not being silently closed by these PRs.
Missing model labels and baseline evidence do not prevent implementation work,
but they do prevent a final model-quality verdict. Service operations on the
owner's bench require separate authorization; none is implied by this checklist.

## Model evaluation protocol

Use the existing [evaluation kit](../scripts/ai_eval/README.md); do not substitute
an ad hoc prompt or a different model to obtain a pass. The agreed reference is
keyless LAN Ollama with `qwen3-coder:30b`, timeout 180 seconds, raw-value consent
off, and the specified context variants. Confirm the actual model/server
configuration and availability before making calls. Record code head, dirty
state, corpus version, source drift, model identity, server context, Optimus
context and all relevant feature settings. Keep outputs outside the repository.
If the model digest or server version differs from the baseline, stop the
comparison for an owner decision on reconstruction or explicitly accepted drift;
do not silently relabel the new environment as the historical baseline.

The runner's `--num-ctx` records metadata only; it does not configure Ollama or
Optimus Settings. For context 0, retain the prescribed default and record the
actual server context. For 4096, configure and verify both ends as 4096 before
running. No secrets, session identifiers or Error Log text belong in the report.

The runner calls the product's completion path, validates worktree imports and
rolls back each corpus case without attributing spend to a Session. That does
not replace a test-site backup or permission to change its Settings/services.
Only the approved local test sites may be used. Reference-model unavailability
is an incomplete run, not permission to switch to a paid provider.

For each complete run, obtain owner labels, then score with the pinned Semgrep
installation and the confirmed baseline. Acceptance requires all corpus cases,
no harmful or fabricated displayed code, no Semgrep findings or skipped/failed
scans, at most two wrong answers displayed as code, no regression of a previously
correct answer, no unaccepted coverage losses and no unresolved loop/label
conflicts. Report correct-as-code, coverage losses, still-in-loop cases, blind
spots, truncation and context fit alongside the quality totals. Explain every
retired or newly gated finding category instead of counting silence as a fix.
Only the scorer's PASS
is a passing model verdict. Record FAIL when a known defect exists even if
other evidence is missing; otherwise incomplete evidence remains INCOMPLETE.
Report elapsed time, calls, known tokens and missing usage separately. Include
both final context variants and retain failures, not just the best attempt.

## Deployment preparation

1. Back up the database, private files and required site configuration under
   the operator's normal protected backup procedure. Verify restore access.
   Do not copy credentials into PR evidence. Rehearse on each supported database.
2. Record existing Settings, worker queues, scheduler state, app versions and
   schema. Check storage capacity and database privileges for migration. Install
   declared dependencies, including `nh3`; missing sanitizer support degrades to
   escaped text, not unrestricted HTML.
3. Drain or stop admitting new profiling/Phase 2/AI work using the deployment's
   normal maintenance procedure. Quiesce and replace old web, worker and scheduler
   processes. The Error Log hook cannot be safely introduced to a running image
   that cannot import it; a refreshed hook cache can break old Error Log inserts.
   Existing in-flight old workers do not gain new ownership or privacy checks.
4. Deploy the complete code stack, migrate the target site and clear caches
   through authorized deployment operations before new traffic. Model sync adds
   the internal AI journal and Phase 2 fields. Post-model-sync patches seed the
   mutex, seed an absent manual cap to 20 while preserving an explicit zero,
   scrub Error Logs and emit endpoint-policy guidance. Settings history and the
   default-off privacy field must be present. A missing journal must refuse work.
5. Start the replacement processes and confirm a worker listens on the configured
   AI queue (`long` by default), a worker listens on `long` for Phase 2, and the
   scheduler runs the recovery hooks. No worker implies no completion guarantee;
   the scheduler alone cannot execute the provider call. Leave AI disabled until
   configuration and smoke checks pass.
6. Complete the [key-leak runbook](../SECURITY.md#detecting-and-cleaning-a-key-leak),
   including its hook-cache check and counts-only scrub after restart. Verify
   `changed=0`, `deleted_docs_changed=0`, `residual=0`, `failed=0` and
   `key_unreadable=False`. `hooks_refreshed` is informative, not a completion gate.
   Historical snapshots, Versions, backups and external logs need their own
   retention/remediation decisions; new code does not rewrite them.

These are operator deployment steps, not authorization to run migration or
restart services in the owner's current checkout. Final rollout needs a concrete
approved maintenance window. Never mix old and new capture sample writers;
start a fresh capture after replacement. New capture input expires after
24 hours; the active flag expires after 10 minutes. Invalid old or corrupt
recordings need re-recording, not weaker input validation.

## Provider and privacy configuration

Keep the AI master switch off until a System Manager approves the destination
and disclosure. Automatic fixes default on when that master is enabled; turn
that switch off if each session should require explicit Refresh. Manual Steps
is separately controlled. Set context and timeouts to the actual model.
Aerele remains disabled; token counters in Optimus are informational, not a
prepaid balance or an authoritative provider invoice.

Use HTTPS except a deliberately trusted loopback/LAN deployment. Provider URLs
reject userinfo, query, fragment and prohibited literal addresses. Non-loopback
HTTP keys are withheld unless explicitly opted in. This validation is not a DNS
rebind firewall; restrict egress and DNS at the deployment boundary. A changed
destination clears an unchanged stored key. Enter a new credential explicitly
for its intended endpoint and use the administrator's escaped Test connection
result. Only same-origin 307/308 redirects within the documented limit are
followed. Do not solve endpoint errors by weakening those checks.

Leave `ai_send_raw_values` off unless business-value disclosure is approved.
SQL literals/comments and Steps document names/title are minimized by default;
source, finding titles and schema names can still contain sensitive information.
Review provider and local model logging/retention. Limit source access to the
configured bench app/environment boundaries and permission-checked Server Scripts.
Protect Redis and the site's encryption key; do not enable unsigned legacy
pickles as a general recovery measure. Persisted recording bundles are JSON only.
Existing reports/backups are not retroactively anonymized.

## End-user and recovery rehearsal

Use fake credentials and sanitized recordings. Record only booleans, counts and
fixed outcomes. Back up first and clean up exactly the created fixtures. Test
with an authorized owner, write-sharee, read-sharee and stranger; an Optimus role
alone does not authorize another user's session mutations.

| Scenario | Acceptance observation |
| --- | --- |
| Empty and large profiles, AI disabled | Profiling and report generation remain usable without any provider call. |
| Provider offline, malformed reply, timeout or repeated errors | Saved profiling remains Ready. Refresh shows the fixed failure; auth/quota/config/rate errors stop immediately and other consecutive failures stop at three. |
| Duplicate deliveries and concurrent refresh requests | One active session reservation and one claimed slice; no duplicate result, token or completion increments. |
| Worker death before/after send and during settlement | Pre-send work can recover delivery; a committed Calling attempt with unknown outcome becomes uncertain. Known outcome settlement is idempotent and does not repeat HTTP. |
| Redis outage, stale lease or queued expiry | SQL progress survives; recovery fences stale workers. No inline provider fallback, no automatic uncertain retry. |
| Cancellation during a call or permission/input change | No stale answer is saved. Late known usage is counted once. Completed profiling and prior results remain available. |
| Explicit resume and retry limits | Original selection/cap is retained, saved Steps are not billed again, at most three resumes per chain; Phase 2 has at most four admitted attempts. |
| Quota exhaustion or incomplete usage | Run stops on quota refusal. Reported usage and uncertainty stay distinct; missing usage is not claimed as zero cost. |
| Report write fails after a good answer | Answer/usage remain saved; old report survives. Regenerate Reports repairs pending HTML without a new AI call. |
| Browser loses status or has unsaved edits | Unknown state is not completion. Polling recovers and does not overwrite unsaved changes or release another Save hold. |
| Corrupt, missing, oversized, foreign or symlinked snapshot | Validation refuses it without executing pickle or substituting another session's file. Core data remains available; new capture may be necessary. |
| Malicious URL, source path or Server Script access | Endpoint/source controls refuse unauthorized inputs without disclosing credentials, arbitrary files or another actor's script. |
| Session deletion during refresh/capture | Parent and journal deletion commit together; late workers cannot recreate them. Exact capture cleanup does not stop another generation. |

Exercise these in a real supported deployment in addition to the deterministic
interleaving tests. Do not infer worker-crash recovery from a mocked happy path.

## Monitoring and rollback

Monitor queue depth/listening workers, scheduler activity, queued age, SQL run
state, failed/uncertain attempts and pending reports. Repeated SQL contention,
provider quota/auth failures and type-only masking breadcrumbs need investigation.
Use the [Error Log reference](AI-FIXING.md#error-log-reference) for fixed titles
and actions. Do not clear a stuck job by deleting its journal or resetting usage
fields: that destroys the duplicate/uncertain-call protections.

Disable new AI admissions while investigating a provider problem, cancel through
the authorized API where needed, and keep the core profiling result. Disabling AI
does not revoke data already sent. An uncertain attempt may already be billed;
explicit retry can bill again. No local counter can undo an external charge.

Rehearse rollback with a verified predeployment database/files backup and a
consistent code/process set. A code-only rollback has not been validated: older
code may not understand new journal fields, capture ownership, strict snapshots
or hook state. Quiesce jobs first; retain uncertainty evidence so restoring an
older database does not silently repeat an already-sent call. Do not delete new
DocTypes or downgrade security controls as an improvised fix. Prefer a reviewed
forward repair where possible. Document the data-loss window of any restore.

The owner signs off only after the pending table has evidence, required checks
are green on the final head, migration/rollback is rehearsed, known limitations
are accepted and model reports pass. The owner then merges and publishes the
GitHub Release with the key-leak advisory. This document does not mark that
sign-off complete.

## Deferred follow-up triggers

Deferral is not evidence of safety or a waived acceptance check. Record the
trigger outcome when the relevant evidence becomes available; do not expand
this documentation change into another implementation without review.

| Follow-up | Trigger and present disposition |
| --- | --- |
| Deterministic Redundant Call recipes and N+1 AST fixes | Revisit if final labelled answers remain wrong/harmful or keep the expensive call inside the loop. NOT EVALUATED: final model evidence is missing; older Redundant Call cases without corrected callsites remain unscored. |
| Per-type prompt assembly | Revisit after final context-fit/truncation and tokens-per-call measurements. NOT EVALUATED: current offline fit checks do not measure live model behavior. |
| Advice-tier changes | Inspect `dynamic-import` and `metadata-index` violations with owner labels. NOT EVALUATED: do not promote or retire tiers without those results. |
| Split the provider/HTTP module | Deferred maintainability work after security hardening merges. `ai_fix.py` still owns both concerns; a split must retain interrupt, key-binding and logging invariants. |
| Automatic AI after auto-armed Phase 2 | Revisit when an affected deployment needs it. The current behavior gives Phase 2 priority and requires manual AI Refresh afterward; no automatic rescheduling is promised. |
| Faster bundle decode or a per-session bundle index | Measure bounded post-hardening snapshots on an approved site first. NOT EVALUATED: no live bundle timing was collected here. |
| Native Frappe per-user rate limiter | Revisit when the supported Frappe release gains the required user-based API; the existing canary detects that change. |
| Real permission and composite-index coverage | The permission and schema-sync gates above still need an approved isolated test setup; SQL adapter coverage does not close them. |
| PostgreSQL duplicate-index residual | A PostgreSQL test site is available, so the original availability trigger is met. Real recipe/schema-sync verification remains pending, with no claim of resolution. |

Known evaluation coverage limits include the single-flow corpus, limited finding
types, masked source windows rather than full live recordings, no complete
recording-derived example-query measurement, and no hosted-provider quality
comparison. Record source/model drift and stochastic variation alongside the
labels. A single successful generation is not proof of consistency; never drop
failed attempts from the evidence. The accepted direct-download, DNS-rebinding
and external telemetry boundaries remain described in SECURITY.md.
