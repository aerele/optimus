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
that names the usual causes (a pasted smart quote, a stray space, a
no-break space, a control character). In Optimus's code it exists only in
local variables named `api_key` or `secret` (names both Frappe's traceback
sanitizer and Sentry's default denylist redact) and in
`ai_fix._ApiKeyAuth`, a `requests` auth object whose `repr` is masked. That
object sets the header on the HTTP library's own prepared request, whose
headers hold the key while the request is sent; the response keeps that
request, and neither one's `repr` shows its headers. Apart from those, it
is never placed in a dict, a header dict, a request body, an exception
message or an exception chain. A provider's error reply is
scrubbed before it is shown, of the key stored in Optimus Settings and of
the key the request was sent with (so an echo is masked even when the key
in Settings was changed while the request ran), each in its raw and its
JSON-escaped form. A 404 message names the request URL with any
credentials in it masked (a `user:password@` typed into a custom Base URL,
or the key).

Every Error Log row the AI code writes goes through
`optimus.ai_fix.log_ai_failure`, which writes an explicit message with no
frame locals and scrubs secrets from it (`optimus.redaction.scrub_secrets`).
It is always called after the failure has been handled, so Frappe's Sentry
hook never receives the frames of the failed request, where the prepared
headers are. Two tests keep it that way: `optimus/tests/test_ai_log_audit.py` fails if AI code calls
`frappe.log_error` directly or logs inside an `except` block, and
`optimus/tests/test_ai_secret_canary.py` fails if a fake key reaches any
log, traceback, error-tracker payload or response on any failure path.

`log_ai_failure` inserts the row at once, in the current transaction. On
MariaDB the Error Log table is MyISAM, so the row is written immediately and
survives a rollback. On Postgres, if that transaction is rolled back later (a
`frappe.throw` in the same request, a background job that fails), a
`frappe.db.after_rollback` callback finds the row gone and queues the same
scrubbed row in Redis, and Frappe's scheduler (every 15 minutes) or the next
`bench migrate` writes it. When a row may be missing (its write failed;
after a rollback, its existence could not be checked or it could not be
queued again; or the rollback callback could not be registered), or a hook
that runs after the insert failed (a broken Error Log notification, say:
the row is then written, and a caller that logs the same error again writes
a second row), one line naming only the error type goes to the `optimus`
log (`logs/optimus.log`): "an AI Error Log row may not have been written or
re-queued, or a hook after the insert failed". It is logged at error level,
the lowest level Frappe's loggers keep on a production site. A row for an HTTP failure holds the provider, the call site, the
status and, when the reply names one made only of lowercase words (letters
joined by `_`, `.`, `:` or `-`, at most 64 characters), the provider's
error code (`provider_error=`); any other value is left out, and the reply
body, which can echo the prompt, is never logged. That holds when this row
cannot be written and the caller logs the error instead: the caller's row
shows the status, the call site and `provider_error=` in place of the
error's message, which can quote the reply. An unexpected error in the HTTP
layer is logged with its type and plain `file:line:function` frames,
without local variables or its message.

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
   could not process every row or queued entry, or left key-shaped values;
   both name the reason (counts or an error type) and the command to run.
4. Count what the scrub would change:
   `bench --site <site> execute optimus.maintenance.scrub_error_log_secrets --kwargs "{'dry_run': True}"`.
5. Run it with `'dry_run': False`, then, right after it, the dry run
   again. The dry run after the real run must report these values:
   `changed` 0, `deleted_docs_changed` 0, `residual` 0 and `failed` 0.
   Every row the scrub reads is then masked (see the known limitations
   below for which rows it reads). `queued` counts the Error Log entries
   still waiting in Frappe's deferred-insert queue in Redis. A dry run only
   counts them. A real run inserts the entries that were waiting when it
   started, each masked first; entries past the first 10,000, or left when
   the database stopped answering, it masks in Redis and leaves queued.
   `queued` also counts Frappe's own new server-error snapshots, and every
   error in developer mode: once the processes have restarted on this
   release these come from the fixed code and are harmless. So a small
   `queued` that changes between runs is new traffic; if it stays large,
   run the real scrub again. The scrub locks the Error Log while it scans
   it, one window of 1000 rows per statement, so on a large Error Log run
   it off-peak. Run it with `bench execute` or
   `bench --site <site> console`, never as a background job: it refuses to
   run inside one, because a failed job's log would store the unmasked rows
   it reads.
6. Optional: to delete the Optimus AI rows entirely (they can also hold
   prompt text: source code and SQL with literal values), count them first,
   then delete:
   `bench --site <site> execute optimus.maintenance.purge_ai_error_logs --kwargs "{'dry_run': True}"`,
   then the same command with `'dry_run': False`. It deletes the rows with
   a frame in `optimus/ai_fix.py`, or in `frappe_profiler/ai_fix.py` from
   releases before the app was renamed, and their Deleted Document copies.
   It locks the Error Log while it scans it too, so on a busy site run it
   off-peak.
7. Enter the new key in Optimus Settings.

What the scrub sends to the database: its search sends at most an
8-character fragment of the stored key, never the whole key, and checks the
full key in Python; the queued Error Log rows it inserts are masked before
the INSERT; and its UPDATEs carry masked text. So the database's query logs
record at most that fragment of the stored key. A ROW-format binlog still
records each UPDATE's before-image, the row as it was, and binlogs,
replicas, bench `logs/` files and backups from before the upgrade
(including the backup `bench update` takes when it starts) still hold the
old text. Treat bench log files and binlogs from before the upgrade like
backups: rotating the key is what makes them harmless.

The migrate patch that runs the scrub runs once per site. bench migrate
inserts whatever is left in the deferred-insert queue right after the
patches, so the scrub first masks in Redis the entries it leaves there of
those that were waiting when it started, unless the flush stopped early (it
then reports failed entries). Run steps 4 and 5 again after the
restart, to mask any rows that reached the table another way. Run them by
hand after a downgrade to an earlier release and the upgrade back (the
earlier release can write keys again, and the patch does not run twice),
on a site where Optimus was uninstalled and installed again (a new install
marks every patch as done), and on a site that ran `frappe_profiler` 0.6.x
with AI fix suggestions and then installed Optimus fresh (for the same
reason). The scrub's passes that find rows by an `ai_fix.py` frame and a
secret marker also find the rows with a `frappe_profiler/ai_fix.py` frame.
A site that ran an earlier release and then uninstalled Optimus still
holds the rows, and there `bench execute optimus.maintenance...` fails
because the app is not installed on the site. With the app still on the
bench, run the scrub from `bench --site <site> console` instead:

```python
from optimus.maintenance import scrub_error_log_secrets
scrub_error_log_secrets(dry_run=False)
scrub_error_log_secrets(dry_run=True)  # must report the values of step 5
```

Or install Optimus on the site again and run steps 4 and 5. If no key is
stored on the site any more, the scrub has no stored key to search for; its
passes that find rows by an `ai_fix.py` frame and a secret marker still
run.

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
  outside the two places described under "API key handling", but its frames
  do hold the prompt (source code and normalised SQL), so prompt text can
  reach Sentry when an AI call fails, and the Error Log when an AI call is
  cut off by a job timeout or fails in developer mode. On stock Frappe v16
  (Python 3.14 with sentry-sdk 1.45.1) Sentry currently sends no frame
  locals at all, so there the prompt does not reach Sentry this way.
- On MariaDB an AI failure row survives any rollback (Error Log is a MyISAM
  table). On Postgres it is still lost when only a savepoint is rolled back,
  when the database connection drops before the commit, or when the COMMIT
  itself fails: no rollback callback runs in those cases, and no line goes
  to the `optimus` log. On a site whose scheduler is off, a row queued after
  a rollback waits in Redis until the next `bench migrate` or a real run of
  the scrub writes it.
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
