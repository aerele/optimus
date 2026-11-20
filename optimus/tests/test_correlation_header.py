# optimus/tests/test_correlation_header.py
# Copyright (c) 2026, Optimus contributors

"""Tests for the X-Optimus-Recording-Id response header injection.

The correlation header is read by the browser-side optimus_frontend.js shim to
tie each XHR timing back to a server recording. The Access-Control-Expose-Headers
entry is load-bearing: without it browsers refuse to surface the custom header
to JavaScript, even for same-origin requests.
"""

import types

import frappe

from optimus.hooks_callbacks import _inject_correlation_header


def _set_fake_local(headers=None):
    """Replace frappe.local with a minimal SimpleNamespace holding a
    dict-like response_headers. Returns the namespace so tests can
    assert on it."""
    local = types.SimpleNamespace()
    if headers is not None:
        local.response_headers = headers
    frappe.local = local
    return local


def test_inject_correlation_header_merges_with_existing_expose():
    """If another app has already set Access-Control-Expose-Headers,
    our value must be appended, not overwritten."""
    headers = {"Access-Control-Expose-Headers": "X-Some-Other-Header"}
    _set_fake_local(headers)

    _inject_correlation_header("rec-uuid-abc123")

    expose = headers["Access-Control-Expose-Headers"]
    assert "X-Some-Other-Header" in expose
    assert "X-Optimus-Recording-Id" in expose


def test_inject_correlation_header_idempotent():
    """Calling the injector twice must not duplicate the expose entry."""
    headers = {}
    _set_fake_local(headers)

    _inject_correlation_header("rec-uuid-1")
    _inject_correlation_header("rec-uuid-2")

    expose = headers["Access-Control-Expose-Headers"]
    assert expose.count("X-Optimus-Recording-Id") == 1


def test_inject_correlation_header_tokenwise_match_not_substring():
    """The idempotency check must split on commas and compare tokens, not do a
    substring ``in`` match. Otherwise an existing
    ``X-Optimus-Recording-Id-Legacy`` entry makes the substring check skip
    adding the real header, which then never gets exposed to JavaScript.
    """
    headers = {"Access-Control-Expose-Headers": "X-Optimus-Recording-Id-Legacy"}
    _set_fake_local(headers)

    _inject_correlation_header("rec-uuid-abc123")

    expose = headers["Access-Control-Expose-Headers"]
    # Both tokens must be present the legacy one preserved, the
    # real one appended as a new token.
    tokens = [t.strip() for t in expose.split(",")]
    assert "X-Optimus-Recording-Id-Legacy" in tokens
    assert "X-Optimus-Recording-Id" in tokens


def test_inject_correlation_header_case_insensitive_idempotency():
    """HTTP header names are case-insensitive, so the token check must be too:
    an existing lowercase expose entry shouldn't get a duplicate added in
    different casing."""
    headers = {"Access-Control-Expose-Headers": "x-optimus-recording-id"}
    _set_fake_local(headers)

    _inject_correlation_header("rec-uuid-abc123")

    expose = headers["Access-Control-Expose-Headers"]
    # Must not add a second entry just because our spelling differs.
    assert expose.lower().count("x-optimus-recording-id") == 1
