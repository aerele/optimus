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
# Recorder stacks contain many small scalar fields. Keep a finite work budget
# large enough for ordinary multi-request captures, alongside the byte bounds.
MAX_JSON_NODES = 4_000_000
MAX_JSON_DEPTH = 64
MAX_RECORDINGS = 20_000
SNAPSHOT_FAILURE_WARNING = "Recording snapshot could not be saved. Redis input was retained for Retry Analyze (up to 7 days); do not clear Recorder data."
DEGRADED_WARNING = "Recording snapshot exceeded full-detail limits: saved every query with up to 3 application stack frames and without EXPLAIN results."


class InvalidBundle(ValueError):
	"""Fixed reason code, never recording content or a filesystem path."""


class SnapshotUnavailable(InvalidBundle):
	"""A complete recording snapshot was not committed; retain transient input."""


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
	_validate_recordings(bundle)
	if len(bundle.get("recordings", {})) > MAX_RECORDINGS:
		raise InvalidBundle("invalid_recordings")
	content = bytearray()
	for chunk in json.JSONEncoder(default=str, allow_nan=False).iterencode(bundle):
		chunk = chunk.encode("utf-8")
		if len(content) + len(chunk) > MAX_JSON_BYTES:
			raise InvalidBundle("json_size_limit")
		content.extend(chunk)
	compressed = gzip.compress(content, mtime=0)
	if len(compressed) > MAX_COMPRESSED_BYTES:
		raise InvalidBundle("compressed_size_limit")
	return compressed


def encode_snapshot(bundle: dict) -> tuple[bytes, bool]:
	"""Prefer full detail; preserve all queries in one bounded degraded form.

	Neither form mutates live Recorder data. If even the reduced form cannot fit,
	the caller must retain Redis and fail analysis instead of erasing its input.
	"""
	try:
		return encode(bundle), False
	except InvalidBundle as exc:
		if str(exc) not in {"json_structure_limit", "json_size_limit", "compressed_size_limit", "json_string_limit"}:
			raise
	reduced = {**bundle, "snapshot_detail": "reduced-stacks-without-explain", "recordings": {}}
	for uuid, entry in bundle["recordings"].items():
		rec = dict(entry["rec"])
		calls = []
		for original in rec.get("calls", []):
			call = {k: v for k, v in original.items() if k not in {"stack", "explain", "explain_result", "explain_results"}}
			frames = original.get("stack")
			if isinstance(frames, list):
				app = [frame for frame in frames if isinstance(frame, dict) and "/apps/" in "/" + str(frame.get("filename", "")) and "/apps/frappe/" not in "/" + str(frame.get("filename", ""))]
				call["stack"] = (app or [f for f in frames if isinstance(f, dict)])[:3]
			calls.append(call)
		rec["calls"] = calls
		reduced["recordings"][uuid] = {**entry, "rec": rec}
	return encode(reduced), True


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
	_validate_recordings(bundle)
	if not session_uuid or bundle.get("session_uuid") != session_uuid:
		raise InvalidBundle("session_uuid_mismatch")
	for entry in bundle["recordings"].values():
		# Opaque trees and argument sidecars never reach a persisted consumer.
		for key in ("tree_b64", "pyi_session", "sidecar"):
			entry.pop(key, None)
			entry["rec"].pop(key, None)
	return bundle


def _validate_recordings(bundle):
	"""The writer and reader must accept the same recorder structure."""
	if not isinstance(bundle, dict) or type(bundle.get("schema")) is not int or bundle["schema"] != 1:
		raise InvalidBundle("unsupported_schema")
	if not isinstance(bundle.get("session_uuid"), str) or not bundle["session_uuid"]:
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
