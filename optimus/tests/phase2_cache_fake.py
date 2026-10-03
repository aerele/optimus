"""Redis serialization and optimistic transactions for capture ownership tests."""

import pickle
import threading

from redis.exceptions import WatchError


class Cache:
	"""Redis byte values plus optimistic transactions, with forced race hooks."""
	def __init__(self):
		self.data, self.versions = {}, {}
		self.local = {}
		self.lock = threading.Lock()
		self.before_execute = None

	def make_key(self, key):
		return ("fake-site|" + key).encode()

	def set_value(self, key, value, **kw):
		key = self.make_key(key)
		self.data[key] = pickle.dumps(value, protocol=5)
		self.versions[key] = self.versions.get(key, 0) + 1

	def get_value(self, key, **kw):
		raw = self.data.get(self.make_key(key))
		return pickle.loads(raw) if raw else None

	def delete_value(self, key):
		key = self.make_key(key)
		self.data.pop(key, None)
		self.versions[key] = self.versions.get(key, 0) + 1

	def lrange(self, key, start, stop):
		items = self.data.get(self.make_key(key), [])
		return items[start:] if stop == -1 else items[start:stop + 1]

	def get(self, key):
		return self.data.get(key)

	def set(self, key, value, **kw):
		self.data[key] = value
		self.versions[key] = self.versions.get(key, 0) + 1

	def delete(self, *keys):
		for key in keys:
			self.data.pop(key, None)
			self.versions[key] = self.versions.get(key, 0) + 1

	def pipeline(self):
		cache = self
		class Pipeline:
			def __enter__(self):
				self.watched, self.commands = {}, []
				return self
			def __exit__(self, *a):
				return False
			def watch(self, *keys):
				self.watched.update({key: cache.versions.get(key, 0) for key in keys})
			def get(self, key):
				return cache.data.get(key)
			def llen(self, key):
				return len(cache.data.get(key, []))
			def exists(self, *keys):
				return sum(key in cache.data for key in keys)
			def multi(self):
				pass
			def set(self, key, value, **kw):
				self.commands.append((key, value))
				return self
			def rpush(self, key, value):
				self.commands.append((key, cache.data.get(key, []) + [value]))
				return self
			def expire(self, key, value):
				return self
			def delete(self, key):
				self.commands.append((key, None))
				return self
			def execute(self):
				if cache.before_execute:
					cache.before_execute()
				with cache.lock:
					if any(cache.versions.get(key, 0) != version for key, version in self.watched.items()):
						raise WatchError()
					for key, value in self.commands:
						if value is None:
							cache.data.pop(key, None)
						else:
							cache.data[key] = value
						cache.versions[key] = cache.versions.get(key, 0) + 1
					return [True] * len(self.commands)
		return Pipeline()
