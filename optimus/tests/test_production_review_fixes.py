"""Behavioral regressions from the PR 69 production-readiness review."""

import json
from types import SimpleNamespace

import pytest

from optimus import ai_guardrails as gates
from optimus import ai_privacy, error_log_mask, maintenance, redaction, renderer
from optimus.tests.fixtures.ai_refresh_journal import journal as _journal_fixture  # noqa: F401
from optimus.tests.fixtures.ai_refresh_work import Doc  # noqa: F401
from optimus.tests.fixtures.ai_refresh_work import work as _work_fixture
from optimus.tests.test_error_log_mask import FRAME, _Doc
from optimus.tests.test_error_log_mask import env as _env_fixture

ANSWER = "**Diagnosis**: repeated work.\n**Fix**\n```python\n{}\n```\n**Why it works**: less work.\n**Verify**: re-profile."


@pytest.fixture(name="env")
def masking_env(monkeypatch):
	return _env_fixture.__wrapped__(monkeypatch)


@pytest.mark.parametrize("code,rule", [
	('import builtins\nbuiltins.eval(payload)', 'eval-exec'),
	('import builtins as b\nb.exec(payload)', 'eval-exec'),
	('from builtins import eval as evaluate\nevaluate(payload)', 'eval-exec'),
	('import os\nos.execvp("x", [])', 'shell-exec'),
	('from os import execve as start\nstart("x", [], {})', 'shell-exec'),
	('import os\nos.posix_spawn("x", [], {})', 'shell-exec'),
	('import os\nos.spawnv(0, "x", [])', 'shell-exec'),
	('import marshal\nmarshal.load(stream)', 'unsafe-deserialize'),
	('import cPickle\ncPickle.load(stream)', 'unsafe-deserialize'),
	('import dill as d\nd.load(stream)', 'unsafe-deserialize'),
	('import yaml\nyaml.load(data, Loader=yaml.UnsafeLoader)', 'unsafe-deserialize'),
	('frappe.db.sql("CREATE INDEX ix_name ON `tabUser` (name)")', 'raw-ddl'),
	('ev = eval\nev(payload)', 'eval-exec'),
	('import os\nlist(map(os.system, cmds))', 'shell-exec'),
	('import functools, subprocess\nfunctools.partial(subprocess.run, cmd)()', 'shell-exec'),
	('import builtins\ngetattr(builtins, "exec")(code)', 'unverified-safety'),
	('__builtins__.eval(x)', 'eval-exec'),
	('__builtins__["eval"](x)', 'unverified-safety'),
	('import yaml\nyaml.load_all(s, Loader=yaml.UnsafeLoader)', 'unsafe-deserialize'),
	('import yaml\nyaml.unsafe_load_all(s)', 'unsafe-deserialize'),
	('import pickle\npickle.Unpickler(f).load()', 'unsafe-deserialize'),
	('import dill\ndill.Unpickler(f).load()', 'unsafe-deserialize'),
	('import yaml\nyaml.SafeLoader = yaml.UnsafeLoader\nyaml.load(s, Loader=yaml.SafeLoader)', 'unverified-safety'),
	('list(map(os.system, cmds))', 'shell-exec'),
	('functools.partial(subprocess.run, cmd)()', 'shell-exec'),
	('getattr(os, "system")(cmd)', 'unverified-safety'),
	('from os import *\nsystem(cmd)', 'unverified-safety'),
	('from pickle import *\nloads(data)', 'unverified-safety'),
	('from subprocess import *\nrun(cmd)', 'unverified-safety'),
])
def test_generation_and_saved_policy_block_known_sinks(code, rule):
	text = ANSWER.format(code)
	assert rule in {v.code for v in gates.verify_fix(text, source_lines=[])}
	assert rule in {v.code for v in gates.stored_policy_violations(text)}


@pytest.mark.parametrize('identifier', ['private_marker', '`private_marker`', '"private_marker"'])
def test_raw_sql_applies_current_sensitive_columns(monkeypatch, identifier):
	monkeypatch.setattr(ai_privacy, '_fresh_sensitive_columns', lambda: 'private_marker')
	query = f"SELECT name FROM tabCustomer WHERE {identifier}='CANARY_8123' AND enabled=1"
	text = ai_privacy.query_text(query, send_raw=True)
	assert 'CANARY_8123' not in text and 'enabled=1' in text


