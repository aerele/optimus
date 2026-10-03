# Background AI refresh engine

Refresh AI suggestions queues optional work after the profiling result has
been saved. Analyze-time AI also starts only after the session is Ready. A
provider timeout, queue outage, cancellation or failed report replacement does
not roll back the saved profile. Regenerate Reports renders saved results
without contacting a provider.

The Desk form shows progress, cancellation, reported usage and uncertainty.
Manual refresh selects missing and outdated suggestions by default. The
confirmation shows the selected count; replacing current suggestions requires
an explicit checkbox. Refresh and an active Phase 2 pass exclude one another.
Auto-armed Phase 2 therefore takes precedence over automatic AI, with an
admission notice on the session. Refresh AI after Phase 2 finishes.

This is part of a staged integration. Recording/provider/source/privacy
hardening and final acceptance remain required before release.

## Durable state and duplicate delivery

Three private SQL DocTypes store the site admission mutex, refresh runs and
provider attempts. They grant no ordinary Desk/REST permissions. Records hold
identities, input hashes, fixed states and counts, never prompts, provider
responses or credentials. An active session reservation is unique; terminal
runs release it. Admission locks the site mutex, then the session. Other
transitions lock the session, run and attempt in that order.

The mutex is updated on every successful admission. This also fences an older
PostgreSQL REPEATABLE READ snapshot. Definite serialization, deadlock and lock
conflicts retry at most three SQL transactions, with bounded delay. Admission
uses NOWAIT on the site mutex. These retries never send HTTP.

The worker commits a Calling attempt before contacting a provider, then
releases journal/session locks during HTTP. Answer persistence, usage, attempt
outcome and run counters share one transaction. Each settlement is idempotent;
retrying an ambiguous SQL commit acknowledgement does not repeat the provider
request or usage increment. Completion accounting also shares its transaction
with release of the session reservation.

RQ delivery is asynchronous, after commit, through Frappe's original enqueue
function. Each run/slice has a deterministic job ID; SQL claim is the execution
fence even if delivery is duplicated. A committed dispatch intent survives
Redis failure and remains pending until a worker claims it. Recovery can
redeliver it, at most once per 30 seconds per run. There is no inline fallback.

## Failure, cancellation and recovery

Runs move through queued/running to complete, stopped, cancelled or interrupted.
The worker rechecks the requester, session readiness, Phase 2 activity and
input signature before sending and before saving. A changed or deleted input
cannot receive a stale answer. Reported usage from a late answer is still
accounted once, even when cancellation prevents saving the answer.

A lost worker leaves its Calling attempt uncertain. Expired leases and total
deadlines fence results immediately; the recovery sweep records interruption
and releases admission. An unknown outcome is never automatically retried.
Later known usage can reconcile the attempt. A new run skips an uncertain
attempt with the same input unless the caller explicitly requests retry of
uncertain work, which may incur another provider charge.

Explicit resumes preserve the original selection time, cap and included
sections. Saved Steps are carried forward without another call or usage/count
increment; an unfinished report remains marked for regeneration. There are at
most three resumes in a chain, and a prior run can be
resumed only once. A deliberate new refresh is a new request, subject to
permission, rate and admission controls. There is no automatic
provider retry after transport failure; the existing bounded parameter-repair
and guardrail re-ask behavior still applies inside a call.

Authentication, endpoint-not-found, quota, configuration and rate-limit errors
stop the run immediately. Three consecutive other failures also stop it;
success resets that streak. Known reported tokens are kept for unusable
responses or failed local writes. Missing or contradictory usage is explicitly
incomplete, including reported zero versus an absent report. Usage is
informational; Optimus has no prepaid token wallet and cannot reconcile an
unknown provider bill without a later known response.

Cancellation and polling read/write SQL without Redis. Attaching to an existing
refresh still works when the provider is disabled or worker discovery is down.
New admissions require a listening worker. Worker interrupts remain interrupts
after cleanup; logs use the shared scrubbed AI logging path. If SQL is entirely
unavailable, the committed Calling intent remains for later recovery.

## Configuration and deployment

Normal Frappe migration installs the journal schema, Phase 2 queue fields and
admission mutex. Fresh installation seeds the same mutex. A post-model-sync
patch gives previously saved Settings a manual refresh cap of 20 when the
field was absent; an explicitly saved zero remains unlimited. Deploy code and
schema before routing traffic to new callers. Restart web, workers and the
scheduler together through the normal deployment process.
Do not route work to a worker running older code. A missing journal is an error,
never permission to run without concurrency controls.

| Site configuration | Default | Accepted values |
| --- | --- | --- |
| `optimus_ai_queue` | `long` | Lowercase queue name, 1 to 40 letters/digits/underscores/hyphens, starting with a letter |
| `optimus_ai_slice_seconds` | 120 | Integer, 30 to 1800 |
| `optimus_ai_refresh_max_seconds` | 3600 | Integer, 60 to 86400 |
| `optimus_ai_max_active_refreshes` | 2 | Integer, 1 to 1000 |

