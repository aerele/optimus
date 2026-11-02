# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Remove the retired Optimus-owned Codex home.

The Codex subscription backend used to keep its own sign-in in ``<site>/private/optimus-codex``.
It now uses the operator's own ``~/.codex`` login, so that folder is orphaned and still holds a
live refresh token. This deletes it and prints one line; on failure it prints one warning with
the path instead, and never blocks the migrate."""

import os
import shutil

import frappe

_NOT_REVOKED = (
	"The token was not revoked on the server: if a backup or snapshot holds that folder, "
	"sign those sessions out from your ChatGPT account."
)


def execute():
	path = os.path.abspath(frappe.get_site_path("private", "optimus-codex"))
	if not os.path.lexists(path):
		return
	try:
		if os.path.islink(path):
			os.unlink(path)  # the link only, never what it points at
		else:
			shutil.rmtree(path)
	except OSError:
		print(
			f"Optimus could not remove the retired Codex sign-in folder {path}. It holds a "
			"live refresh token: delete it by hand. Optimus now uses your own ~/.codex login. "
			+ _NOT_REVOKED
		)
		return
	print(
		f"Optimus removed the retired Codex sign-in folder {path}. Codex subscription mode now uses "
		"your own `codex login` from your terminal (~/.codex). "
		+ _NOT_REVOKED
	)
