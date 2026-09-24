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
request is sent. Inside the process it exists only in a local variable named
`api_key` (a name both Frappe's traceback sanitizer and Sentry's default
denylist redact) and in `ai_fix._ApiKeyAuth`, a `requests` auth object whose
`repr` is masked. It is never placed in a dict, a header dict, a request
body, an exception message or an exception chain, and a provider's error
reply is scrubbed of it before it is shown.

Every Error Log row the AI code writes goes through
`optimus.ai_fix.log_ai_failure`, which writes an explicit message with no
frame locals and scrubs secrets from it (`optimus.redaction.scrub_secrets`).
It is always called after the failure has been handled, so Frappe's Sentry
hook never receives the frames of the failed request, where the prepared
headers are. Two tests keep it that way: `optimus/tests/test_ai_log_audit.py` fails if AI code calls
`frappe.log_error` directly or logs inside an `except` block, and
`optimus/tests/test_ai_secret_canary.py` fails if a fake key reaches any
log, traceback, error-tracker payload or response on any failure path.

`log_ai_failure` inserts the row at once, in the current transaction. If
that transaction is rolled back later (a `frappe.throw` in the same request,
a background job that fails), a `frappe.db.after_rollback` callback queues
the same scrubbed row in Redis, and Frappe's scheduler (every 15 minutes) or
the next `bench migrate` writes it. If the row cannot be written at all, one
line with the error type goes to the `optimus` log (`logs/optimus.log`). A
row for an HTTP failure holds the provider, the call site, the status and,
when the reply names one, the provider's error code (`provider_error=`),
never the reply body, which can echo the prompt. An unexpected error in the
HTTP layer is logged with its type and plain `file:line:function` frames,
without local variables or its message.

Earlier releases with AI fix suggestions could store the key in plain text
in the Error Log after a failed AI call. See the API key advisory in
`CHANGELOG.md` for the required key rotation and cleanup
(`optimus.maintenance`), and the next section for checking a site at any
time.

## Detecting and cleaning a key leak

1. Where to look: Error Log rows titled `optimus *` (for example
   `optimus ai_fix` or `optimus refill_indexes`), and rows Frappe wrote
   itself for a server error or a failed background job whose traceback
   passes through `optimus/ai_fix.py`. A row titled "Optimus: Error Log key
   scrub did not run" means the migrate skipped or could not finish the
   scrub; it names the reason and the command to run.
2. Count what the scrub would change:
   `bench --site <site> execute optimus.maintenance.scrub_error_log_secrets --kwargs "{'dry_run': True}"`.
   A result of `"changed": 0`, `"deleted_docs_changed": 0`,
   `"residual": 0` and `"failed": 0` means that every row the scrub reads
   is already masked (see the known limitations below for which rows it
   reads). Otherwise run it with `'dry_run': False`, then the dry run again.
   The scrub locks the Error Log while it scans it, one window of 1000 rows
   per statement, so on a large Error Log run it off-peak. It sends only an
   8-character fragment of the stored key to the database, never the key.
3. To delete the Optimus AI rows entirely (they can also hold prompt text:
   source code and SQL with literal values), count them first, then delete:
   `bench --site <site> execute optimus.maintenance.purge_ai_error_logs --kwargs "{'dry_run': True}"`,
   then the same command with `'dry_run': False`.
4. Rotate every key that was ever found (create a new key at the provider
   and revoke the old one): backups and replicas keep the old rows.

The migrate patch that runs the scrub runs once per site. Run step 2 by
hand after a downgrade to an earlier release and the upgrade back (the
earlier release can write keys again, and the patch does not run twice), on
a site that ran an earlier release and then uninstalled Optimus (the rows
stay), and on a site where Optimus was uninstalled and installed again (a
new install marks every patch as done).

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
- An AI failure row is still lost when only a savepoint is rolled back, when
  the database connection drops before the commit, or when the COMMIT itself
  fails: no rollback callback runs in those cases. On a site whose scheduler
  is off, a row queued after a rollback waits in Redis until the next
  `bench migrate` or a real run of the scrub writes it.
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
