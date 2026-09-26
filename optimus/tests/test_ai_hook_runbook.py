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
