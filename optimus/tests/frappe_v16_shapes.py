# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Frappe v16's ``RedisWrapper`` value API, transcribed so Frappe-free CI runs the same
behaviour the bench tests check against the real class.

Transcribed from frappe/utils/redis_wrapper.py as of frappe commit 2fcb9397a1 (its
__version__ is 16.54.0; compared line by line with that checkout, not a release tag): make_key,
set_value, get_value, _get_value_with_size, delete_value. Unlike v15, v16 swallows
redis ConnectionError on get, set and unlink, ``get_value`` takes ``use_local_cache`` and
``set_value`` always fills ``frappe.local.cache``. The real class stays exercised by the
``v16`` parameter of test_analyze_singleflight_redis.py, which runs on a bench only.
"""

import pickle
from contextlib import suppress

import frappe

from optimus.tests.singleflight_fakes import connection_error


class V16ValueApi:
	"""v16's value API over a server exposing get/set/setex/expire/unlink/delete."""

	def __init__(self, server):
		self.server = server

	def get(self, name):
		return self.server.get(name)

	def set(self, name, value, ex=None, nx=False, **kwargs):
		return self.server.set(name, value, ex=ex, nx=nx)

	def setex(self, name, time, value):
		return self.server.setex(name, time, value)

	def expire(self, name, time, *args, **kwargs):
		return self.server.expire(name, time)

	def unlink(self, *names):
		return self.server.unlink(*names)

	def delete(self, *names):
		return self.server.delete(*names)

	def make_key(self, key, user=None, shared=False):
		if shared:
			return key
		if user:
			if user is True:
				user = frappe.local.session.get("user")
			key = f"user:{user}:{key}"
		return f"{frappe.local.conf.get('db_name')}|{key}".encode()

	def set_value(self, key, val, user=None, expires_in_sec=None, shared=False) -> int:
		key = self.make_key(key, user, shared)
		frappe.local.cache[key] = val
		serialized = pickle.dumps(val, protocol=5)
		with suppress(connection_error()):
			self.set(name=key, value=serialized, ex=expires_in_sec)
		return len(serialized)

	def get_value(self, key, generator=None, user=None, expires=False, shared=False, *, use_local_cache=True):
		return self._get_value_with_size(
			key,
			generator=generator,
			user=user,
			expires=expires,
			shared=shared,
			use_local_cache=use_local_cache,
		)[0]

	def _get_value_with_size(
		self, key, *, generator=None, user=None, expires=False, shared=False, use_local_cache=True
	):
		original_key = key
		key = self.make_key(key, user, shared)
		size = 0
		local_cache = frappe.local.cache
		if key in local_cache and use_local_cache:
			return local_cache[key], size
		val = None
		try:
			val = self.get(key)
		except connection_error():
			pass
		if val is not None:
			size = len(val)
			val = pickle.loads(val)
		if not expires:
			if val is None and generator:
				val = generator()
				size = self.set_value(original_key, val, user=user, shared=shared)
			else:
				local_cache[key] = val
		return val, size

	def expire_key(self, key, time, *, user=None, shared=False):
		key = self.make_key(key, user, shared)
		try:
			return self.expire(key, time)
		except connection_error():
			pass

	def delete_value(self, keys, user=None, make_keys=True, shared=False):
		if not keys:
			return
		if not isinstance(keys, list | tuple):
			keys = (keys,)
		if make_keys:
			keys = [self.make_key(k, shared=shared, user=user) for k in keys]
		local_cache = frappe.local.cache
		for key in keys:
			local_cache.pop(key, None)
		try:
			self.unlink(*keys)
		except connection_error():
			pass
