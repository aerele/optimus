"""Bounded JSON-only recording snapshots. No persisted input reaches pickle."""

import gzip
import io
import json
import math
import os
import stat
from itertools import chain
from pathlib import Path

MAX_COMPRESSED_BYTES = 16 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_JSON_NODES = 250_000
MAX_JSON_DEPTH = 64
MAX_RECORDINGS = 20_000


class InvalidBundle(ValueError):
	"""Fixed reason code, never recording content or a filesystem path."""


def _pairs(items):
	out = {}
	for key, value in items:
		if key in out:
			raise InvalidBundle("duplicate_json_key")
		out[key] = value
	return out


def _constant(_value):
	raise InvalidBundle("nonfinite_json_number")


def _check_structure(value):
	# Iterator frames bound auxiliary memory by depth, even for a wide object.
	stack = [(iter((value,)), 0)]
	count = 0
	while stack:
		iterator, depth = stack[-1]
		try:
			item = next(iterator)
		except StopIteration:
			stack.pop()
			continue
		count += 1
		if count > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
			raise InvalidBundle("json_structure_limit")
		if isinstance(item, dict):
			stack.append((chain.from_iterable(item.items()), depth + 1))
		elif isinstance(item, list):
			stack.append((iter(item), depth + 1))
		elif isinstance(item, float) and not math.isfinite(item):
			raise InvalidBundle("nonfinite_json_number")
		elif isinstance(item, str) and len(item) > MAX_JSON_BYTES:
			raise InvalidBundle("json_string_limit")


def encode(bundle: dict) -> bytes:
	"""Do not save a new snapshot that the bounded reader would reject."""
	_check_structure(bundle)
	if len(bundle.get("recordings", {})) > MAX_RECORDINGS:
		raise InvalidBundle("invalid_recordings")
	content = bytearray()
	for chunk in json.JSONEncoder(default=str, allow_nan=False).iterencode(bundle):
		chunk = chunk.encode("utf-8")
		if len(content) + len(chunk) > MAX_JSON_BYTES:
			raise InvalidBundle("json_size_limit")
		content.extend(chunk)
	compressed = gzip.compress(content)
	if len(compressed) > MAX_COMPRESSED_BYTES:
		raise InvalidBundle("compressed_size_limit")
	return compressed


def read(path: str, *, private_root: str, session_uuid: str) -> dict:
	"""Open a regular file directly inside the site's private-files directory.

	The caller checks the File row's attachment and URL first. A directory fd
	and O_NOFOLLOW prevent a symlink swap from redirecting this read. Nonblocking
	open also prevents a malicious FIFO from hanging a request before fstat.
	"""
	root, candidate = Path(private_root).resolve(), Path(path)
	if candidate.parent.resolve() != root or candidate.name in {"", ".", ".."}:
		raise InvalidBundle("file_path_outside_private_files")
	directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
	try:
		fd = os.open(candidate.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
	finally:
		os.close(directory)
	try:
		with os.fdopen(fd, "rb", closefd=False) as stream:
			info = os.fstat(stream.fileno())
			if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_COMPRESSED_BYTES:
				raise InvalidBundle("file_type_or_size")
			raw = stream.read(MAX_COMPRESSED_BYTES + 1)
	finally:
		os.close(fd)
	if len(raw) > MAX_COMPRESSED_BYTES:
		raise InvalidBundle("compressed_size_limit")
	with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
		raw = stream.read(MAX_JSON_BYTES + 1)
	if len(raw) > MAX_JSON_BYTES:
		raise InvalidBundle("json_size_limit")
	bundle = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
	_check_structure(bundle)
	if not isinstance(bundle, dict) or type(bundle.get("schema")) is not int or bundle["schema"] != 1:
		raise InvalidBundle("unsupported_schema")
	if not session_uuid or bundle.get("session_uuid") != session_uuid:
		raise InvalidBundle("session_uuid_mismatch")
	recordings = bundle.get("recordings")
	if not isinstance(recordings, dict) or len(recordings) > MAX_RECORDINGS:
		raise InvalidBundle("invalid_recordings")
	for uuid, entry in recordings.items():
		if not isinstance(uuid, str) or not uuid or len(uuid) > 140 or not isinstance(entry, dict):
			raise InvalidBundle("invalid_recording")
		rec = entry.get("rec")
		if not isinstance(rec, dict) or ("uuid" in rec and rec["uuid"] != uuid):
			raise InvalidBundle("recording_uuid_mismatch")
		if "calls" in rec and (not isinstance(rec["calls"], list) or any(not isinstance(call, dict) for call in rec["calls"])):
			raise InvalidBundle("invalid_query_records")
		# Old bundles remain useful, but opaque trees and argument sidecars
		# are never exposed to a downstream consumer.
		for key in ("tree_b64", "pyi_session", "sidecar"):
			entry.pop(key, None)
			rec.pop(key, None)
	return bundle
