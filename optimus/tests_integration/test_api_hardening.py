"""Real framework method enforcement and bounded Redis ingress on disposable sites."""
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from optimus import api, redis_keys, redis_schema


class TestApiHardening(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user('Administrator')

	def test_pdf_framework_guard_rejects_get_and_accepts_post(self):
		from frappe.handler import is_valid_http_method
		missing = object()
		old_request = getattr(frappe.local, 'request', missing)
		old_job = getattr(frappe.local, 'job', missing)
		old_safe = frappe.flags.in_safe_exec
		try:
			if old_job is not missing:
				del frappe.local.job
			frappe.flags.in_safe_exec = False
			frappe.local.request = SimpleNamespace(method='GET')
			with self.assertRaises(frappe.PermissionError):
				is_valid_http_method(api.download_pdf)
			frappe.local.request = SimpleNamespace(method='POST')
			is_valid_http_method(api.download_pdf)
		finally:
			frappe.flags.in_safe_exec = old_safe
			if old_request is missing:
				del frappe.local.request
			else:
				frappe.local.request = old_request
			if old_job is not missing:
				frappe.local.job = old_job

	def test_metrics_oversize_never_reaches_json_parser(self):
		with patch.object(frappe, 'parse_json', side_effect=AssertionError('oversize reached parser')):
			result = api.submit_frontend_metrics('x' * (api.MAX_FRONTEND_PAYLOAD_BYTES + 1))
		self.assertEqual(result, {'accepted': False, 'reason': 'payload too large'})

	def test_metrics_guest_is_denied_before_size_or_rate_work(self):
		try:
			frappe.set_user('Guest')
			with patch.object(api.ratelimit, 'enforce_user_rate_limit', side_effect=AssertionError('guest reached rate limiter')):
				with self.assertRaisesRegex(frappe.ValidationError, 'You must be logged in'):
					api.submit_frontend_metrics('x' * (api.MAX_FRONTEND_PAYLOAD_BYTES + 1))
		finally:
			frappe.set_user('Administrator')

	def test_owned_metrics_append_real_redis_only_after_validation(self):
		import json
		uuid = 'metrics-hardening-' + frappe.generate_hash(length=16)
		meta = redis_keys.session_meta(uuid)
		xhr, vitals = api._frontend_xhr_key(uuid), api._frontend_vitals_key(uuid)
		try:
			frappe.cache.set_value(meta, redis_schema.wrap_value({'user': 'Administrator', 'session_uuid': uuid}), expires_in_sec=60)
			result = api.submit_frontend_metrics(json.dumps({'session_uuid': uuid, 'xhr': [{'url': '/api/method/ping', 'duration_ms': 1}], 'vitals': []}))
			self.assertTrue(result['accepted'])
			self.assertEqual(len(frappe.cache.lrange(xhr, 0, -1)), 1)
		finally:
			frappe.cache.delete(xhr, vitals)
			frappe.cache.delete_value(meta)
