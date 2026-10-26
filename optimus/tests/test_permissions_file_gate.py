# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Who may download a session's report files."""

import types

import pytest

from optimus import permissions


def _file(doctype="Optimus Session", field="raw_report_file", name="OPT-1"):
	return types.SimpleNamespace(
		attached_to_doctype=doctype, attached_to_field=field, attached_to_name=name
	)


@pytest.fixture
def env(monkeypatch):
	frappe = permissions.frappe

	state = types.SimpleNamespace(roles=["Optimus User"], owner="alice@example.com")
	monkeypatch.setattr(frappe, "get_roles", lambda user=None: state.roles, raising=False)
	monkeypatch.setattr(frappe, "session", types.SimpleNamespace(user="alice@example.com"), raising=False)
	monkeypatch.setattr(
		frappe,
		"db",
		types.SimpleNamespace(get_value=lambda dt, name, field: state.owner),
		raising=False,
	)
	return state


@pytest.mark.parametrize("doc", [None, _file(doctype="ToDo"), _file(field="other_attachment")])
def test_unrelated_files_are_left_to_the_standard_checks(env, doc):
	assert permissions.file_has_permission(doc, user="mallory@example.com") is None


@pytest.mark.parametrize("field", ["raw_report_file", "raw_report_pdf_file"])
def test_recording_user_may_fetch_both_report_files(env, field):
	assert permissions.file_has_permission(_file(field=field), user="alice@example.com") is None


def test_other_user_is_denied_even_with_read_access_to_the_parent(env):
	assert permissions.file_has_permission(_file(), user="bob@example.com") is False


@pytest.mark.parametrize("role", ["System Manager", "Administrator"])
def test_admins_defer_to_standard_checks_without_owning_the_session(env, role):
	env.roles = [role]

	assert permissions.file_has_permission(_file(), user="bob@example.com") is None


def test_report_file_with_no_parent_name_is_denied(env):
	assert permissions.file_has_permission(_file(name=None), user="alice@example.com") is False


def test_defaults_to_the_session_user_when_none_is_passed(env):
	assert permissions.file_has_permission(_file()) is None  # alice owns it
	env.owner = "someone-else@example.com"
	assert permissions.file_has_permission(_file()) is False