@pytest.mark.parametrize('boundary', ['settings', 'redactor'])
def test_raw_sql_omitted_on_redaction_failure(monkeypatch, boundary):
	def fail(*a, **kw):
		raise ValueError('synthetic failure')
	monkeypatch.setattr(ai_privacy, '_fresh_sensitive_columns', lambda: '')
	monkeypatch.setattr('optimus.ai_privacy._fresh_sensitive_columns' if boundary == 'settings' else 'optimus.redaction._sql_literal_regex', fail)
	assert ai_privacy.query_text("SELECT name FROM tabUser WHERE password='CANARY_8123'", send_raw=True) == ''


@pytest.mark.parametrize('text', ['Authorization: CANARY_ROTATED_9823', "{'Authorization': 'CANARY_ROTATED_9823'}", 'Proxy-Authorization: CANARY_ROTATED_9823'])
def test_schemeless_authorization_masked(text):
	assert 'CANARY_ROTATED_9823' not in redaction.scrub_secrets(text)


@pytest.mark.parametrize('length', [2048, 2049, 3000, 65536])
def test_oversized_invalid_header_masked(length):
	text = "Invalid header value b'" + 'x' * length + "'"
	assert 'x' * 16 not in redaction.scrub_secrets(text)


def test_failed_ai_scrub_withholds_metadata_and_title(env, monkeypatch):
	def fail(*a, **kw):
		raise ValueError('synthetic failure')
	monkeypatch.setattr(maintenance, '_mask', fail)
	doc = _Doc(error=FRAME, method='CANARY_TITLE_9812', metadata=json.dumps({'Authorization': 'CANARY_8123'}))
	error_log_mask.mask_error_log(doc)
	assert doc.error == error_log_mask.WITHHELD
	assert doc.method == error_log_mask.WITHHELD_TITLE
	assert doc.metadata == error_log_mask.WITHHELD


def test_saved_ddl_is_withheld_in_report():
	from optimus.tests.test_finding_card_ai_fix import _doc, _finding
	page = renderer.render_raw(_doc([_finding(llm_fix=dict(suggestion=ANSWER.format('frappe.db.sql("CREATE INDEX ix_name ON `tabUser` (name)")'), prompt_version=4))]), recordings=[])
	assert 'CREATE INDEX ix_name' not in page
	assert 'Code withheld because this saved suggestion' in page


def test_valueless_class_code_markup():
	from optimus.ai_code_html import rendered_code
	assert rendered_code('<pre><code><span class>print(1)</span></code></pre>') == ['print(1)']


def test_malformed_preparation_is_journaled_without_starving_valid_item(work, monkeypatch):
	jobs, store = work.jobs, work.journal.mod
	run = work.journal.run
	run['cap'] = 1
	monkeypatch.setattr(work.journal.db, 'rollback', lambda: None, raising=False)
	rows = {f'bad-{n}': Doc(work.row, name=f'bad-{n}') for n in range(3)}
	rows['good'] = Doc(work.row, name='good')
	work.journal.db.rows['Optimus Finding'] = rows
	load = jobs.frappe.get_doc
	def get_doc(*args):
		doc = load(*args)
		doc['findings'] = list(rows.values())
		return doc
	monkeypatch.setattr(jobs.frappe, 'get_doc', get_doc)
	build = jobs._build_payload
	def prepare(row, *a, **kw):
		if row.name.startswith('bad'):
			raise AttributeError('malformed callsite')
		return build(row, *a, **kw)
	monkeypatch.setattr(jobs, '_build_payload', prepare)
	monkeypatch.setattr(jobs.ai_fix, 'log_ai_failure', lambda *a, **kw: None)
	for _ in range(3):
		assert jobs._next_item(run, {}) is jobs._PREPARATION_SKIPPED
	item = jobs._next_item(run, {})
	assert item.target == 'good'
	attempts = list(work.journal.db.rows[store.ATTEMPT].values())
	assert len(attempts) == 3 and not work.sends
	assert all(a['state'] == 'skipped' and a['settled'] and a['send_certainty'] == 'not_sent' and a['tokens_reported'] == 0 and a['usage_complete'] for a in attempts)
	assert jobs._attempts(run)[2] == 0
	latest = work.journal.db.rows[store.RUN][run['name']]
	assert not latest.get('breaker_kind') and not latest.get('active_attempt')


