# Security Policy

## Supported versions

| Version | Status |
|---|---|
| 0.7.x | Active development; security fixes ship in patch releases. |
| 0.6.x | Frozen; no further fixes. Migrate to 0.7.x (fresh deploy). |
| 0.5.x and earlier (`frappe_profiler`) | Unsupported. |

## Reporting a vulnerability

**Do not file a public GitHub issue for security bugs.** Email
`security@aerele.in` with:

- Affected Optimus version.
- Reproduction steps + minimal proof-of-concept.
- Expected vs. observed behaviour.
- Your preferred attribution name + GitHub handle (for the
  CHANGELOG credit) or "anonymous" if you'd rather not be named.

We aim to:

- **Acknowledge** new reports within **48 hours**.
- **Triage + assign severity** within **5 business days**.
- **Patch HIGH / CRITICAL** within **14 days** of triage.
- **Publish a CVE-style advisory** in the CHANGELOG once the patch
  ships, crediting the reporter.

## Threat model (v0.7.x)

Optimus runs inside a Frappe bench process, with full DB +
filesystem access via the Frappe stack. The following surfaces are
the highest-value security considerations:

1. **Recordings persist sensitive data.** Captured HTTP recordings
   include raw SQL with parameter values, request form_dict and
   request headers. v0.7.x redacts known-sensitive keys
   (`password`, `api_key`, `token`, `secret`, `csrf`, `cookie`,
   `authorization`) at render and export time. Custom-named
   sensitive columns may still leak; treat any exported report as
   admin-only.
2. **AI fix sends source code to an LLM.** When the AI fix feature
   is enabled (Optimus Settings ▸ AI Fix), code snippets +
   normalised SQL are POSTed to the configured `ai_base_url`. The
   site operator chooses the endpoint (typically OpenAI / Anthropic
   / a self-hosted Ollama instance). No validation that the URL is
   safe; operators are responsible for endpoint selection.
3. **Redis cache contains HMAC-signed pickles.** Optimus stashes
   pyinstrument trees as HMAC-SHA256-signed pickle blobs in Redis
   (signature derived from `frappe.conf.encryption_key`). A
   Redis-poisoning attacker without the encryption_key cannot
   inject a malicious pickle - signature verification fires on
   read.
4. **Whitelisted API endpoints carry IP-based rate limits.**
   `suggest_fix`, `regenerate_*`, `download_pdf`, `export_session`,
   `retry_analyze` are throttled (5-30 req/min per IP depending
   on cost) to prevent LLM-cost-burn or CPU DoS.
5. **`_resolve_source_path` enforces a bench-boundary check.**
   Analyzer-controlled callsite filenames cannot escape the bench
   directory tree, so a malicious analyzer dict can't be used to
   read arbitrary host files.

## API key handling

