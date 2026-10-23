"""Provenance for the exact captured Steps saved by the analysis pipeline."""

import hashlib


def notes_hash(value):
	return (
		hashlib.sha256(value.encode("utf-8")).hexdigest()
		if isinstance(value, str) and value.strip()
		else None
	)


def may_rewrite(doc):
	# Only _persist sets this managed value, after Frappe's save-time sanitizer.
	# Every external save clears it, including an unchanged Desk round-trip;
	# Retry Analyze clears it unless it generates a new captured-actions note.
	return bool(doc.get("auto_notes_sha256") and doc.get("auto_notes_sha256") == notes_hash(doc.get("notes")))