Invalid configuration is rejected. A custom queue needs a listening worker.
Keep the scheduler enabled: minute recovery sweeps expire lost leases and
redeliver committed queue intents. The scheduler does not replace a worker.
Phase 2 additionally requires a worker on `long`, even when AI uses a custom
queue. Disabling the scheduler or losing all workers delays recovery; it never
enables synchronous AI or Phase 2 analysis in a web request.

Optimus Settings exposes **Maximum findings per AI refresh** (20 by default; zero is
unlimited within the job deadline) separately from **Max auto-suggested findings per session**.
Sensitivity profiles do not change the manual cap. **AI model context window (tokens)**
exposes the existing context-fit limit; zero selects the provider/model default.
Settings cache failures during the seed print a fixed recovery instruction to
clear the cache after restart.
Manual runs additionally have a per-requester cap of two. Analyze-time
`fixes_missing` runs reserve their own session but do not consume the manual
site/user cap. The item cap applies even to Regenerate all; zero means unlimited
within the run deadline and bounded history limit.

The default slice budget is 120 seconds. A slice processes at least one item
when its remaining total budget permits, then queues a continuation. Each call
uses the smaller of the configured AI timeout and the remaining run budget,
with a five-second reserve. A call is not started below `min(15, AI timeout)`.
RQ's timeout is slice budget plus AI timeout plus 300 seconds; the SQL running
lease uses a 180-second margin. Queued leases last 1800 seconds. Socket read
timeouts measure inactivity, so worker time limits remain necessary.

## API compatibility and report recovery

`refill_ai_suggestions(session_uuid)` still accepts an existing session-only
request, but now returns `queued`, `already_running` or `refused` with SQL
progress or a fixed explanation. It no longer returns completed inline work.
`regenerate_all` explicitly includes current answers. `resume_from` identifies
a stopped run; `retry_uncertain` is explicit consent to possible duplicate
provider charges. Resume preserves the original selection time and cap.

`ai_refresh_status` requires session read permission and returns counts, states
and usage. Only an actor who may update the session receives a new selection
plan. `cancel_ai_refresh` requires the action gate and binds its run identity
to that session. Polling and cancellation are not rate-limited and do not need
Redis. New manual requests retain the per-user rate limit. Permission checks
always precede queue/provider activity.

A status-read failure is unknown, never proof of completion. Desk keeps Save
held until state becomes known, polls again with backoff, and rejects late
responses. It releases only the Save hold it acquired. Completion does not
reload over unsaved edits. Save or discard those edits before reloading to see
new results; the server may reject an old form's save after background changes.

Report HTML is built outside SQL locks. The new private File, report reference,
PDF-reference invalidation and pending-render flags commit together after a
current session/worker check. The previous attachment survives a failed or
stale replacement. Old File records remain for normal retention. A pending
report can be repaired with Regenerate Reports, without repeating AI calls.
Retry Analyze atomically fences optional workers before resetting a Failed
session to Stopping; late AI usage can still be accounted without saving a
stale answer.

## Phase 2 delivery

Stopping or retrying Phase 2 queues analysis on `long`. The response reports
`Analyzing` and `ran_inline: false`; a disabled scheduler never selects inline
compute. Batch retry accepts at most five runs and uses each run's per-user
limit. A live generation is attached to, not restarted. Completed or Recording
runs cannot be retried through the analysis retry endpoint.

SQL child state stores each generation, requester, claim, dispatch intent and
lease. Parent and direct child saves cannot forge, reset or erase that internal state. There
are at most four admitted attempts per captured run, including the initial
attempt. A queued generation expires after 30 minutes; a claimed job has a
25-minute worker timeout and a 27-minute SQL lease. Queue redelivery is
throttled to 30 seconds and never repeats a claimed generation. After failure
or expiry, use explicit Retry; after the attempt limit, record a new pass.

Missing capture metadata is a failure, not a successful empty analysis. Valid
picks with no invocations still produce uninvoked-function diagnostics. Failed
analysis retains available Redis input for explicit retry; missing/expired
input requires a new capture. Findings and Ready commit together under the
current generation. A later render failure leaves those findings Ready and
marks the report pending. Regenerate Reports repairs that report without
repeating analysis. Old queued deliveries are adopted only while their row is
Analyzing and has no new generation. Replace old workers during deployment;
an already-running old process does not gain the new ownership checks.

## Validation and remaining acceptance

Automated tests cover duplicate delivery, stale workers, cancellation during
HTTP, input/permission changes, durable send intent, atomic rollback, lost commit
acknowledgement, unknown outcomes, failure limits, queue loss, resume limits and
secret-safe interrupts. AI Quality runs real MariaDB and PostgreSQL transaction
checks on disposable test-owned tables. Those checks exercise real row locks
and isolation through a narrow database adapter; they do not replace Frappe
document-hook or end-user acceptance tests.

Journal history cleanup belongs to the privacy/retention work
in the security follow-up. Until then, journal history is retained; uncertain
attempts must not be pruned while their parent exists, because that would erase
the protection against silently repeating an unknown call. Final model
evaluations and both-database end-user/deployment acceptance remain release
requirements, not results established by these unit tests.
