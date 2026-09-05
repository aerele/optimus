# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for PII identifier hashing in capture.py."""

import pytest

from optimus import capture


def test_identify_args_get_doc_returns_safe_and_raw():
	raw, safe = capture._identify_args("get_doc", ("User", "customer@example.com"), {})
	assert raw == ("User", "customer@example.com")
	assert safe[0] == "User"  # doctype not hashed
	assert len(safe[1]) == 12  # sha256[:12]
	assert safe[1] != "customer@example.com"  # actually hashed


def test_identify_args_get_doc_deterministic():
	_, safe1 = capture._identify_args("get_doc", ("User", "x@y.com"), {})
	_, safe2 = capture._identify_args("get_doc", ("User", "x@y.com"), {})
	assert safe1 == safe2


def test_identify_args_cache_get_hashes_key():
	# cache_get wraps RedisWrapper.get_value (a class method), so args[0]
	# is the RedisWrapper instance (self) and args[1] is the actual key.
	fake_self = object()
	raw, safe = capture._identify_args(
		"cache_get", (fake_self, "user_lang:admin@example.com"), {},
	)
	assert raw == "user_lang:admin@example.com"
	assert isinstance(safe, str) and len(safe) == 12
	assert "@" not in safe


def test_identify_args_has_permission_keeps_doctype_and_ptype():
	# Frappe signature: has_permission(doctype, ptype="read", doc=None, ...)
	# So args[0]=doctype, args[1]=ptype, args[2]=doc.
	raw, safe = capture._identify_args(
		"has_permission",
		("Sales Invoice", "read", "SI-2026-00042"),
		{},
	)
	assert raw == ("Sales Invoice", "SI-2026-00042", "read")
	assert safe[0] == "Sales Invoice"  # doctype unchanged
	assert len(safe[1]) == 12  # name hashed
	assert safe[2] == "read"  # ptype unchanged


def test_identify_args_handles_missing_name():
	# has_permission can be called with name=None
	raw, safe = capture._identify_args("has_permission", ("Sales Invoice",), {})
	assert raw[0] == "Sales Invoice"
	assert raw[1] is None
	assert safe[1] is None


def test_identify_args_get_doc_with_dict_arg():
	"""frappe.get_doc({"doctype": "X", "name": "Y", ...}) dict form must yield a
	hashable identifier (returning the dict as-is once made the bucket key
	unhashable and crashed the redundant_calls analyzer)."""
	doc_dict = {
		"doctype": "Sales Invoice",
		"name": "SI-2026-00042",
		"items": [{"item_code": "X"}],
	}
	raw, safe = capture._identify_args("get_doc", (doc_dict,), {})
	# Both raw and safe must be tuples of strings (hashable!)
	assert raw == ("Sales Invoice", "SI-2026-00042")
	assert safe[0] == "Sales Invoice"
	assert len(safe[1]) == 12  # hashed name
	# Must be hashable for use as a dict key
	hash(raw)
	hash(safe)


def test_identify_args_get_doc_with_islocal_dict():
	"""An unsaved doc (``__islocal=1``, no final name) must report name=None rather
	than capturing a transient temp name."""
	doc_dict = {
		"doctype": "Sales Invoice",
		"name": "new-sales-invoice-fmoblfwoxh",  # transient temp name
		"__islocal": 1,
		"__unsaved": 1,
	}
	raw, safe = capture._identify_args("get_doc", (doc_dict,), {})
	# Name should be None we don't want temp names polluting the buckets
	assert raw == ("Sales Invoice", None)
	assert safe == ("Sales Invoice", None)


def test_identify_args_has_permission_correct_signature():
	"""Frappe's signature is has_permission(doctype, ptype="read", doc=None, ...),
	so args[0]=doctype, args[1]=ptype, args[2]=doc (not name).
	"""
	# Positional with ptype only
	raw, safe = capture._identify_args(
		"has_permission",
		("Sales Invoice", "write"),
		{},
	)
	assert raw == ("Sales Invoice", None, "write")
	assert safe[0] == "Sales Invoice"
	assert safe[1] is None  # no doc → no name → no hash
	assert safe[2] == "write"

	# Positional with doc as a string name
	raw, safe = capture._identify_args(
		"has_permission",
		("Sales Invoice", "read", "SI-2026-00042"),
		{},
	)
	assert raw == ("Sales Invoice", "SI-2026-00042", "read")
	assert safe[0] == "Sales Invoice"
	assert len(safe[1]) == 12

	# Positional with doc as a dict
	raw, safe = capture._identify_args(
		"has_permission",
		("Sales Invoice", "read", {"name": "SI-2026-00099", "doctype": "Sales Invoice"}),
		{},
	)
	assert raw[1] == "SI-2026-00099"


def test_identify_args_cache_get_handles_bytes_key():
	"""Frappe sometimes uses bytes cache keys; _identify_args must coerce to str so
	the hash is deterministic and the result hashable."""
	fake_self = object()
	bytes_key = b"_366a941cdecd5da0|table_columns::tabItem"
	raw, safe = capture._identify_args("cache_get", (fake_self, bytes_key), {})
	# Raw should be the decoded string
	assert raw == "_366a941cdecd5da0|table_columns::tabItem"
	assert isinstance(safe, str) and len(safe) == 12


def test_identify_args_unknown_fn_returns_hashable():
	"""Unknown fn_names must return hashable identifiers so redundant_calls
	bucketing doesn't crash."""
	raw, safe = capture._identify_args("unknown_fn", ("a", "b"), {"k": "v"})
	hash(raw)
	hash(safe)