def test_server_script_representative_callsite_renders():
	from optimus.tests.test_finding_card_ai_fix import _doc, _finding
	query = 'SELECT code FROM `tabExample`'
	finding = _finding(finding_type='Full Table Scan')
	finding.technical_detail_json = json.dumps({'normalized_query': query, 'table': 'tabExample'})
	recording = {'uuid': 'review-recording', 'calls': [{'query': query, 'normalized_query': query, 'duration': 1,
		'stack': [{'filename': '<serverscript>: review_probe', 'lineno': 1, 'function': 'execute'}]}]}
	page = renderer.render_raw(_doc([finding]), recordings=[recording])
	assert 'review_probe' in page and "('server_script'" not in page


def test_where_filter_precedes_sort_even_with_partial_parser():
	from optimus.renderer import index_recipes as ir
	from optimus.tests.test_index_recipes import F, _ev, _explain, _lookup
	query = 'SELECT name FROM `tabExample` WHERE customer=? ORDER BY customer'
	ev = _ev('Example', fields={'customer': F('Data')})
	def partial(sql):
		return {'tables': ['tabExample'], 'verb': 'SELECT', 'index_cols': {'tabExample': [('ORDER BY', 'customer')]}}
	advice = ir.advise_finding(_explain('Filesort', query, table='tabExample'), evidence_lookup=_lookup(ev), parser=partial)
	assert advice.route != ir.ROUTE_NO_CODE


@pytest.mark.parametrize('predicate', ['LOWER(e.code)=o.code', 'e.code=o.code OR e.other=o.other', 'e.code + 1=o.code'])
def test_join_expression_does_not_produce_plain_index(predicate):
	from optimus.renderer import index_recipes as ir
	from optimus.tests.test_index_recipes import F, _ev, _explain, _lookup
	query = 'SELECT e.code FROM tabExample e JOIN tabOther o ON ' + predicate
	ev = _ev('Example', fields={'code': F('Data'), 'other': F('Data')})
	advice = ir.advise_finding(_explain('Full Table Scan', query, table='tabExample'), evidence_lookup=_lookup(ev))
	assert advice is None or advice.route == ir.ROUTE_NO_CODE


def test_mariadb_usable_prefix_is_coverage_without_uniqueness(monkeypatch):
	import frappe

	from optimus.dbdialect.mariadb import MariaDBDialect
	from optimus.renderer.index_evidence import IndexEvidence
	from optimus.tests.test_index_recipes import _ev
	rows = [dict(Key_name='idx_customer_status', Seq_in_index=n, Column_name=col, Non_unique=0,
		Index_type='BTREE', Ignored='NO', Sub_part=prefix) for n, col, prefix in [(1, 'customer', None), (2, 'status', 5)]]
	monkeypatch.setattr(frappe, 'db', SimpleNamespace(sql=lambda *a, **kw: rows))
	index = MariaDBDialect().existing_indexes('tabExample')[0]
	assert index.leftmost == 'customer' and not index.eligible and index.usable_prefix == ('customer',)
	from dataclasses import replace
	ev = replace(_ev('Example'), indexes=(IndexEvidence(index.name, tuple(index.columns), index.unique, index.eligible, index.reason, index.usable_prefix),))
	usable = ev.usable_indexes[0]
	assert usable.columns == ('customer',) and not usable.unique


