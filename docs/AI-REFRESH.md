# Background AI refresh engine

The engine is an internal foundation. Nothing starts a refresh yet: no UI, API
endpoint, analyzer or scheduler calls `start_refresh`, `cancel_refresh` or
`recover_pending`. The one touch point is #72's synchronous Refresh, which asks
`live_run` and refuses while the engine holds the session (see "One path at a
time"). The refresh wiring follow-up must land before the feature is released.
That follow-up moves optional AI work after saved profiling results, connects
progress/cancellation, schedules recovery and replaces report attachments
safely. The security follow-up adds the recording, provider URL, source and
privacy restrictions. This document describes the engine that is implemented
and how to operate it. The operator runbook is "Operating the engine" below.

## Durable state and duplicate delivery

Three private SQL DocTypes store the site admission mutex, refresh runs and
provider attempts. They grant no ordinary Desk/REST permissions. Records hold
identities, input hashes, fixed states and counts, never prompts, provider
responses or credentials. An active session reservation is unique; terminal
runs release it.

Every journal transaction starts fresh and its first statement locks the
session row. The caller passes the run's session name, read earlier in its own
transaction, so no plain read comes before that lock. Then the transition locks
the run and the attempt, in that order. The other reads come after the locks,
except that an admission's takeover reads the active run first and then locks it
by key (safe: every writer of that run locks the session row first, which the
admission holds).
On MariaDB 11.8, `innodb_snapshot_isolation` is on by default. With it, a lock
taken after a read view has opened fails with error 1020 ("Record has changed
since last read") when another transaction changed that row in between.
Because the locks come first, that cannot happen here. Each transition names
its session, and a run of another session is treated as missing.

No read of the journal tables takes a gap lock. The worker's recheck does read
the session's Phase 2 rows with a `FOR UPDATE` by filter. On MariaDB that locks
a range of that child table's parent index, so another session's Phase 2
insert can wait until this short transaction ends.

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
there. Cancellation is an HTTP path too and waits one timeout at most; a busy
cancellation answers `busy`. The dispatch an admission makes after its commit
takes the session row with NOWAIT, since the admission already waited, so one
click waits one timeout at most. A busy dispatch leaves the committed intent
pending, and the response still says `queued`. Worker transitions and settlement do retry a lock wait
timeout. One such operation can wait up to three timeouts. A settlement that
gives up and then writes its failed outcome is two operations, so it can wait
up to six.

The worker commits a Calling attempt before contacting a provider, then
releases journal/session locks during HTTP. The answer, the attempt outcome and
the run counters commit in one short transaction. That transaction also adds
the reported tokens to the session's `ai_tokens_spent` through #72's counter.
That counter is log-and-continue (D2). If its own statement fails, only the
increment is rolled back and logged (`optimus ai spend`), and the outcome
commits without it. The run's `tokens_reported` always keeps the count, so it
is the source for reconciling the session total.

The refresh count (`ai_refresh_count`) is counted once per accepted user
refresh, when the admission inserts its reservation, through #72's counter
(savepoint, log and continue: a failed increment is logged as
`optimus ai refresh count` and the admission still commits). A resume is a
click and counts once more. A refused admission (`busy`, a cap, `already_running`,
a session that is not Ready, an unresumable or exhausted resume, or a Refresh
running beside it) counts nothing, and so does an analyze-time `fixes_missing`
run, which is no click. No ending of a run counts anything, whatever it had
answered: not complete, stopped, cancelled, interrupted, abandoned, idle or
disabled.

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
those tokens twice. When both writes fail, the error reaches the worker
cleanup, which makes the attempt uncertain; its last row carries `tokens=` and
`outcome=` too.

RQ delivery is asynchronous, after commit, through Frappe's original enqueue
function. Each run/slice has a deterministic job ID; SQL claim is the execution
fence even if delivery is duplicated. A committed dispatch intent survives
Redis failure and remains pending until a worker claims it. The session's next
click sends it again, best effort: a click that attaches to a queued run whose
intent is still pending takes the session row with NOWAIT and enqueues it, and
neither a busy row nor an enqueue failure changes the click's answer. The sweep
does the same for every session. Either sends a slice at most once per 30
seconds; a continuation is dispatched at once, however soon its slice yields.
There is no inline fallback.

## Failure, cancellation and recovery

Runs move through queued/running to complete, stopped, cancelled or interrupted.
The worker rechecks the requester, session readiness, Phase 2 activity and
input signature before sending and before saving. A changed or deleted input
cannot receive a stale answer. A fix answer is guarded by the finding's own
fingerprint and the Finding row lock, so an unrelated save of the session
during the call (a Desk edit, a counter, a Phase 2 save) does not discard it;
the Steps answer rewrites the session's notes, so any save of the session since
it was prepared makes it stale. Reported usage from a late answer is still
accounted once, even when cancellation prevents saving the answer.

A lost worker leaves its Calling attempt uncertain. Expired leases and total
deadlines fence results immediately, and a queued run past its lease or deadline
is dead too: a late queue delivery cannot claim it, so it cannot be revived
beside a path that treated it as dead. Recovery does not need the scheduler: the
session's next admission takes over a run whose lease or deadline has passed.
In the same transaction it ends that run as interrupted, makes its open call
uncertain and admits the new run. Progress already shows such a run as
interrupted (`stalled`). The recovery sweep does the same for every session and
redelivers lost dispatches, but it only speeds recovery up. Each row is its own
transaction and takes its session row with NOWAIT, so a row a dead transaction
holds is skipped (`busy`), and one failing row never stops the others. A run
whose journal rows are inconsistent is released by a cancel, the sweep or a
takeover instead of holding its session: its open call counts as uncertain with
unknown usage, and one Error Log row names it. Cancellation names its session;
another session's run is refused (`not_running`) and nothing changes. An
unknown outcome is never automatically retried.
Later known usage can reconcile the attempt. A new run skips an uncertain
attempt with the same input unless the caller explicitly requests retry of
uncertain work, which may incur another provider charge.

Explicit resumes preserve the original selection time, cap and included
sections. The cap is cumulative over the resume chain: the fix attempts of the
run and of every run it resumed count against it, so a resume continues the
same refresh and does not grant the cap again. Saved Steps are carried forward
without another call or usage increment; an unfinished report remains marked
for regeneration. There are at
most three resumes in a chain, and a prior run can be
resumed only once. A deliberate new refresh is a new request, subject to
permission/rate/admission controls in the wiring layer. There is no automatic
provider retry after transport failure; the existing bounded parameter-repair
and guardrail re-ask behavior still applies inside a call.

The failure rules are #72's. An authentication, endpoint-not-found, quota or
configuration error (`ai_fix.AI_FATAL_KINDS`) stops the run at once, because
the next call would fail the same way until the operator acts. Any other
failure, a rate limit included, counts toward three in a row, which also stop
it; a success resets that streak. A prompt that does not fit the model's
context window (`context`) or that the provider refuses (`refused`) is recorded
under its own kind and never feeds the streak: it says nothing about the next
finding's prompt. A finding refused before any request (`not_eligible`, from
`ai_fix.AI_SKIP_KINDS`) is skipped, not failed. The engine keeps no list of
its own: it reads those two. Three slices in a row that could not begin any
item stop the run with `no_progress`. A provider switched off mid-run stops it
with `disabled`. Known reported tokens are kept for unusable
responses or failed local writes. Missing or contradictory usage is explicitly
incomplete, including reported zero versus an absent report. A refusal before
any request is a known zero, proven by the call count: the engine counts every
call it begins, and a failure without that proof is unknown usage. A cache
count Anthropic reports as null is not a missing one. Usage is
informational; Optimus has no prepaid token wallet and cannot reconcile an
unknown provider bill without a later known response.

Cancellation and polling read/write SQL without Redis. Attaching to an existing
refresh still works when the provider is disabled or worker discovery is down.
New admissions require a listening worker. Worker interrupts remain interrupts
after cleanup; logs use the shared scrubbed AI logging path. If SQL is entirely
unavailable, the committed Calling intent remains for later recovery.

## One path at a time

The synchronous Refresh (`api.refill_ai_suggestions`) and this engine must not
run on one session together: both would bill every finding and overwrite each
other's answers. Each refuses while the other holds the session, and a refusal
counts and bills nothing.

- The engine reads #72's Refresh flag (`optimus:ai_refresh:<session_uuid>`)
  fresh from Redis, with a raw GET, before it admits. While it is held, the
  answer is `{"status": "refused", "reason": "refresh_running"}` with a
  message. A run that already exists is still shown (attach), not refused.
- The Refresh looks up the session's live engine run (`ai_jobs.live_run`, read
  in a fresh transaction) before it takes its flag, counts itself or calls the
  provider. While one is live, it answers `{"ok": False, "busy": True}` with a
  message. A run whose lease or deadline has passed is dead and does not hold
  the session. A lookup that fails never blocks the Refresh, as a cache
  failure does not.

