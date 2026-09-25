# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Property tests (Hypothesis) for the key masking: ``redaction.scrub_secrets``
and ``maintenance._mask`` / ``_masked_record``.

A generated key is embedded in generated text in the shapes a leaked key
takes (a header dict, an ``api_key`` field, ``Bearer <key>``, a URL's
credentials, the bare key, its JSON-escaped form, a frame-local value
line) and must never survive, raw or JSON-escaped; a second pass must
change nothing.

The runs are deterministic (``derandomize=True``, no example database) and
bounded (``max_examples``), so they behave like any other unit test. Keys
are printable ASCII (``!`` to ``~``, what ``ai_fix._get_api_key`` accepts)
without ``*``: the mask itself is ``********``, so a key made of ``*`` could
not be told apart from it.
"""

import json
import string

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, example, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from optimus import maintenance  # noqa: E402
from optimus.redaction import scrub_secrets  # noqa: E402

_SETTINGS = settings(
	derandomize=True, database=None, deadline=None, max_examples=300,
	suppress_health_check=[HealthCheck.too_slow],
)

_KEY_CHARS = "".join(chr(c) for c in range(0x21, 0x7F) if chr(c) != "*")
_keys = st.text(alphabet=_KEY_CHARS, min_size=8, max_size=48)
# Keys the shape patterns alone must mask (a key the caller does not know,
# such as an older, rotated one): no quote, backslash, slash or "*", the
# characters that end a token in those shapes.
_SHAPE_KEY_CHARS = "".join(ch for ch in _KEY_CHARS if ch not in "'\"\\/")
_shape_keys = st.text(alphabet=_SHAPE_KEY_CHARS, min_size=8, max_size=48)
# Ordinary text around the key: one log line's worth, quotes and URL
# characters included, no "*".
_filler = st.text(alphabet=string.ascii_letters + string.digits + " .,:;=()[]{}_-/'\"@", max_size=40)


def _escaped(key: str) -> str:
	return json.dumps(key)[1:-1]


def _shape(name: str, key: str) -> str:
	"""``key`` as it sits in a leaked row."""
	return {
		"header_dict": f"headers = {{'content-type': 'application/json', 'authorization': 'Bearer {key}'}}",
		"x_api_key_dict": f"headers = {{\"x-api-key\": \"{key}\"}}",
		"api_key_field": f"provider = {{'name': 'OpenAI', 'api_key': '{key}'}}",
		"bearer": f"Authorization: Bearer {key} rejected",
		"url_userinfo": f"POST https://user:{key}@llm.internal:11434/v1/chat/completions",
		"bare": f"the provider echoed {key} back",
		"json_escaped": f'{{"error": "invalid key {_escaped(key)}"}}',
	}[name]


_SHAPES = ("header_dict", "x_api_key_dict", "api_key_field", "bearer", "url_userinfo", "bare", "json_escaped")
# The shapes the patterns cover without the key as a literal.
_PATTERN_SHAPES = ("header_dict", "x_api_key_dict", "api_key_field", "bearer", "url_userinfo")


@st.composite
def _leaky_text(draw, keys=_keys, shapes=_SHAPES, bare_in_filler=True):
	"""``(key, text)``: the key in one to three shapes, between lines of
	filler. With ``bare_in_filler=False`` the filler never holds the key:
	a bare key is masked only as a literal."""
	key = draw(keys)
	filler = _filler if bare_in_filler else _filler.filter(lambda text: not _survives(key, text))
	chosen = draw(st.lists(st.sampled_from(shapes), min_size=1, max_size=3))
	lines = [draw(filler)]
	for shape in chosen:
		lines += [_shape(shape, key), draw(filler)]
	return key, "\n".join(lines)


def _survives(key: str, text: str) -> bool:
	return key in text or _escaped(key) in text


class TestScrubSecrets:
	@_SETTINGS
	@given(_leaky_text())
	def test_a_key_passed_as_a_literal_never_survives(self, case):
		key, text = case
		out = scrub_secrets(text, literals=(key, _escaped(key)))
		assert not _survives(key, out)

	@_SETTINGS
	@given(_leaky_text(keys=_shape_keys, shapes=_PATTERN_SHAPES, bare_in_filler=False))
	def test_the_shapes_mask_a_key_the_caller_does_not_know(self, case):
		key, text = case
		assert not _survives(key, scrub_secrets(text))

	@_SETTINGS
	@given(_leaky_text(), st.booleans())
	def test_it_is_idempotent(self, case, with_literals):
		key, text = case
		literals = (key, _escaped(key)) if with_literals else ()
		once = scrub_secrets(text, literals=literals)
		assert scrub_secrets(once, literals=literals) == once

	@_SETTINGS
	@given(_filler, _keys)
	def test_text_without_a_key_or_a_shape_is_left_as_it_is(self, text, key):
		hypothesis.assume(not _survives(key, text) and "://" not in text and "Bearer" not in text)
		hypothesis.assume(not any(marker in text.lower() for marker in ("authorization", "api_key", "api-key", "apikey")))
		assert scrub_secrets(text, literals=(key, _escaped(key))) == text


# Frappe's with-context traceback prints the header value bare in the
# urllib3 / http.client frames; Deleted Document data holds the same lines
# JSON-escaped.
def _value_lines(key: str) -> list[str]:
	return [f"      value = '{key}'", f"      values = ['{key}']", f"      one_value = b'{key}'"]


@st.composite
def _leaky_error(draw, keys=_keys):
	"""``(key, text)``: a traceback-like text holding the key in the shapes,
	in value lines and in their JSON-escaped form."""
	key, text = draw(_leaky_text(keys=keys))
	lines = [text, *draw(st.lists(st.sampled_from(_value_lines(key)), max_size=3))]
	joined = "\n".join(lines)
	if draw(st.booleans()):
		joined = json.dumps({"error": joined})  # a Deleted Document's data
	return key, joined


_AI_FRAME = '  File "apps/optimus/optimus/ai_fix.py", line 1347, in _call_openai_chat'
_OTHER_FRAME = '  File "apps/erpnext/erpnext/controllers/queries.py", line 80, in get'
# A value line's value where no pattern can apply: no quote, "Bearer",
# "://" or key-field name.
_plain_values = st.text(alphabet=string.ascii_lowercase + string.digits + " ", min_size=1, max_size=30)


class TestMask:
	@_SETTINGS
	@given(_leaky_error(), st.booleans())
	def test_the_key_never_survives(self, case, value_lines):
		key, text = case
		assert not _survives(key, maintenance._mask(text, key, value_lines=value_lines))

	@_SETTINGS
	@given(_leaky_error(), st.booleans())
	def test_it_is_idempotent(self, case, value_lines):
		key, text = case
		once = maintenance._mask(text, key, value_lines=value_lines)
		assert maintenance._mask(once, key, value_lines=value_lines) == once


# The end of a title that a shape can continue across the join when a long
# title is moved in front of the error ("<title>\n<error>").
_TITLE_ENDS = ("", " Bearer", " 'authorization': 'Bearer", " 'api_key': '", " https://user:")
_ERROR_STARTS = ("", "abcdefghij rest'", "  value = 'abc'")


@st.composite
def _error_log_records(draw):
	"""``(key, record)``: an Error Log record, as the Error Log hook masks it
	before its insert, whose text fields may hold the key in any shape,
	with a title of any length, which may end in the start of a shape that
	the error's first line completes."""
	key = draw(_keys)
	fields = {}
	for field in ("error", "method", "metadata"):
		if draw(st.booleans()):
			_, text = draw(_leaky_error(keys=st.just(key)))
		else:
			text = draw(_filler)
		fields[field] = text
	fields["error"] = draw(st.sampled_from(_ERROR_STARTS)) + fields["error"]
	if draw(st.booleans()):
		fields["error"] = _AI_FRAME + "\n" + fields["error"]
	fields["method"] = (
		fields["method"] + draw(st.text(alphabet=string.ascii_letters + " ", max_size=300))
		+ draw(st.sampled_from(_TITLE_ENDS))
	)
	return key, {**fields, "seen": 0, "reference_doctype": None}