@pytest.mark.parametrize('backend', ['mariadb', 'postgres'])
def test_raw_sql_reads_current_committed_rules_and_closes_connection(monkeypatch, backend):
	import frappe
	current, reads, closed = ['private_marker'], [], []
	class Cursor:
		def execute(self, query, values):
			reads.append((query, values))
		def fetchone(self):
			return (current[0],)
		def close(self):
			closed.append('cursor')
	class Connection:
		def cursor(self):
			return Cursor()
		def close(self):
			closed.append('connection')
	monkeypatch.setattr(frappe, 'db', SimpleNamespace(db_type=backend, db_schema='public', get_connection=Connection))
	query = "SELECT name FROM tabCustomer WHERE private_marker='CANARY_8123'"
	assert 'CANARY_8123' not in ai_privacy.query_text(query, send_raw=True)
	current[0] = 'another_column'
	assert 'CANARY_8123' in ai_privacy.query_text(query, send_raw=True)
	assert closed == ['cursor', 'connection'] * 2
	assert sum(values == ('Optimus Settings', 'sensitive_sql_columns') for _query, values in reads) == 2


@pytest.mark.parametrize('kind', ['stop', 'database', 'interrupt', 'permission'])
def test_preparation_failure_preserves_control_and_infrastructure_errors(work, monkeypatch, kind):
	jobs = work.jobs
	monkeypatch.setattr(work.journal.db, 'rollback', lambda: None, raising=False)
	DatabaseError = type('DatabaseError', (Exception,), {'__module__': 'pymysql.err'})
	def prepare():
		if kind == 'permission':
			work.can_read = False
			raise AttributeError('invalid source')
		raise {'stop': jobs.StopRefresh('disabled'), 'database': DatabaseError(), 'interrupt': SystemExit()}[kind]
	expected = jobs.StopRefresh if kind in {'stop', 'permission'} else DatabaseError if kind == 'database' else SystemExit
	with pytest.raises(expected):
		jobs._prepare_item(work.journal.run, kind='fix', target=work.row.name, prepare=prepare)
	assert not work.journal.db.rows.get(work.journal.mod.ATTEMPT)
	assert not work.sends


@pytest.mark.parametrize('code', ['object.eval(value)', 'def eval(value):\n    return value\neval(1)', 'import yaml\nyaml.safe_load(data)', 'import yaml\nyaml.load(data, Loader=yaml.SafeLoader)', 'import yaml\nyaml.load_all(data, Loader=yaml.SafeLoader)', 'os.path.exists(p)', 'yaml.safe_load(s)', 'yaml.load(s, Loader=yaml.SafeLoader)', 'def f(os):\n    os.system(cmd)'])
def test_unrelated_eval_and_safe_yaml_remain_available(code):
	assert not gates.strips_code(gates.verify_fix(ANSWER.format(code), source_lines=[]))


@pytest.mark.parametrize('siblings,disable_fails', [(0, True), (1, True), (0, False)])
def test_capture_teardown_flushes_only_after_safe_detachment(monkeypatch, siblings, disable_fails):
	from optimus.line_profile import capture, hooks
	seen = []
	def disable():
		seen.append('disable')
		if disable_fails:
			raise RuntimeError('synthetic disable failure')
	profiler = SimpleNamespace(disable_by_count=disable, disable=lambda: (_ for _ in ()).throw(RuntimeError('fallback failed')))
	monkeypatch.setattr(hooks, 'capture', capture)
	monkeypatch.setattr(hooks, 'frappe', SimpleNamespace(local=SimpleNamespace(_lp_watchdog=None)))
	monkeypatch.setattr(capture, 'sys', SimpleNamespace(monitoring=SimpleNamespace(PROFILER_ID=2, get_tool=lambda pid: 'line_profiler' if siblings else None)))
	monkeypatch.setattr(capture, 'decr_active_profilers', lambda: seen.append('decrement') or siblings)
	monkeypatch.setattr(capture, 'release_monitoring_tool', lambda: seen.append('release'))
	monkeypatch.setattr(capture, 'serialize_stats', lambda p: seen.append('serialize') or [])
	monkeypatch.setattr(capture, 'flush_samples', lambda *a: seen.append('flush'))
	monkeypatch.setattr(capture, 'mark_capture_incomplete', lambda *a: seen.append('incomplete'))
	hooks._finish_capture(profiler, 'fake-run')
	if siblings and disable_fails:
		assert 'incomplete' in seen and 'flush' not in seen and 'release' not in seen
	else:
		assert seen.index('release') < seen.index('serialize') < seen.index('flush')


