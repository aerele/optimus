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
runs release it.

Every journal transaction starts fresh and its first statement locks the
session row. The caller passes the run's session name, read earlier in its own
transaction, so no plain read comes before that lock. Then the transition locks
the run and the attempt, in that order. All other reads come after the locks.
On MariaDB 11.8, `innodb_snapshot_isolation` is on by default. With it, a lock
taken after a read view has opened fails with error 1020 ("Record has changed
since last read") when another transaction changed that row in between.
Because the locks come first, that cannot happen here. Each transition names
its session, and a run of another session is treated as missing.

Admission locks the session row first. When another writer holds that row for
a whole lock wait timeout, the admission answers `busy` instead of trying again.
Only a user refresh (`scope` all, the only scope with caps) then takes the site
mutex, with NOWAIT. When another admission holds the mutex, the answer is a
`busy` refusal at once, never a wait or an error. An admission that waits on its
own session therefore never holds the mutex, and analyze-time `fixes_missing`
runs never take it. The capacity
count is a plain read after those locks. Admissions with caps serialize on the
mutex, and each one updates the mutex row. An older PostgreSQL REPEATABLE READ
snapshot then fails on that row (40001) and is retried, so the caps stay exact
on both databases. Admission takes no lock on a range of runs: such next-key
locks deadlocked other sessions' workers. The unique reservation decides
`already_running`: an insert that hits it is refused, even when the admission's
snapshot could not see the other run.

A new attempt is an INSERT on its unique key, with no read of that key first.
A locking read of a missing key takes a gap lock on MariaDB, and two runs
beginning at once then deadlocked. A duplicate key means the item was already
attempted in this run, so no second call is sent.

SQL conflicts are retried as whole short transactions, at most three in all,
with a short delay. These are deadlocks, lock waits, snapshot conflicts and
serialization failures, raw or as Frappe wraps them. On MariaDB that means
1213, 1205 and 1020, and Frappe's `QueryDeadlockError` and
`QueryTimeoutError`. On Postgres it means 40001, 40P01 and 55P03. A Postgres
statement timeout (57014) is not retried. A retry never sends HTTP.

A lock wait timeout (MariaDB 1205, or Postgres 55P03 when `lock_timeout` is set)
is not retried by an admission, which is an HTTP request. It answers `busy`, so
the request waits at most one `innodb_lock_wait_timeout`, well inside the web
server's timeout. Deadlocks, 1020 and serialization failures are still retried
there. Worker transitions and settlement do retry a lock wait timeout. One such
operation can wait up to three timeouts. A settlement that gives up and then
writes its failed outcome is two operations, so it can wait up to six.
Cancellation still retries a lock wait timeout too; its HTTP endpoint lands with
the wiring follow-up.

The worker commits a Calling attempt before contacting a provider, then
releases journal/session locks during HTTP. The answer, the attempt outcome and
the run counters commit in one short transaction. That transaction also adds
the reported tokens to the session's `ai_tokens_spent` through #72's counter.
That counter is log-and-continue (D2). If its own statement fails, only the
increment is rolled back and logged (`optimus ai spend`), and the outcome
commits without it. The run's `tokens_reported` always keeps the count, so it
is the source for reconciling the session total. The completion count
(`ai_refresh_count`) works the same way. Its failure is logged
(`optimus ai refresh count`), and the terminal state and the release of the
reservation still commit.

Settlement follows #72's `_write_ai_answer` discipline. The first try commits
whatever the worker's transaction holds, so no read view from before the call
remains. A conflict, including a whole-transaction rollback, is rolled back and
the write tried again, three tries in all. The attempt's `settled` bit makes a
repeated write a no-op, so neither the provider call nor the usage increment
repeats. Settlement also retries a lock wait timeout and a Postgres
serialization failure, which #72's write does not. Only this write takes
locking reads, and its `settled` bit makes a retry safe. After any other
failure, the `settled` bit is read again in a fresh query. When it is set, the
outcome is saved even though the COMMIT reported an error. When it cannot be
read, the outcome is logged as unknown: its tokens may well be recorded, as #72
says. When the answer's write did not commit, a second write records the
outcome as failed with the known tokens and without the answer. Every row is
written once both writes have ended, and each carries an `outcome=`. That
includes the counter's own rows and each write's failure, with `tries` and
`tokens`. When the second write recorded the tokens, the first write's rows say
`recorded by fallback`. A reconciler reading the Error Log therefore never adds
those tokens twice.

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
never permission to run without concurrency controls. A missing mutex row
refuses user refreshes, whose caps need it. Analyze-time runs need only the
session lock and the unique reservation.

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
HTTP, input/permission changes, durable send intent, settlement retries, lost
commit acknowledgement, unknown outcomes, failure limits, queue loss, resume
limits and secret-safe interrupts. AI Quality runs real transaction checks on
three databases: MariaDB 10.6, MariaDB 11.8 (snapshot isolation on) and
PostgreSQL 16. The tables are disposable and test-owned. The adapter shapes
them like a Frappe site. It builds the tables with Frappe's DDL rules
(indexes, unique columns, defaults) and raises errors the way
`frappe.db.sql` and `Document.insert` do. It also runs #72's real session
counters. The checks include regression probes for the deadlocks and 1020
conflicts above, on every leg. They do not replace Frappe document-hook or
end-user acceptance tests.

The wiring follow-up owns scheduled recovery, report replacement and UI/API
compatibility. Journal history cleanup belongs to the privacy/retention work
in the security follow-up. Until then, journal history is retained; uncertain
attempts must not be pruned while their parent exists, because that would erase
the protection against silently repeating an unknown call. Final model
evaluations and both-database end-user/deployment acceptance remain release
requirements, not results established by these unit tests.
