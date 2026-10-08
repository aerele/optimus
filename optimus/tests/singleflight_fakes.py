# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Shared in-memory Redis for the analyze single-flight tests (T10, PF1).

Not a test module (no ``test_`` prefix), so pytest does not collect it. ``FakeRedis``
stands in for the Redis server behind ``frappe.cache``: GET, SET with NX and EX, SETEX,
EXPIRE, UNLINK and DELETE, each atomic under one lock, with keys that expire on a virtual
clock. ``FlagCache`` is a small ``frappe.cache`` over it that keeps no job-local cache;
test_analyze_singleflight_redis.py drives Frappe's real ``RedisWrapper`` over the same
server for the ``frappe.local.cache`` behaviour.
"""

from __future__ import annotations

import pickle
import threading

from optimus import analyze

SITE = "site1"


class FakeRedis:
	"""The Redis server. ``before`` (optional) runs before every command, outside the lock,
	with the command name: a test uses it to line up two racing sessions or to change the
	server between two commands of one session."""

	def __init__(self):
		self.data: dict = {}  # name -> [value, expires_at | None]
		self.now = 0.0
		self.lock = threading.Lock()
		self.before = None

	def advance(self, seconds: float) -> None:
		self.now += seconds

	def _hook(self, command: str) -> None:
		if self.before is not None:
			self.before(command)

	def _live(self, name):
		entry = self.data.get(name)
		if entry is not None and entry[1] is not None and entry[1] <= self.now:
			del self.data[name]
			return None
		return entry

	def _store(self, name, value, ex=None, nx=False):
		with self.lock:
			if nx and self._live(name) is not None:
				return None
			self.data[name] = [value, None if ex is None else self.now + ex]
			return True

	def get(self, name):
		self._hook("get")
		with self.lock:
			entry = self._live(name)
			return entry[0] if entry else None

	def set(self, name, value, ex=None, nx=False, **kwargs):
		self._hook("set")
		return self._store(name, value, ex=ex, nx=nx)

	def setex(self, name, time, value):
		self._hook("setex")
		return self._store(name, value, ex=time)

	def expire(self, name, time):
		self._hook("expire")
		with self.lock:
			entry = self._live(name)
			if entry is None:
				return False
			entry[1] = self.now + time
			return True

	def unlink(self, *names):
		self._hook("unlink")
		with self.lock:
			return sum(self.data.pop(name, None) is not None for name in names)

	def delete(self, *names):
		return self.unlink(*names)

	# Test-side access: no hook, no command.
	def put(self, name, value, ex=None):
		"""Another session's write, pickled the way Frappe stores values."""
		self._store(name, pickle.dumps(value), ex=ex)

	def peek(self, name):
		with self.lock:
			entry = self._live(name)
			return pickle.loads(entry[0]) if entry else None

	def ttl(self, name):
		with self.lock:
			entry = self._live(name)
			if entry is None or entry[1] is None:
				return None
			return entry[1] - self.now


def connection_error() -> type[Exception]:
	"""redis-py's ConnectionError (what a dropped Redis raises; RedisWrapper swallows it in
	some calls), or the builtin where redis is not installed."""
	try:
		from redis.exceptions import ConnectionError as RedisConnectionError
	except ImportError:
		return ConnectionError
	return RedisConnectionError


def flag_key() -> bytes:
	"""The flag's key as Frappe's ``make_key`` builds it for ``SITE``."""
	return f"{SITE}|{analyze._SINGLEFLIGHT_KEY}".encode()


class FlagCache:
	"""A ``frappe.cache`` over ``FakeRedis`` for the CI stub run: the value API pickles like
	Frappe's ``RedisWrapper`` but keeps no ``frappe.local.cache``; the raw ``set`` and
	``expire`` commands go straight to the server, as redis-py's do."""

	def __init__(self, holder=None, ttl=None, server: FakeRedis | None = None):
		self.server = server or FakeRedis()
		if holder:
			self.set_value(analyze._SINGLEFLIGHT_KEY, holder, expires_in_sec=ttl)

	def make_key(self, key, user=None, shared=False):
		return f"{SITE}|{key}".encode()

	def get_value(self, key, *args, expires=False, **kwargs):
		raw = self.server.get(self.make_key(key))
		return None if raw is None else pickle.loads(raw)

	def set_value(self, key, val, *args, expires_in_sec=None, **kwargs):
		self.server.set(self.make_key(key), pickle.dumps(val), ex=expires_in_sec)

	def delete_value(self, key, *args, **kwargs):
		self.server.unlink(self.make_key(key))

	def set(self, name, value, **kwargs):
		return self.server.set(name, value, **kwargs)

	def expire(self, name, time):
		return self.server.expire(name, time)

	@property
	def holder(self):
		return self.server.peek(flag_key())

	@property
	def ttl(self):
		return self.server.ttl(flag_key())
