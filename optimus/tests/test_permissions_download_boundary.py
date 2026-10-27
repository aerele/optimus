# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Characterize the known direct-download gap using installed Frappe code.

These are not proof that raw artifacts are protected. A parent read-share
still grants a direct download even when Optimus denies File read access.
Only storage, identity and response delivery are faked; the download route,
file lookup, File.is_downloadable and core File permission check are real.
No site, database, Redis or HTTP server is used. Without Frappe, skip rather
than substitute a hook-loop replay for the download route.
"""

from importlib import import_module
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import permissions


@pytest.mark.parametrize("field", sorted(permissions._GATED_FIELDS))
@pytest.mark.parametrize("parent_read", [False, True], ids=["stranger", "read-sharee"])
def test_direct_download_uses_parent_read_despite_file_hook_denial(monkeypatch, field, parent_read):
	if not getattr(permissions.frappe, "__file__", None):
		pytest.skip("Requires installed Frappe; a stub cannot prove the download route")
	core = import_module("frappe.core.doctype.file.file")
	response = import_module("frappe.utils.response")
	frappe = core.frappe
	assert permissions.frappe is frappe
	user = "reader@example.test"
	url = "/private/files/synthetic-artifact.json.gz"
	doc = SimpleNamespace(
		name="synthetic-file", is_private=True, owner="system@example.test",
		attached_to_doctype="Optimus Session", attached_to_name="synthetic-session",
		attached_to_field=field, file_url=url,
	)
	doc.is_downloadable = MethodType(core.File.is_downloadable, doc)
	parent = SimpleNamespace(has_permission=Mock(return_value=parent_read))

	def get_doc(*args, **kwargs):
		if kwargs.get("doctype") == "File":
			assert kwargs["name"] == doc.name
			return doc
		assert args == ("Optimus Session", doc.attached_to_name)
		return parent

	monkeypatch.setattr(frappe, "session", SimpleNamespace(user=user))
	monkeypatch.setattr(frappe, "form_dict", SimpleNamespace(fid=None))
	monkeypatch.setattr(frappe, "db", SimpleNamespace(get_value=lambda *a, **kw: "owner@example.test"))
	monkeypatch.setattr(frappe, "get_roles", lambda user: ["Optimus User"])
	monkeypatch.setattr(frappe, "share", SimpleNamespace(get_shared=lambda *a, **kw: []))
	monkeypatch.setattr(frappe, "get_doc", get_doc)
	lookup = Mock(return_value=[{"name": doc.name}])
	monkeypatch.setattr(frappe, "get_all", lookup)
	# Any unexpected attempt to enter the application hook loop is a failure.
	monkeypatch.setattr(frappe, "has_permission", Mock(side_effect=AssertionError("File hook loop reached")))
	monkeypatch.setattr(frappe, "get_hooks", Mock(side_effect=AssertionError("App hooks loaded")))
	deliver = Mock(return_value=object())
	access_log = Mock()
	monkeypatch.setattr(response, "send_private_file", deliver)
	monkeypatch.setattr(response, "make_access_log", access_log)
	gate = Mock(wraps=permissions.file_has_permission)
	monkeypatch.setattr(permissions, "file_has_permission", gate)
	assert gate(doc, "read", user=user) is False
	gate.reset_mock()

	if parent_read:
		assert response.download_private_file(url) is deliver.return_value
		deliver.assert_called_once_with("/files/synthetic-artifact.json.gz")
		access_log.assert_called_once()
	else:
		with pytest.raises(response.Forbidden):
			response.download_private_file(url)
		deliver.assert_not_called()
		access_log.assert_not_called()

	lookup.assert_called_once_with("File", filters={"file_url": url}, fields="*")
	parent.has_permission.assert_called_once_with("read", debug=False, user=user)
	gate.assert_not_called()
