# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The operator's hook check must work with ERPNext's tuple doc-event keys."""

import json
import re
import subprocess
from pathlib import Path

import pytest

from optimus import hooks


@pytest.mark.parametrize("document", ["CHANGELOG.md", "SECURITY.md"])
def test_documented_hook_check_accepts_tuple_doc_events(document):
	text = (Path(__file__).resolve().parents[2] / document).read_text()
	checks = re.findall(r"bench --site <site> execute frappe\.get_\w+[^`\n]*", text)
	assert checks
	command = 'bench --site <site> execute frappe.get_doc_hooks | grep -o \'"Error Log": {[^}]*}\''
	assert all(check == command for check in checks)
	# get_doc_hooks expands tuple keys with append_hook on both v15 and
	# v16. All handlers below have already been listified by get_hooks.
	merged = {
		("Sales Invoice", "Purchase Invoice"): {"on_submit": ["erpnext.accounts.doctype.process"]},
		"Error Log": {"before_insert": [hooks.doc_events["Error Log"]["before_insert"]]},
	}
	with pytest.raises(TypeError):
		json.dumps(merged)  # the former get_hooks command cannot serialize it
	expanded = {}
	for key, events in merged.items():
		for doctype in key if isinstance(key, tuple) else (key,):
			for event, handlers in events.items():
				expanded.setdefault(doctype, {}).setdefault(event, []).extend(handlers)
	for active in (True, False):
		if not active:
			expanded.pop("Error Log")
		result = subprocess.run(
			["grep", "-o", '"Error Log": {[^}]*}'],
			input=json.dumps(expanded), text=True, capture_output=True, check=False,
		)
		assert result.returncode == (0 if active else 1)
		assert ("optimus.error_log_mask.mask_error_log" in result.stdout) is active


_ROOT = Path(__file__).resolve().parents[2]
_DOCS = ("CHANGELOG.md", "SECURITY.md", "docs/AI-FIXING.md")


def _flat(document: str) -> str:
	"""The document's text with its line wrapping undone."""
	return " ".join((_ROOT / document).read_text().split())


@pytest.mark.parametrize("document", _DOCS)
@pytest.mark.parametrize(
	"statement",
	[
		"307 or 308",  # the only redirects followed, same host and port
		"the key the request was sent with (the only key the provider received",
		"or of the key stored in Optimus Settings when the request carried none",
		"neither sent nor refused",  # a keyless provider and an unsendable key
		"the session's query count and query time leave it out",
	],
)
def test_the_three_documents_agree(document, statement):
	assert statement in _flat(document)


@pytest.mark.parametrize("document", _DOCS)
def test_no_document_still_says_a_redirect_is_never_followed(document):
	text = _flat(document)
	for stale in ("A request never follows a redirect", "no longer follow redirects", "which is never followed"):
		assert stale not in text