@pytest.mark.parametrize('protected', [False, True])
def test_large_unrelated_file_alias_set_does_not_block_upload(monkeypatch, protected):
	from optimus import managed
	from optimus.tests.fixtures.ai_refresh_journal import Database
	db = Database()
	db.rows['File'] = {str(n): {'name': str(n), 'attached_to_doctype': 'Issue', 'attached_to_field': 'attachment',
		'content_hash': 'same-bytes', 'file_url': '/private/files/example.txt'} for n in range(1001)}
	if protected:
		db.rows['File']['protected'] = {'name': 'protected', 'attached_to_doctype': 'Optimus Session',
			'attached_to_field': 'raw_report_file', 'content_hash': 'same-bytes'}
	def deny(message, error):
		raise error(message)
	monkeypatch.setattr(managed, 'frappe', SimpleNamespace(get_all=db.get_all, throw=deny, ValidationError=ValueError))
	doc = Doc(content_hash='same-bytes', file_url='/private/files/another.txt', attached_to_doctype='Issue', attached_to_field='attachment')
	doc.is_new = lambda: True
	if protected:
		with pytest.raises(ValueError, match='managed profiling artifact'):
			managed.protect_file(doc)
	else:
		managed.protect_file(doc)


def test_analyze_retry_preserves_cancelled_call_reconciliation_after_commit(work, monkeypatch):
	jobs, store = work.jobs, work.journal.mod
	with work.journal.db.transaction():
		attempt = store.begin_attempt(work.journal.run['name'], docname=work.parent['name'],
			worker_token='worker', kind='fix', target=work.row.name, input_hash='fake', now=20)
	work.parent['status'] = 'Failed'
	logged = []
	def ending(run, docname):
		assert not work.journal.db.active
		logged.append((run, docname))
	monkeypatch.setattr(jobs, '_note_ending', ending)
	assert jobs.prepare_analyze_retry(work.parent['name'], work.parent['session_uuid'], requested_by='fake-owner')
	assert len(logged) == 1 and logged[0][1] == work.parent['name']
	assert logged[0][0]['uncertain_attempt'] == attempt['name']
	assert work.journal.db.rows[store.ATTEMPT][attempt['name']]['state'] == 'uncertain'


def test_pre_monitoring_capture_uses_disable_fallback_before_flushing(monkeypatch):
	from optimus.line_profile import capture, hooks
	seen = []
	def broken_count():
		raise RuntimeError('count teardown failed')
	profiler = SimpleNamespace(disable_by_count=broken_count, disable=lambda: seen.append('disable'))
	monkeypatch.setattr(hooks, 'capture', capture)
	monkeypatch.setattr(hooks, 'frappe', SimpleNamespace(local=SimpleNamespace(_lp_watchdog=None)))
	monkeypatch.setattr(capture, 'sys', SimpleNamespace())
	monkeypatch.setattr(capture, 'decr_active_profilers', lambda: 0)
	monkeypatch.setattr(capture, 'release_monitoring_tool', lambda: None)
	monkeypatch.setattr(capture, 'serialize_stats', lambda p: seen.append('serialize') or [])
	monkeypatch.setattr(capture, 'flush_samples', lambda *a: seen.append('flush'))
	monkeypatch.setattr(capture, 'mark_capture_incomplete', lambda *a: seen.append('incomplete'))
	assert isinstance(hooks._finish_capture(profiler, 'fake-run'), RuntimeError)
	assert seen == ['disable', 'serialize', 'flush']


def test_saved_inline_ddl_is_withheld():
	text = '**Diagnosis**: slow read.\n**Fix**\nRun `CREATE INDEX ix ON tabUser (name)`.\n**Why it works**: faster.\n**Verify**: profile.'
	assert 'raw-ddl' in {v.code for v in gates.stored_policy_violations(text)}


def test_real_malformed_callsite_is_skipped_without_provider_send(work, monkeypatch):
	from optimus import analyze
	work.row['technical_detail_json'] = '{"callsite":{"function":7}}'
	monkeypatch.setattr(work.jobs, '_build_payload', lambda row, cache, grounding, **kw: analyze.ai_payload_for_finding(row, cache))
	monkeypatch.setattr(work.journal.db, 'rollback', lambda: None, raising=False)
	monkeypatch.setattr(work.jobs.ai_fix, 'log_ai_failure', lambda *a, **kw: None)
	assert work.jobs._next_item(work.journal.run, {}) is work.jobs._PREPARATION_SKIPPED
	assert work.jobs._next_item(work.journal.run, {}) is None
	assert not work.sends