# A title of 147 characters that ends in "Bearer", and an error whose first
# token completes it once the title is moved in front of the error.
_SPLIT_BEARER = ("sk-live-0123456789abcdefXYZ", {
	"method": "x" * 140 + " Bearer", "error": "abcdefghij rest", "metadata": None, "seen": 0,
	"reference_doctype": None,
})


class TestMaskedRecord:
	@_SETTINGS
	@given(_error_log_records())
	def test_the_key_never_survives(self, case):
		key, record = case
		out = maintenance._masked_record(record, key)
		assert out is not None
		for field in ("error", "method", "metadata"):
			assert not _survives(key, out[field]), field

	@_SETTINGS
	@given(_error_log_records())
	@example(_SPLIT_BEARER)
	def test_it_is_idempotent(self, case):
		key, record = case
		once = maintenance._masked_record(record, key)
		assert maintenance._masked_record(once, key) == once

	@_SETTINGS
	@given(_error_log_records())
	def test_the_title_fits_its_column_and_a_long_one_leads_the_error(self, case):
		key, record = case
		out = maintenance._masked_record(record, key)
		limit = maintenance._FIELD_LIMITS["method"]
		assert len(out["method"]) <= limit
		value_lines = maintenance._is_ai_record(record, key)
		title = maintenance._mask(record["method"], key, value_lines=value_lines)
		error = maintenance._mask(record["error"], key, value_lines=value_lines)
		if len(title) > limit:
			assert out["method"] == title[:limit]
			# v16's ErrorLog.validate: the whole title, a newline, then the
			# error. The joined text is masked again, so a shape the join
			# completes is masked too; where it completes none, this is the
			# masked title, a newline and the masked error.
			joined = f"{title}\n{error}"
			assert out["error"] == maintenance._mask(joined, key, value_lines=value_lines)
		else:
			assert out["method"] == title
			assert out["error"] == error
		assert out["seen"] == 0 and out["reference_doctype"] is None  # other fields kept

	def test_a_shape_the_join_completes_is_masked(self):
		# The title ends in "Bearer" and the error starts with the token: only
		# the joined text holds "Bearer <token>".
		key, record = _SPLIT_BEARER
		out = maintenance._masked_record(record, key)
		assert out["method"] == "x" * 140
		assert out["error"] == "x" * 140 + " Bearer\n******** rest"

	@_SETTINGS
	@given(_keys, st.lists(_shape_keys, min_size=1, max_size=4), _filler)
	def test_an_ai_records_value_lines_are_masked_whatever_they_hold(self, key, values, filler):
		# A header value in the urllib3 / http.client frames of Optimus's AI
		# code: masked even when it is not the stored key (an older key).
		hypothesis.assume("value" not in filler)
		lines = [f"      value = '{value}'" for value in values]
		record = {"error": "\n".join([_AI_FRAME, *lines, filler]), "method": "optimus ai_fix", "metadata": None}
		out = maintenance._masked_record(record, key)
		for value in values:
			assert f"      value = '{value}'" not in out["error"]
		assert out["error"].count("      value = ********") == len(values)

	@_SETTINGS
	@given(_keys, st.lists(_plain_values, min_size=1, max_size=4), _filler)
	def test_a_record_that_is_not_an_ai_record_keeps_its_value_lines(self, key, values, filler):
		# An ERPNext error with value locals: not from Optimus's AI code and
		# without the key, so only scrub_secrets runs on it.
		lines = [f"      value = '{value}'" for value in values]
		error = "\n".join([_OTHER_FRAME, *lines, filler])
		hypothesis.assume(not _survives(key, error))
		record = {"error": error, "method": "frappe.client.get", "metadata": None}
		assert not maintenance._is_ai_record(record, key)
		out = maintenance._masked_record(record, key)
		for line in lines:
			assert line in out["error"]