The two checks are separate reads, so a click on each path in the same
millisecond can still start both. The window is narrow and the engine is not
wired yet; the wiring follow-up removes the synchronous path and its flag.

## Selection, grounding and the Error Log

The run keeps the selection's `gated` and `excluded` counts
(`AiSelection.gated` and `.excluded`, #71's two Refresh toasts), taken once at
admission, and progress returns them. A background fix prompt gets the same
grounding as the synchronous Refresh: the Phase 2 hottest-line index and the
Slow Query index evidence. Both are built once per slice, not once per finding;
a failure of either is logged once with the session and the slice goes on
without it.

Every Error Log row the engine writes has a title that starts with
`optimus ai refresh` and carries `docname` (the session) and, where there is one,
`run_id` and `attempt`. Search the Error Log for `optimus ai`.

| Title | When |
| --- | --- |
| `optimus ai refresh worker` | A slice failed. Carries `tokens` and `outcome` when the outcome write gave up. |
| `optimus ai refresh cleanup` | The cleanup of a failed slice failed, so its call stays for recovery. |
| `optimus ai refresh item` | One item's provider call failed. Carries `attempt`, `kind` and `item` (`fix` or `steps`). |
| `optimus ai refresh result storage` | An answer or outcome write failed; the settlement rows carry `tries`, `tokens` and `outcome`. |
| `optimus ai refresh enqueue` | A slice could not be handed to RQ; the intent stays pending. |
| `optimus ai refresh worker availability` | Redis could not say whether a worker listens; new admissions are refused. |
| `optimus ai refresh recovery` | A row of the recovery sweep failed. |
| `optimus ai refresh release` | A run with an inconsistent journal row was force-released (takeover, cancel or sweep). |
| `optimus ai refresh selection` | The gated and excluded counts could not be taken; the run started with zero. |
| `optimus ai refresh evidence` | The Slow Query evidence could not be read; the prompts went without index advice. |
| `optimus ai refresh discarded` | A release-blocking signal: a paid answer was not saved because its attempt is stale or fenced. |
| `optimus ai refresh duplicate blocked` | A release-blocking signal: the journal stopped a second send or a second delivery. |

What state each title leaves the run in, and what to do, is in "Operating the
engine".

The session counters keep #72's `optimus ai spend` and `optimus ai refresh count`,
and a Phase 2 index failure keeps `optimus ai phase 2 context`.

## Configuration and deployment

Normal Frappe migration installs the journal schema and its admission mutex.
Fresh installation seeds the same mutex, and every migrate seeds it again if it
is missing (`after_migrate`). Deploy code/schema before enabling
callers; restart web and workers together through the normal deployment process.
Do not route work to a worker running older code. A missing journal is an error,
never permission to run without concurrency controls. A missing mutex row
fails a user refresh closed: admission raises `JournalUnavailable`, because the
caps need the mutex. The error names the repair, which seeds the row again:
`bench --site <site> execute optimus.patches.v0_12_0.initialize_ai_refresh_store.execute`.
The seed is idempotent. Analyze-time runs need only the session lock and the
unique reservation.

On Postgres, Frappe names an index after its column alone, and index names are
unique per schema. The Run and Attempt tables both index `session_name`, so the
second table to be created used to get no index. The same patch (run by install,
upgrade and every migrate) creates any journal index that is missing, under the
name `<table>_<column>_index`, and leaves the table that already has one alone.
MariaDB names indexes per table and is not touched.

| Site configuration | Default | Accepted values |
| --- | --- | --- |
| `optimus_ai_queue` | `long` | Lowercase queue name, 1 to 40 letters/digits/underscores/hyphens, starting with a letter |
| `optimus_ai_slice_seconds` | 120 | Integer, 30 to 1800 |
| `optimus_ai_refresh_max_seconds` | 3600 | Integer, 60 to 86400 |
| `optimus_ai_max_active_refreshes` | 2 | Integer, 1 to 1000 |

Every setting, the queue name included, is checked before admission, and
invalid configuration is rejected with the setting's name. A string of digits
counts as its number, which is what `bench set-config` stores without `-p`; set
the numbers with `bench --site <site> set-config -p <key> <value>`. A custom
queue needs a listening worker.
Manual runs additionally have a per-requester cap of two. Analyze-time
`fixes_missing` runs reserve their own session but do not consume the manual
site/user cap. The item cap applies even to Regenerate all, and across a resume
chain; zero means unlimited within the run deadline and bounded history limit.

Slices run on the `optimus_ai_queue` queue, `long` by default, which analysis
shares. A run holds one worker for up to a slice at a time, and the site cap
allows two manual runs, so a site with one worker has its profiled-flow
analyses wait behind a slice. Give the queue a worker of its own
(`optimus_ai_queue`, with a worker started for it) on a site that refreshes
often. Analyze-time `fixes_missing` runs are not capped per site; the wiring
follow-up decides that.

The default slice budget is 120 seconds. A slice processes at least one item
when its remaining total budget permits, then queues a continuation. Each call
uses the smaller of the configured AI timeout and the remaining run budget,
with a five-second reserve. A call is not started below `min(15, AI timeout)`.
RQ's timeout is slice budget plus AI timeout plus 300 seconds; the SQL running
lease uses a 180-second margin. Queued leases last 1800 seconds. Socket read
timeouts measure inactivity, so worker time limits remain necessary.

## Operating the engine

The three journal DocTypes have no permissions, so they have no Desk list or
report, even for a System Manager. Read them from the console or with SQL. The
queries below only read; the repair command further down is the one thing that
writes. Table names are `tab` plus the DocType name. The SQL is MariaDB's
(backticks); on Postgres use double quotes. A run's `lease_until`, `deadline`
and the other `*_epoch` columns are seconds since the epoch.

```
bench --site <site> console
>>> frappe.get_all("Optimus AI Refresh Run", filters={"session_name": "<session name>"},
...     fields=["name", "state", "end_reason", "breaker_kind", "cancelled_by", "tokens_reported",
...             "usage_incomplete", "uncertain", "lease_until", "deadline"], order_by="creation desc")
>>> frappe.get_all("Optimus AI Refresh Attempt", filters={"run_id": "<run id>"},
...     fields=["kind", "target_name", "state", "error_kind", "tokens_reported", "usage_complete", "settled"])
```

```sql
-- active runs, and whether they have expired (run in: bench --site <site> mariadb)
SELECT name, session_name, requested_by, state, slice_no, dispatch_pending,
       lease_until <= UNIX_TIMESTAMP() OR deadline <= UNIX_TIMESTAMP() AS expired
FROM `tabOptimus AI Refresh Run` WHERE active_session IS NOT NULL;

-- the same on Postgres (bench --site <site> postgres): quote the table, and use
--   lease_until <= extract(epoch from now()) OR deadline <= extract(epoch from now()) AS expired

-- the last Error Log rows the engine wrote
SELECT creation, method, reference_name, LEFT(error, 300) AS error
FROM `tabError Log` WHERE method LIKE 'optimus ai refresh%' ORDER BY creation DESC LIMIT 50;

-- a session's attempts that nobody will repeat: their usage is unknown until a later reply
SELECT run_id, kind, target_name, error_kind FROM `tabOptimus AI Refresh Attempt`
WHERE session_name = '<session name>' AND state = 'uncertain';
```

### What you see, and what to do

| You see | It means | What to do |
| --- | --- | --- |
| **Stuck**: a run is `queued` or `running` and `public_state` says `stalled`, or the query above shows `expired` | The worker died or lost its lease (`lease_until`), or the total `deadline` passed. Nothing is writing the run. | Nothing is lost and nothing needs cancelling. The session's next admission takes the run over and ends it as `interrupted` (`end_reason` `lease_expired` or `deadline`), its open call becomes `uncertain`. To clear sessions without a click, run `bench --site <site> execute optimus.ai_jobs.recover_pending`; it prints the counts `interrupted`, `dispatched`, `busy` and `failed`, and handles up to 100 runs per pass, so run it again until `interrupted` and `dispatched` are 0. No scheduler runs the sweep yet. |
| **Queued, never starts**: a run stays `queued` with `dispatch_pending` 1 | The slice never reached a worker: no worker listens on the queue (`optimus_ai_queue`), or Redis is down. | Start a worker for that queue. The next click on the same run, or the sweep, sends the slice again, at most once per 30 seconds. See the `optimus ai refresh enqueue` row. |
| **Uncertain**: attempts with `state` `uncertain`, a run with `uncertain` above 0, or `end_reason` `uncertain` (the only items left were skipped because an uncertain attempt with the same input exists) | A call may have reached the provider and its outcome was never recorded. It is never repeated automatically; its usage is unknown. | Check the provider's usage page for the window. A new refresh skips the item while its input is unchanged. Only a deliberate retry of uncertain work (`retry_uncertain`) sends it again, and that can be billed twice. |
| **Cancelled**: `state` `cancelled`, `end_reason` `cancelled`, `cancelled_by` names the user | A user stopped it. A call already in flight is not interrupted: its reply is paid for, its tokens are counted, and its answer is discarded (a `discarded` row, `reason=fenced`). | None. A cancelled run can be resumed once. |
| **Expired**: `state` `interrupted`, `end_reason` `lease_expired` or `deadline` | See Stuck. | A run ended this way can be resumed once. |
| **Incomplete usage**: `usage_incomplete` above 0 on the run, `usage_complete` 0 on an attempt | The provider's usage was missing, contradictory or unknown for that call, so the reported tokens may undercount. | Do not trust the run's token total for those attempts; compare with the provider's bill. |
| **Busy**: a click answers `{"status": "refused", "reason": "busy"}` | Another transaction holds the session row for a whole lock wait, or another admission holds the site mutex. Nothing was started or counted. | Try again. If it stays busy, look for a long or dead transaction on that Optimus Session row (`SHOW PROCESSLIST` on MariaDB, `pg_stat_activity` on Postgres). |
| **Already running**: a click answers `{"status": "already_running", ...}` | The session has a live run; the answer carries its progress. | Nothing: the click attached to it. |
| **Refused**: `refresh_running`, `disabled`, `no_worker`, `queue_unavailable`, `not_ready`, `site_cap`, `user_cap`, `not_resumable`, `already_resumed`, `retry_limit` | `refresh_running`: #72's Refresh holds the session. `disabled`: AI is off. `no_worker`: no worker listens on the queue. `queue_unavailable`: Redis could not say. `not_ready`: the session is not Ready. `site_cap`, `user_cap`: the caps are reached. The last three refuse a resume. | Fix the cause named; none of them counts or bills. Three other cases raise instead of answering: `StopRefresh` with the reason `not_ready`, `permission` (the requester has no access) or `phase2` (a Phase 2 run is in progress), and an error naming a site setting that is not a valid whole number (see Configuration). |
| **Journal unavailable**: the error `AI refresh storage is not initialized` | The admission mutex row is missing (a rollback, or a failed install). A user refresh fails closed. | Run the repair command below. |
| **Inconsistent**: the error `Refresh journal ... is inconsistent` | A run's reservation or attempt row disagrees with the run. | Cancel the run: that releases it, its open call counts as uncertain with unknown usage, and one `optimus ai refresh release` row names the problem. A takeover or the sweep does the same for an expired run. |

### Values you will find in the journal

| Column | Values |
| --- | --- |
| Run `state` | `queued`, `running`, then `complete`, `stopped`, `cancelled` or `interrupted`. Only `queued` and `running` hold the session. |
| Run `end_reason` | `complete`; `uncertain` (nothing left but uncertain items); `breaker` (see `breaker_kind`); `deadline` (the total time was spent); `no_progress` (three slices in a row began no item); `disabled` (AI switched off mid-run); `not_ready`, `permission`, `phase2` (the session, the requester's access or a Phase 2 run changed); `history_limit` (more than 10000 attempts to weigh); `cancelled`; `lease_expired` and `deadline` (a run interrupted by a takeover or the sweep); `worker_interrupted` (the worker failed and its cleanup ended the run). |
| Run `breaker_kind` | The failure kind that stopped the run: `auth`, `quota`, `not_found` or `config` at once, or the kind of the third failure in a row (for example `rate_limited`, `timeout`, `server`, `transport`). `context` and `refused` never set it. |
| Run `cancelled_by` | The user id (normally the email) that cancelled; empty otherwise. |
| Attempt `state` | `calling`, then `succeeded`, `failed`, `skipped`, `uncertain` or `discarded`. `error_kind` is the failure kind (`not_eligible` or `deadline` for a skipped item). |

### Error Log title to state

| Title | The run is | What to do |
| --- | --- | --- |
| `optimus ai refresh worker` | `interrupted` with `end_reason` `worker_interrupted`, its open call `uncertain` (a failure before the claim leaves it `queued`; when the cleanup itself failed, see `cleanup`) | Read the row for the cause. The row's `tokens` and `outcome` say whether the call's usage is recorded. |
| `optimus ai refresh cleanup` | Still `running` with its call open until the lease expires | Wait for the lease, or click Refresh: the takeover ends it. |
| `optimus ai refresh item` | Going on, unless this is the third in a row or a fatal kind | The row's `kind`, `fatal` and `hint` say what to fix (AI-FIXING section 6.5). |
| `optimus ai refresh result storage` | Going on, or interrupted when both writes failed | Its `outcome` says: `committed on try N`, `committed: the error came after the COMMIT ...` or `recorded: ...` (the outcome and its tokens are saved; the write's error is still logged), `recorded by fallback` (the tokens are saved, the answer is not), `not committed after try N`, or `unknown` (the tokens may well be recorded). |
| `optimus ai refresh enqueue` | `queued`, intent pending | Redis or the queue is down; the intent is sent again by the next click or sweep. |
| `optimus ai refresh worker availability` | None started | New admissions answer `queue_unavailable` until Redis answers. |
| `optimus ai refresh recovery` | Other rows of the sweep were done | The row names the run; the next sweep or admission tries again. |
| `optimus ai refresh release` | `cancelled` or `interrupted`, its open call `uncertain` with unknown usage | A journal row was inconsistent and the run was released rather than hold its session; the row names which row. Inspect the Attempt rows of the run. |
| `optimus ai refresh selection`, `optimus ai refresh evidence` | Going on | The counts were zero, or the prompts went without index advice. |
| `optimus ai refresh discarded`, `optimus ai refresh duplicate blocked` | Going on | See the signals below. |

### The release-blocking signals

Three things must be visible before the feature is released: a second provider
call that was prevented, a stale answer that was dropped, and a charge that
does not add up. Each of the first two is one Error Log row, written when it
happens (after the transaction that decided it ends), with `docname` (the
session), `run_id`, and `attempt` and `kind` where there is one.

- `optimus ai refresh duplicate blocked` (`signal=duplicate_blocked`): `reason=already_claimed`
  means RQ delivered a slice that was already claimed or already over, and the
  SQL claim refused it (`slice_no` is the delivered slice). `reason=already_attempted`
  means a second send of an item the run had already attempted was stopped by
  the attempt's unique key. No provider call was made either way. The first is
  ordinary when RQ redelivers; the second should not happen, because the
  selection skips attempted items, so treat each one as a bug to find.
- `optimus ai refresh discarded` (`signal=discarded`): a successful reply was
  not saved. `reason=fenced`: the worker no longer owns the run (cancelled,
  interrupted, or its lease or deadline passed). `reason=session_changed`: the
  session left Ready. `reason=input_changed`: the finding, or the session for a
  Steps answer, changed during the call, or the requester lost access, or a Phase 2 run
  started. `reason=settled`: the attempt already
  had an outcome. The tokens are still counted, except for a `fenced` reply whose run or
  attempt row is missing or belongs to another worker. That cannot happen
  without the journal rows being edited by hand. Such a row carries `tokens=`
  and is the only record of them: they are not charged, because without the
  attempt row nothing stops a second charge when a lost COMMIT acknowledgement
  makes settlement fall back. A few rows after cancels and edits are normal, a steady stream means workers lose their lease
  mid-call (check worker load and the AI request timeout).
- A charge that does not add up: run the reconciliation below.

### Reconciling the charge

Three numbers describe one session's AI tokens: the session's `ai_tokens_spent`,
the runs' `tokens_reported`, and the increments #72's counter logged as dropped
(`optimus ai spend`, each with `amount=`; a statement-level counter failure
drops the increment but the outcome still commits). The query lists, for every
session that has a run, the counter, the journal's total, the dropped amounts
and `other_sources = spent + dropped - journal_tokens`. That is the tokens from
everything the journal does not see: the analyze-time suggestions, the Steps
rewrite and #72's own Refresh.

```sql
-- reconcile (MariaDB)
SELECT s.name AS session, COALESCE(s.ai_tokens_spent, 0) AS spent, j.tokens AS journal_tokens,
       COALESCE(d.amount, 0) AS dropped_increments,
       COALESCE(s.ai_tokens_spent, 0) + COALESCE(d.amount, 0) - j.tokens AS other_sources
FROM `tabOptimus Session` s
JOIN (SELECT session_name, SUM(tokens_reported) AS tokens
      FROM `tabOptimus AI Refresh Run` GROUP BY session_name) j ON j.session_name = s.name
LEFT JOIN (SELECT reference_name,
                  SUM(CAST(REGEXP_SUBSTR(REGEXP_SUBSTR(error, 'amount=[0-9]+'), '[0-9]+') AS UNSIGNED)) AS amount
           FROM `tabError Log` WHERE method = 'optimus ai spend' AND reference_doctype = 'Optimus Session'
             AND error LIKE '%savepoint_rollback=done%'
           GROUP BY reference_name) d ON d.reference_name = s.name
ORDER BY other_sources;
```

```sql
-- reconcile (PostgreSQL)
SELECT s.name AS session, COALESCE(s.ai_tokens_spent, 0) AS spent, j.tokens AS journal_tokens,
       COALESCE(d.amount, 0) AS dropped_increments,
       COALESCE(s.ai_tokens_spent, 0) + COALESCE(d.amount, 0) - j.tokens AS other_sources
FROM "tabOptimus Session" s
JOIN (SELECT session_name, SUM(tokens_reported) AS tokens
      FROM "tabOptimus AI Refresh Run" GROUP BY session_name) j ON j.session_name = s.name
LEFT JOIN (SELECT reference_name,
                  SUM(CAST(substring(error from 'amount=([0-9]+)') AS bigint)) AS amount
           FROM "tabError Log" WHERE method = 'optimus ai spend' AND reference_doctype = 'Optimus Session'
             AND error LIKE '%savepoint_rollback=done%'
           GROUP BY reference_name) d ON d.reference_name = s.name
ORDER BY other_sources;
```

Only `savepoint_rollback=done` rows count as dropped: the counter rolled back to
its savepoint and the outcome committed without the increment. A
`savepoint_rollback=failed` row means the whole transaction was gone and the
settlement was retried, so those tokens were charged on the retry.

Read `other_sources`: **negative** means the session counter holds fewer tokens
than the journal reported and no `optimus ai spend` row explains it. Settlement
saves the counter, the attempt and the run in one transaction, so a failed
settlement does not cause this. Look for a spend row that was pruned (below), a
reply whose count is above 2147483647 (the counter ignores it; the engine logs an
`optimus ai refresh item` row with `outcome` `clamped`), a `discarded` row with
`reason=fenced` and `tokens=` (an attempt row that was edited or deleted), or an
overwrite of `ai_tokens_spent` by hand. **Zero** is a session whose AI use was
all through the engine. **Above zero** is another source's tokens; the query
cannot tell it apart from a double count, so for a session you have only used
through the engine a positive value is a double charge.

Frappe prunes the Error Log after 14 days (`default_log_clearing_doctypes`), and
with it the spend rows and the two signal rows. The journal is never pruned.
Reconcile within that window, or raise the Error Log retention in Log Settings.

The journal also checks itself: the query below
returns no row when every run's `tokens_reported` equals the sum of its
attempts.

```sql
-- self-check (MariaDB)
SELECT r.name AS run, r.tokens_reported, a.tokens AS attempts_tokens
FROM `tabOptimus AI Refresh Run` r
JOIN (SELECT run_id, SUM(tokens_reported) AS tokens FROM `tabOptimus AI Refresh Attempt` GROUP BY run_id) a
  ON a.run_id = r.name
WHERE a.tokens <> r.tokens_reported;
```

### Repair, rollback and the settings

- **Repair.** `bench --site <site> execute optimus.patches.v0_12_0.initialize_ai_refresh_store.execute`
  seeds the admission mutex row if it is missing, and on Postgres creates any
  journal index that is missing. It is idempotent and is also what install,
  upgrade and every `bench migrate` run. It cannot create the tables: that is
  `bench migrate`.
- **Rollback.** A release without the journal makes Frappe delete its DocType
  records (`remove_orphan_doctypes`) and `bench trim-database` then drops their
  tables. Do not run `trim-database` during a rollback you may undo: it drops
  the uncertain attempts that keep an unknown provider call from being sent
  again.
- **Settings.** `bench set-config` stores a string unless it is given `-p`. A
  string of digits is accepted as its number, but set numbers with
  `bench --site <site> set-config -p <key> <value>`. An out-of-range or
  non-numeric value makes an admission raise an error that names the setting.

## Data retention

Nothing deletes the journal. A Run and its Attempts stay after the run ends,
after the session is swept by the retention janitor and after the session is
deleted. Each Run holds `requested_by`, and a cancelled one `cancelled_by`: user
ids, normally email addresses, that therefore outlive the session's own
retention. The rows hold no prompts, answers or credentials, only identities,
hashes, states and counts. Uncertain attempts must not be pruned while their
session exists, because that would erase the protection against repeating an
unknown call. The retention work of the security follow-up (#75) owns pruning
and the treatment of those ids; nothing here implements it.

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
conflicts above, on every leg, and they run the reconciliation query of the
runbook as written. The Postgres role must be a superuser (CI's `POSTGRES_USER`
is): one test sets `deadlock_timeout`. They do not replace Frappe document-hook
or end-user acceptance tests.

The wiring follow-up owns scheduled recovery, report replacement and UI/API
compatibility. Journal retention is described under "Data retention". Final
model evaluations and both-database end-user/deployment acceptance remain
release requirements, not results established by these unit tests.
