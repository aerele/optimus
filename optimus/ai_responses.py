"""Received final-answer evidence and bounded handling, separate from displayed advice.

Stored inside the finding's llm_fix_json, under its parent session permissions and
deletion lifecycle. Never copied into report views, exports or usage journals.
This is evidence for future recovery, not a validated or applicable patch.
"""

import hashlib
import json

MARKDOWN_CHARS = 16384
VALIDATION_CHARS = 1024 * 1024
RETENTION_BYTES = 4 * 1024 * 1024


def completion_status(reason):
	reason = reason.strip().lower() if isinstance(reason, str) else None
	if reason in ("stop", "end_turn", "stop_sequence"):
		return "complete"
	if reason in ("content_filter", "refusal"):
		return "blocked"
	if reason in ("length", "max_tokens", "model_context_window_exceeded", "aborted", "insufficient_system_resource"):
		return "incomplete"
	return "unknown"


def fingerprint(system, messages):
	# Includes the actual source shown, not only the row's SQL or modification time.
	# Random data fence nonces are deliberately retained: identity is exact, not fuzzy.
	return hashlib.sha256(json.dumps([system, messages], sort_keys=True).encode()).hexdigest()


def retain(text, meta, *, system, messages, provider, send_raw):
	from optimus.ai_prompts import PROMPT_VERSION
	encoded = text.encode("utf-8", errors="surrogatepass")
	limited = len(encoded) > RETENTION_BYTES
	prefix = encoded[:RETENTION_BYTES].decode("utf-8", errors="ignore") if limited else text
	return {
		"text": prefix,
		"text_sha256": hashlib.sha256(encoded).hexdigest(),
		"retention_truncated": limited,
		"prompt_version": PROMPT_VERSION,
		"received_chars": len(text),
		"applied": False,
		"provider": provider["name"], "model": provider["model"],
		"privacy_mode": "raw" if send_raw else "redacted",
		"prompt_fingerprint": fingerprint(system, messages),
		"provider_finish_reason": meta.get("provider_finish_reason"),
		"completion_status": completion_status(meta.get("finish_reason")),
		# Numeric subset of completion usage; never hidden reasoning text.
		"reasoning_tokens": meta.get("reasoning_tokens"),
		"output_budget": meta.get("output_budget"),
		"budget_source": meta.get("budget_source"),
	}


def unavailable_note(status):
	return {
		"blocked": "The provider blocked this response. No suggested code is shown. Review the request and provider policy before deciding whether to retry.",
		"unknown": "The provider did not confirm that this response finished. Its received text is retained privately; no suggested code is shown. Refresh can request a new answer and uses provider tokens.",
		"incomplete": "The response was incomplete. Its received text is retained privately; unfinished code is not shown. Refresh can request a new answer and uses provider tokens.",
		"processing_limited": "The received response exceeded local validation limits. Its text is retained privately within the storage limit; no suggested code is shown. The provider's finish reason is unchanged. Review the retained response before spending tokens on another request.",
	}[status]
