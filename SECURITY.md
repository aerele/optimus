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