The AI provider key is kept out of every log. It is stored in the encrypted
`ai_api_key` Password field of Optimus Settings and decrypted only when a
request is sent. It must be plain printable ASCII: a key with any other
character (a space inside it, a pasted smart quote or no-break space, a
control character) is refused before any request is made, with a message
that names the usual causes (a pasted smart quote, a stray space, a no-break
space, a control character). In Optimus's code it exists only in local
variables named `api_key` or `secret` (names both Frappe's traceback
sanitizer and Sentry's default denylist redact), for a moment in the
`literals` parameter of `redaction.scrub_secrets` (which moves the key into
`secret` before it scrubs anything), and in `ai_fix._ApiKeyAuth`, a
`requests` auth object whose `repr` is masked. That object sets the header
on the HTTP library's own prepared request, whose headers hold the key while
the request is sent; the response keeps that request, and neither one's
`repr` shows its headers. Apart from those, it is never placed in a dict, a
header dict, a request body, an exception message or an exception chain. A
provider's error reply is scrubbed before it is shown, of the key stored in
Optimus Settings and of the key the request was sent with (so an echo is
masked even when the key in Settings was changed while the request ran),
each in its raw and its JSON-escaped form. A 404 message names the request
URL with any credentials in it masked (a `user:password@` typed into a
custom Base URL, or the key), or only "(the configured Base URL)" when the
URL cannot be scrubbed. A request never follows a redirect
(`allow_redirects=False`): the HTTP library drops only a header named
`Authorization` when it follows one to another host, so the `x-api-key`
header Anthropic uses would have been sent on to the redirect target. A 3xx
reply is reported as an unexpected response that names its status and says
to set the Base URL to the address it redirects to.

Every Error Log row the AI code writes goes through
`optimus.ai_fix.log_ai_failure`, which writes an explicit message with no
frame locals and scrubs secrets from it (`optimus.redaction.scrub_secrets`).
It is always called after the failure has been handled, so Frappe's Sentry
hook never receives the frames of the failed request, where the prepared
headers are. Two tests keep it that way:
`optimus/tests/test_ai_log_audit.py` fails if AI code calls
`frappe.log_error` directly or logs inside an `except` block, and
`optimus/tests/test_ai_secret_canary.py` fails if a fake key reaches any
log, traceback, error-tracker payload or response in any of the failure
scenarios it models.

`log_ai_failure` inserts the row at once, in the current transaction. On
MariaDB the Error Log table is MyISAM, so the row is written immediately and
survives a rollback. On Postgres, if that transaction is rolled back later
(a `frappe.throw` in the same request, a background job that fails), a
`frappe.db.after_rollback` callback finds the row gone and queues the same
scrubbed row in Redis, and Frappe's scheduler (every 15 minutes) or the next
`bench migrate` writes it. When a row may be missing (its write failed;
after a rollback, its existence could not be checked or it could not be
queued again; or the rollback callback could not be registered), or a hook
that runs after the insert failed (a broken Error Log notification, say: the
row is then written, and a caller that logs the same error again writes a
second row), one line naming only the error type goes to the `optimus` log
(`logs/optimus.log`): "an AI Error Log row may not have been written or
re-queued, or a hook after the insert failed". It is logged at error level,
the lowest level Frappe's loggers keep on a production site. A row for an
HTTP failure holds the provider, the call site, the status and, when the
reply names one made only of lowercase words (letters joined by `_`, `.`,
`:` or `-`, at most 64 characters), the provider's error code
(`provider_error=`); any other value is left out, and the reply body, which
can echo the prompt, is never logged. That holds when this row cannot be
written and the caller logs the error instead: the caller's row shows the
status, the call site and `provider_error=` in place of the error's message,
which can quote the reply. An unexpected error in the HTTP layer is logged
with its type and plain `file:line:function` frames, without local variables
or its message.

Error Log rows that Frappe itself writes (a server error's snapshot, a
failed background job, any error in developer mode) go through an Error Log
`before_insert` hook, `optimus.error_log_mask`. Frappe runs it for every
Error Log row, before `validate`, the length check and the INSERT, whether
`frappe.log_error` inserts the row at once or Frappe inserts it later from
its deferred-insert queue in Redis (bench migrate right after the patches,
and the scheduler every 15 minutes). It changes only a row from Optimus's AI
code (an `optimus/ai_fix.py` or `frappe_profiler/ai_fix.py` frame in its
error, title or metadata) or one holding the key stored in Optimus Settings
(raw or JSON-escaped). In such a row it masks the key, the key shapes
`scrub_secrets` knows and the bare header value lines of the HTTP library's
frames in `error`, `method` (the title) and `metadata`, and moves a title
longer than its 140-character column in front of the error, as Frappe v16
does. Every other row, another app's included, is stored exactly as it was.
It reads the stored key once per Error Log insert (one SELECT on `__Auth`,
and a decrypt when a key is stored) with Frappe's messages muted, never
caches it, and never raises, except an RQ job timeout, which leaves as a
fresh exception so the job still stops. It fails open: when it cannot read
the key, the row is stored as it was. The one exception is a row it would
mask whose masking fails: its error text is replaced by "Optimus withheld
this error text: it could not be masked. See logs/optimus.log for the
reason.", its title and metadata too when they hold an `ai_fix.py` frame or
the key (a title holding neither is kept, cut to 140 characters). A row
whose AI check itself fails is treated as an AI row. Each failure, and each
use of the fallback below, writes one line to the `optimus` log
(`logs/optimus.log`), naming at most an exception type: the first time that
outcome happens in a process, then every 1000th time, with its count, so a
storm of failing inserts cannot fill the log, which Frappe rotates. The hook
never writes an Error Log row itself.

Its module imports only the standard library at import time. So on an
in-place upgrade (the new code on the filesystem the running processes use),
a process still running the previous release that reads the new hooks
resolves the hook without error; there its import of the rest of Optimus can
fail (the old modules are still loaded), and it then falls back to Frappe
alone: it masks the stored key, raw or JSON-escaped, in `error`, `method`
and `metadata` (a key shorter than 8 characters is not replaced), but not
the other key shapes or the value lines. An image-based or rolling
deployment is different. See the known limitations below for both, and for
when the hook takes effect.

Earlier releases with AI fix suggestions could store the key in plain text
in the Error Log after a failed AI call. See the API key advisory in
`CHANGELOG.md` for the required key rotation and cleanup
(`optimus.maintenance`), and the next section for checking a site at any
time.

## Detecting and cleaning a key leak

1. Revoke the key at the provider now: create a new key there and revoke
   the old one. Backups, replicas, binlogs and bench log files keep the old
   text, and only revoking the key makes those copies harmless.
2. Keep the OLD key in Optimus Settings until the scrub has run and its dry
   run reports the values of step 5: the scrub searches the Error Log for
   the key stored there. Only then enter the new key (step 7).
3. Where to look: Error Log rows titled `optimus *` (for example
   `optimus ai_fix` or `optimus refill_indexes`), and rows Frappe wrote
   itself for a server error or a failed background job whose traceback
   passes through `optimus/ai_fix.py`. A row titled "Optimus: Error Log key
   scrub did not run" means the migrate skipped the scrub or it failed, and
   one titled "Optimus: Error Log key scrub did not finish" means it ran but
   could not process every row, left key-shaped values or could not read the
   stored key; both name the reason (counts or an error type) and the
   command to run.
4. Count what the scrub would change:
   `bench --site <site> execute optimus.maintenance.scrub_error_log_secrets --kwargs "{'dry_run': True}"`.
5. Run it with `'dry_run': False`, then, right after it, the dry run again.
   If you just upgraded, run them after restarting the web server and the
   background workers (on an image-based or rolling deployment, after the
   rollout). The dry run after the real run must report these values:
   `changed` 0, `deleted_docs_changed` 0, `residual` 0, `failed` 0 and
   `key_unreadable` False. Every row the scrub reads is then masked (see the
   known limitations below for which rows it reads). The scrub reads only
   the Error Log and Deleted Document tables: it never reads or changes
   Frappe's deferred-insert queue in Redis, whose records the Error Log hook
   masks when Frappe inserts them. A real run first clears and reloads the
   hooks Frappe caches and reads them back, so the hook reaches every
   process once all of them run this release; its result's `hooks_refreshed`
   is True when the hooks read back hold the Error Log hook.
   `hooks_refreshed` is not one of the values above, and a failed refresh is
   not counted in `failed`. A dry run neither refreshes the cached hooks nor
   reads the deferred-insert queue, so its `hooks_refreshed` is always
   False. If the real run's `hooks_refreshed` is False, or you just upgraded
   and cannot run the scrub, run `bench --site <site> clear-cache` after the
   restart. To confirm that the hooks the site's processes read carry the
   Error Log hook, check that this prints
   `optimus.error_log_mask.mask_error_log` under `before_insert`:
   `bench --site <site> execute frappe.get_hooks --kwargs "{'hook': 'doc_events'}" | grep -o '"Error Log": {[^}]*}'`
   (in developer mode it shows only its own process's hooks). If it does
   not, a process of the previous release still runs or cached the old
   hooks: restart or replace every such process, run
   `bench --site <site> clear-cache`, and check again. Each run of the scrub
   that completes writes one line with its counts to the `optimus` log
   (`logs/optimus.log`). `key_unreadable` is True when a key is stored in
   Optimus Settings but cannot be decrypted (the site's `encryption_key`
   changed, for example on a backup restored onto another site): the scrub
   can then neither search for the key nor mask it by value, and it counts
   one in `failed`. Restore the site's `encryption_key`, or enter the OLD
   key again in Optimus Settings (the scrub searches for the key that
   leaked), then run the scrub again. If Optimus Settings cannot be read for
   another reason, `failed` counts one and `key_unreadable` stays False. On
   MariaDB the scrub locks the Error Log while it scans it (the table is
   MyISAM), one window of 1000 rows per statement, so on a large Error Log
   run it off-peak. Run it with `bench execute` or
   `bench --site <site> console`, never as a background job: it refuses to
   run inside one, because a failed job's log would store the unmasked rows
   it reads.
6. Optional: to delete the Optimus AI rows entirely (they can also hold
   prompt text: source code and SQL with literal values), count them first,
   then delete:
   `bench --site <site> execute optimus.maintenance.purge_ai_error_logs --kwargs "{'dry_run': True}"`,
   then the same command with `'dry_run': False`. It deletes the rows with a
   frame in `optimus/ai_fix.py`, or in `frappe_profiler/ai_fix.py` from
   releases before the app was renamed, and their Deleted Document copies.
   On MariaDB it locks the Error Log while it scans it too, so on a busy
   site run it off-peak.
7. Enter the new key in Optimus Settings.

What the scrub sends to the database: its search sends at most an
8-character fragment of the stored key, never the whole key, and checks the
full key in Python, and its UPDATEs carry masked text. So the database's
query logs record at most that fragment of the stored key.
A ROW-format binlog still records each UPDATE's before-image, the row as it
was, and binlogs, replicas, bench `logs/` files and backups from before the
upgrade (including the backup `bench update` takes when it starts) still
hold the old text. Treat bench log files and binlogs from before the upgrade
like backups: rotating the key is what makes them harmless.

The migrate patch that runs the scrub runs once per site. It never reads or
changes the Error Log queue in Redis: the Error Log hook masks the queued
records when Frappe inserts them, bench migrate right after the patches and
the scheduler every 15 minutes. When it skips or cannot run the scrub, it
clears and reloads the hooks Frappe caches, as a real scrub does first, so
the migrate's own insert of the queue runs the hook (unless a process still
running the previous release caches the old hooks again in between). On
every path it prints one line saying nothing needs doing for the queue (or,
when `optimus.maintenance` cannot be imported, that rows, queued ones
included, may be stored unmasked until it can), then a last line saying to
run the scrub after restarting the web server and the background workers,
since it also refreshes Frappe's cached hooks, or, if you cannot,
`bench --site <site> clear-cache` after the restart; that line first says
the cached hooks were not refreshed during the migrate when the refresh
failed or was not attempted. Rows the old processes insert until the restart
may be stored unmasked, or with only the stored key masked: run steps 4 and
5 again after the restart, which mask them in the table and make the hook
reach every process. Run them by hand after a downgrade to an earlier
release and the upgrade back (the earlier release can write keys again, and
the patch does not run twice), on a site where Optimus was uninstalled and
installed again (a new install marks every patch as done), and on a site
that ran `frappe_profiler` 0.6.x with AI fix suggestions and then installed
Optimus fresh (for the same reason). The scrub's passes that find rows by an
`ai_fix.py` frame and a secret marker also find the rows with a
`frappe_profiler/ai_fix.py` frame. A site that ran an earlier release and
then uninstalled Optimus still holds the rows, and there
`bench execute optimus.maintenance...` fails because the app is not
installed on the site. With the app still on the bench, run the scrub from
`bench --site <site> console` instead:

```python
from optimus.maintenance import scrub_error_log_secrets
scrub_error_log_secrets(dry_run=False)
scrub_error_log_secrets(dry_run=True)  # must report the values of step 5
```

Or install Optimus on the site again and run steps 4 and 5. If no key is
stored on the site any more, the scrub has no stored key to search for; its
passes that find rows by an `ai_fix.py` frame and a secret marker still
run, and `key_unreadable` is False there (no key is stored). The Error Log
hook does not run on such a site: Frappe runs only the hooks of the apps
installed on the site.

## Known limitations

- SQL parameter redaction is **best-effort**; a regex pattern over
  known-sensitive column names catches `WHERE password = '...'`
  shapes but won't catch obscure column names or UPDATE SET
  clauses with sensitive values.
- Optimus User role grants access to any session the user
  recorded. There's no per-recording fine-grained ACL.
- Rate limiting is **IP-based**, not per-user. Multi-user deployments
  behind a single load balancer share the rate-limit bucket;
  per-user buckets are on the v0.8 roadmap.
- Frappe attaches the local variables of the failing code's frames to the
  Error Log row it writes for an error that escapes to it: a server error, a
  background job that fails or times out, and every error in developer mode.
  When a site sends errors to Sentry (`FRAPPE_SENTRY_DSN` set and telemetry
  enabled), every event also carries the local variables of the code on the
  call stack (`attach_stacktrace`). The AI code never holds the API key
  outside the places described under "API key handling", but its frames do
  hold the prompt (source code and normalised SQL), so prompt text can reach
  Sentry when an AI call fails, and the Error Log when an AI call is cut off
  by a job timeout or fails in developer mode. The same frames also hold the
  Base URL, so a custom Base URL typed with credentials in it
  (`user:password@host`, or a key in its path) can reach those places too.
  In the Error Log, the Error Log hook masks the `user:password@` shape and
  the stored key in a row from the AI code, but not the prompt text, nor a
  key in the path that is not the stored key. On stock Frappe v16 (Python
  3.14 with sentry-sdk 1.45.1) Sentry currently sends no frame locals at
  all, so there neither reaches Sentry this way.
- On MariaDB an AI failure row survives any rollback (Error Log is a MyISAM
  table). On Postgres it is still lost when only a savepoint is rolled back,
  when the database connection drops before the commit, or when the COMMIT
  itself fails: no rollback callback runs in those cases, and no line goes to
  the `optimus` log. On a site whose scheduler is off, a row queued after a
  rollback waits in Redis until the next `bench migrate` writes it.
- The Error Log hook takes effect in a process only once that process runs
  this release and reads hooks that carry it. On an in-place upgrade, until
  the web server and background workers restart, the processes still running
  the previous release may insert Error Log rows unmasked (while they read
  the old hooks) or with only the stored key masked (once they read the new
  hooks: the hook's import of the rest of Optimus can fail there, the old
  modules being still loaded, and it then masks the stored key with Frappe
  alone, not the other key shapes or the value lines). Frappe caches every
  app's hooks in Redis ("app_hooks"), and a restart does not clear them: a
  process still running the previous release can cache the old hooks,
  without the Error Log hook, during or after the migrate (after the migrate
  clears the cache, after the patch's own refresh, and after
  `bench update`'s asset build, which flushes the whole Redis cache, every
  site's, after the migrate and before the restart), and every process,
  those started after the restart included, then reads those until the cache
  is cleared again. So the normal case is to run steps 4 and 5 after the
  restart: a real run of the scrub clears and reloads the cached hooks and
  reads them back (`hooks_refreshed`). If you do not run them, or
  `hooks_refreshed` is False, run `bench --site <site> clear-cache` after
  the restart. The scrub run after the restart masks, in the table, the rows
  written while the hook was not fully active.
- In developer mode Frappe keeps the hooks in each process (on v16 a copy
  per process, on v15 loaded again for each request from the modules the
  process has already imported), not in Redis. No process can cache old
  hooks for the others there, but the refresh, `hooks_refreshed` and the
  check in step 5 see only their own process, and a process started before
  the upgrade keeps the old hooks until it restarts: restarting every
  process is the remedy.
- The fail-open behaviour described under "API key handling" holds for an
  in-place upgrade only. On an image-based or rolling deployment (Docker,
  Kubernetes, or blue-green benches sharing one `redis_cache`), the
  processes still running the old image do not have the hook's module. Once
  the new hooks are cached they read them too, and Frappe resolves the hook
  outside any error handling, so every Error Log insert in those processes
  fails, for every app (`frappe.log_error` raises, and a queued record is
  dropped), until they are replaced. Nothing in this release can prevent it,
  since the old code runs there. Stop or replace every old web and worker
  process before running the migrate, then run steps 4 and 5 after the
  rollout.
- Error Log records waiting in Frappe's deferred-insert queue are masked
  only when Frappe inserts them: until then (the scheduler's next run, every
  15 minutes, or the next `bench migrate` when the scheduler is off) they
  sit unmasked in Redis. Bench's default cache Redis (`redis_cache`) saves
  nothing to disk.
- Every Error Log insert on the site reads the stored AI key once (a SELECT
  on `__Auth`, and a decrypt when a key is stored), whichever app writes the
  row: the hook needs the key to tell a row holding it. While Optimus
  profiles a flow, that read appears in the report's per-table and
  per-action query breakdowns, one `__Auth` query per Error Log insert; the
  N+1 and slowest-query findings leave it out, as Optimus's own query.
- If you downgrade to a release without the Error Log hook, clear the site's
  cache after it (`bench migrate` does, and after the restart run
  `bench --site <site> clear-cache`): until the cached hooks are cleared they
  still name the hook's module, which Frappe resolves outside any error
  handling, so every Error Log insert fails (`frappe.log_error` raises, and
  a queued record is dropped).
- If the web server's worker timeout interrupts a provider call (a
  `SystemExit` in the request), that call writes no Error Log row. The HTTP
  layer clears the interrupted frames from the exception before it leaves,
  so the prepared request headers do not travel with it.
- The scrub's `residual` count is masking-complete, not selection-complete.
  It reads only the rows its candidate filters select (rows with an
  `ai_fix.py` frame and a secret marker, rows holding the key stored in
  Optimus Settings, and the Deleted Document copies of both) and reports
  those that still hold a key shape after masking. A row outside that
  selection, for example an older key in a row with no `ai_fix.py` frame and
  no marker, is not counted. Rotating the keys is what makes such a copy
  harmless.
- The scrub searches for the stored key by value only when it has at least
  16 characters (the fragment it sends would otherwise be half the key), and
  masks it by value only when it has at least 8 characters (a shorter
  literal would shred ordinary words). A shorter key is masked only where it
  sits in a header, an API-key field, a `Bearer` token or a URL's
  credentials.

## Cryptographic primitives

- HMAC-SHA256 (Python stdlib `hmac`) for Redis-blob signature.
- `frappe.conf.encryption_key` for the HMAC secret.
- No bespoke crypto.
- No `eval` / `exec` / dynamic imports in production code paths.

## Post-deploy hardening: `optimus_allow_unsigned_pickles`

Sprint 1 introduced HMAC signing of the pyinstrument tree blob in
Redis. To avoid silently breaking analyze for sessions in flight at
deploy time (their blobs predate the signing rollout), the read
path falls back to raw `pickle.loads` on unsigned blobs when
`optimus_allow_unsigned_pickles` is truthy in `site_config.json`.

The default is `true`. **Operators should flip it to `false` after
the deploy has been live longer than the Redis blob TTL (10 minutes)
- at that point every blob in Redis was written by the new signing
code and the fallback only weakens the RCE protection.**

```json
// sites/<site>/site_config.json
{
  "optimus_allow_unsigned_pickles": false
}
```

When the fallback fires, a warning is logged to
`frappe.logger().warning(...)` with the recording UUID and a
pointer to this section. Tail `bench logs` after deploy; once the
warnings stop, flip the flag to false.
