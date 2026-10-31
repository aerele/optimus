"""Runtime behavior at the public API error boundary."""
import json

import pytest

from optimus import api


def test_batch_retry_hides_exception_text_and_continues(monkeypatch):
	monkeypatch.setattr(api, '_require_profiler_user', lambda: 'owner')
	seen, logs = [], []
	def retry(run):
		seen.append(run)
		if run == 'first':
			raise RuntimeError('BATCH-PRIVATE-CREDENTIAL-CANARY')
		return {'run_uuid': run, 'status': 'Analyzing'}
	monkeypatch.setattr(api, 'retry_phase2_analyze', retry)
	monkeypatch.setattr(api.safe_call, 'log_error_line', logs.append)
	result = api.retry_phase2_analyzes_batch(['first', 'second'])
	assert seen == ['first', 'second']
	assert result['results'][0]['status'] == 'Failed'
	assert 'could not be confirmed' in result['results'][0]['error']
	assert result['results'][1]['status'] == 'Analyzing'
	assert 'BATCH-PRIVATE-CREDENTIAL-CANARY' not in json.dumps(result) + ''.join(logs)
	assert logs and 'RuntimeError' in logs[0]


def test_batch_retry_rate_refusal_stops_before_later_runs(monkeypatch):
	class Limited(Exception):
		pass
	monkeypatch.setattr(api.frappe, 'RateLimitExceededError', Limited, raising=False)
	monkeypatch.setattr(api, '_require_profiler_user', lambda: 'owner')
	seen = []
	def retry(run):
		seen.append(run)
		raise Limited('wait')
	monkeypatch.setattr(api, 'retry_phase2_analyze', retry)
	with pytest.raises(Limited):
		api.retry_phase2_analyzes_batch(['first', 'second'])
	assert seen == ['first']