@pytest.mark.parametrize('read_fails', [False, True])
def test_sql_rule_memo_is_bounded_to_one_payload_build(monkeypatch, read_fails):
	reads = []
	def current():
		reads.append(True)
		if read_fails:
			raise RuntimeError('settings unavailable')
		return 'private_marker'
	monkeypatch.setattr(ai_privacy, '_fresh_sensitive_columns', current)
	query = "SELECT name FROM tabCustomer WHERE private_marker='CANARY_1234'"
	for _ in range(2):
		with ai_privacy.sql_redaction_scope():
			for _ in range(1000):
				assert 'CANARY_1234' not in ai_privacy.query_text(query, send_raw=True)
	assert len(reads) == 2


@pytest.mark.parametrize('authorized', [False, True])
def test_representative_server_script_snippet_respects_access(monkeypatch, authorized):
	from optimus.renderer import source
	from optimus.tests.test_finding_card_ai_fix import _doc, _finding
	from optimus.tests.test_server_script_render import fake_frappe_db
	state = fake_frappe_db.__wrapped__(monkeypatch)
	state['rows'] = [{'name': 'Review Probe', 'script': 'SCRIPT_BODY_CANARY = 1\n'}]
	monkeypatch.setattr(source, '_may_read_server_script', lambda *a: authorized)
	query = 'SELECT code FROM `tabExample`'
	finding = _finding(finding_type='Full Table Scan')
	finding.technical_detail_json = json.dumps({'normalized_query': query, 'table': 'tabExample'})
	recording = {'uuid': 'review', 'calls': [{'query': query, 'normalized_query': query, 'duration': 1,
		'stack': [{'filename': '<serverscript>: review_probe', 'lineno': 1, 'function': 'execute'}]}]}
	page = renderer.render_raw(_doc([finding]), recordings=[recording])
	assert ('SCRIPT_BODY_CANARY' in page) is authorized


def test_retry_analyze_continues_when_breadcrumb_commit_fails(work, monkeypatch):
	jobs = work.jobs
	work.parent['status'] = 'Failed'
	work.journal.db.rows['Optimus Phase Two Run'] = {'child': {
		'name': 'child', 'parent': work.parent['name'], 'run_uuid': 'phase2', 'recording_user': 'fake-owner', 'status': 'Recording'}}
	seen = []
	from optimus.line_profile import jobs as phase2_jobs
	monkeypatch.setattr(phase2_jobs, 'stop_captures', lambda captures: seen.append('stopped'))
	def failed_commit(*a):
		seen.append('log-failed')
		raise RuntimeError('synthetic log commit failure')
	monkeypatch.setattr(jobs, '_note_ending', failed_commit)
	assert jobs.prepare_analyze_retry(work.parent['name'], work.parent['session_uuid'], requested_by='fake-owner')
	assert seen == ['stopped', 'log-failed']


def test_real_payload_deduplicates_thousand_queries_before_sensitive_rule_read(monkeypatch):
	from optimus import analyze
	from optimus.tests.test_finding_card_ai_fix import _finding
	reads = []
	monkeypatch.setattr(ai_privacy, 'raw_values_enabled', lambda: True)
	monkeypatch.setattr(ai_privacy, '_fresh_sensitive_columns', lambda: reads.append(True) or 'private_marker')
	query = "SELECT name FROM tabCustomer WHERE private_marker='CANARY_1234'"
	recordings = {'rec': {'calls': [{'query': query, 'duration': 1} for _ in range(1000)]}}
	payload = analyze.ai_payload_for_finding(_finding(), {}, recordings_by_uuid=recordings, actions_by_idx={0: {'recording_uuid': 'rec'}})
	assert len(reads) == 1
	assert len(payload['technical_detail']['example_queries']) == 1
	assert 'CANARY_1234' not in json.dumps(payload)
