# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Generated recording-file insertion keeps the narrow request bypass.

The transactional report writer has separate tests. This legacy helper now
serves recording bundles and must restore request state on every outcome.
"""


def test_save_recording_file_failure_is_logged_safely_and_remains_optional(monkeypatch):
	import sys
	from types import SimpleNamespace

	from optimus import ai_fix, analyze

	request, logs, seen = object(), [], []
	local = SimpleNamespace(request=request)
	def insert(**kw):
		seen.append(local.request)
		raise RuntimeError("fake insert failure")
	monkeypatch.setattr(analyze, "frappe", SimpleNamespace(local=local,
		get_doc=lambda *a: SimpleNamespace(insert=insert)))
	monkeypatch.setattr(ai_fix, "log_ai_failure", lambda *a, **kw: logs.append((sys.exc_info()[0], kw)))
	assert analyze._save_report_file(docname="fake-doc", filename="fake.json.gz",
		attached_to_field="recordings_file", content=b"fake") is None
	assert seen == [None]
	assert local.request is request
	assert logs == [(None, {"reason": "file_write_failed"})]
