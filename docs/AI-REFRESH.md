# Background AI refresh engine

The engine is an internal foundation. No UI, API, analyzer or scheduler calls
it yet. Install the refresh wiring follow-up before releasing the feature.
That follow-up moves optional AI work after saved profiling results, connects
progress/cancellation, schedules recovery and replaces report attachments
safely. The security follow-up adds the recording, provider URL, source and
privacy restrictions. This document describes the engine that is implemented.

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
permission/rate/admission controls in the wiring layer. There is no automatic
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

Normal Frappe migration installs the journal schema and its admission mutex.
Fresh installation seeds the same mutex. Deploy code/schema before enabling
callers; restart web and workers together through the normal deployment process.
Do not route work to a worker running older code. A missing journal is an error,
never permission to run without concurrency controls.

| Site configuration | Default | Accepted values |
| --- | --- | --- |
| `optimus_ai_queue` | `long` | Lowercase queue name, 1 to 40 letters/digits/underscores/hyphens, starting with a letter |
| `optimus_ai_slice_seconds` | 120 | Integer, 30 to 1800 |
| `optimus_ai_refresh_max_seconds` | 3600 | Integer, 60 to 86400 |
| `optimus_ai_max_active_refreshes` | 2 | Integer, 1 to 1000 |

Invalid configuration is rejected. A custom queue needs a listening worker.
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

## Validation and remaining acceptance

Automated tests cover duplicate delivery, stale workers, cancellation during
HTTP, input/permission changes, durable send intent, atomic rollback, lost commit
acknowledgement, unknown outcomes, failure limits, queue loss, resume limits and
secret-safe interrupts. AI Quality runs real MariaDB and PostgreSQL transaction
checks on disposable test-owned tables. Those checks exercise real row locks
and isolation through a narrow database adapter; they do not replace Frappe
document-hook or end-user acceptance tests.

The wiring follow-up owns scheduled recovery, report replacement and UI/API
compatibility. Journal history cleanup belongs to the privacy/retention work
in the security follow-up. Until then, journal history is retained; uncertain
attempts must not be pruned while their parent exists, because that would erase
the protection against silently repeating an unknown call. Final model
evaluations and both-database end-user/deployment acceptance remain release
requirements, not results established by these unit tests.
