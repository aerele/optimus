# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Every `__(...)` in the shipped JS names a whole, fixed sentence.

Frappe extracts translation keys by reading the source, so a key built at runtime
(`__("+ " + n + " frames")`), a variable (`__(msg)`) or a key with a leading space
can never be translated. Calls may span lines.
"""

import os
import re

import pytest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIPPED = [
	os.path.join(APP_DIR, "public", "js"),
	os.path.join(APP_DIR, "optimus", "doctype", "optimus_session"),
	os.path.join(APP_DIR, "optimus", "doctype", "optimus_settings"),
]
CALL = re.compile(r"(?<![\w$.])(?<!function )__\(")
STRING = re.compile(r"\"((?:[^\"\\\n]|\\.)*)\"|'((?:[^'\\\n]|\\.)*)'")


def _js_files():
	for root in SHIPPED:
		for name in sorted(os.listdir(root)):
			if name.endswith(".js"):
				yield os.path.join(root, name)


def key_problems(source):
	"""Lines (1-based) with a `__(` whose first argument is not a plain string literal key."""
	problems = []
	for call in CALL.finditer(source):
		line = source.count("\n", 0, call.start()) + 1
		rest = source[call.end() :].lstrip()
		literal = STRING.match(rest)
		if not literal:
			problems.append((line, "not a string literal"))
			continue
		key = literal.group(1) if literal.group(1) is not None else literal.group(2)
		after = rest[literal.end() :].lstrip()
		if not after.startswith((",", ")")):
			problems.append((line, "key is built at runtime"))
		elif key != key.strip():
			problems.append((line, "key starts or ends with a space"))
	return problems


def test_checker_flags_each_bad_shape():
	assert key_problems('__("a" + n)') == [(1, "key is built at runtime")]
	assert key_problems("__(msg)") == [(1, "not a string literal")]
	assert key_problems("__(`t ${x}`)") == [(1, "not a string literal")]
	assert key_problems('__(" lead")') == [(1, "key starts or ends with a space")]
	assert key_problems('x = __(\n\t"split",\n\t[1],\n)') == []
	assert key_problems("a\n\n__(\n\t'multi' +\n\t'part')") == [(3, "key is built at runtime")]
	assert key_problems('frappe.__("x")') == []


@pytest.mark.parametrize("path", list(_js_files()), ids=os.path.basename)
def test_every_translation_key_is_a_fixed_sentence(path):
	with open(path) as f:
		problems = key_problems(f.read())
	assert not problems, f"{os.path.basename(path)}: {problems}"


LIST_VIEW = os.path.join(APP_DIR, "optimus", "doctype", "optimus_session", "optimus_session_list.js")
SESSION_DOCTYPE = os.path.join(APP_DIR, "optimus", "doctype", "optimus_session", "optimus_session.json")
LABEL_RETURN = re.compile(r"return\s*\[\s*([^,\]]+),")


def bare_labels(source):
	"""Label expressions of `return [label, color, filter]` that are a string literal or a variable.

	A bare literal or `__(variable)` shows in English whatever the site language, and no
	`__(` call is there for the key checker above to find.
	"""
	return [m.group(1).strip() for m in LABEL_RETURN.finditer(source) if not m.group(1).strip().startswith(("__(", "status ||"))]


def test_bare_label_checker_flags_literals_and_variables():
	assert bare_labels('return ["Stopping", "orange", ""];') == ['"Stopping"']
	assert bare_labels("return [status, 'gray', ''];") == ["status"]
	assert bare_labels('return [__("Failed"), "red", ""];') == []
	assert bare_labels('return [status || __("Unknown"), "gray", ""];') == []


def test_list_status_labels_are_each_a_translated_literal():
	import json

	with open(LIST_VIEW) as f:
		source = f.read()
	assert bare_labels(source) == []
	with open(SESSION_DOCTYPE) as f:
		options = next(x for x in json.load(f)["fields"] if x["fieldname"] == "status")["options"].split("\n")
	# "Recording"/"Stopping"/"Analyzing"/"Capturing Background Jobs"/"Failed" get a coloured label;
	# every other status falls to the outcome labels or the raw fallback.
	for status in ("Recording", "Stopping", "Analyzing", "Capturing Background Jobs", "Failed"):
		assert status in options
		assert f'__("{status}")' in source, status
