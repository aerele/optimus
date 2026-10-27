from types import SimpleNamespace

import pytest

from optimus import journal_schema

pytestmark = pytest.mark.rq


def test_schema_probe_timeout_escapes_fresh_without_framework_locals():
	JobTimeoutException = pytest.importorskip("rq.timeouts", exc_type=ImportError).JobTimeoutException
	original = JobTimeoutException("synthetic timeout")
	def fail(*a):
		raise original
	with pytest.raises(JobTimeoutException) as caught:
		journal_schema.ready(SimpleNamespace(db=SimpleNamespace(table_exists=fail)), {"Run": ()})
	assert caught.value is not original
	assert caught.value.__context__ is None
