# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Tests for the remove_dedicated_codex_home patch: the Codex subscription backend now uses the
operator's own ``~/.codex`` login, so the old ``<site>/private/optimus-codex`` folder (which
holds a live refresh token) is orphaned and is removed on migrate."""

import importlib
import os
import sys
import types

import pytest


@pytest.fixture
def patch(monkeypatch, tmp_path):
	stub = types.ModuleType("frappe")
	site = tmp_path / "site"
	stub.get_site_path = lambda *parts: str(site.joinpath(*parts))
	monkeypatch.setitem(sys.modules, "frappe", stub)
	name = "optimus.patches.v0_12_128.remove_dedicated_codex_home"
	sys.modules.pop(name, None)
	module = importlib.import_module(name)
	yield types.SimpleNamespace(module=module, home=site / "private" / "optimus-codex", site=site)
	sys.modules.pop(name, None)


def _signed_in_home(home):
	home.mkdir(parents=True, mode=0o700)
	(home / "auth.json").write_text('{"tokens": {"refresh_token": "LIVE"}}')
	(home / "sessions").mkdir()
	(home / "sessions" / "x.jsonl").write_text("x")


def test_the_orphaned_dedicated_home_and_its_token_are_removed(patch, capsys):
	_signed_in_home(patch.home)
	patch.module.execute()
	assert not patch.home.exists()
	out = capsys.readouterr().out
	assert "optimus-codex" in out and "LIVE" not in out  # one notice, never the token


def test_nothing_else_in_the_private_folder_is_touched(patch):
	_signed_in_home(patch.home)
	other = patch.home.parent / "files-keep.txt"
	other.write_text("keep")
	patch.module.execute()
	assert other.read_text() == "keep"


def test_no_dedicated_home_is_a_silent_no_op(patch, capsys):
	patch.module.execute()
	assert capsys.readouterr().out == ""
	assert not patch.site.exists()  # it creates nothing


def test_running_twice_is_safe(patch):
	_signed_in_home(patch.home)
	patch.module.execute()
	patch.module.execute()
	assert not patch.home.exists()


def test_a_symlink_is_unlinked_without_following_it(patch, tmp_path):
	target = tmp_path / "elsewhere"
	target.mkdir()
	(target / "precious.txt").write_text("keep")
	patch.home.parent.mkdir(parents=True)
	patch.home.symlink_to(target)
	patch.module.execute()
	assert not patch.home.is_symlink() and not patch.home.exists()
	assert (target / "precious.txt").read_text() == "keep"


def test_a_removal_failure_warns_with_the_path_and_never_blocks_migrate(patch, monkeypatch, capsys):
	_signed_in_home(patch.home)

	def boom(path, *a, **kw):
		raise PermissionError("denied")

	monkeypatch.setattr(patch.module.shutil, "rmtree", boom)
	patch.module.execute()  # must not raise
	out = capsys.readouterr().out
	assert str(patch.home) in out and "remove" in out.lower()
	assert patch.home.exists()


def test_the_patch_is_registered_in_patches_txt():
	here = os.path.dirname(os.path.abspath(__file__))
	with open(os.path.join(here, "..", "patches.txt")) as f:
		lines = [line.strip() for line in f]
	assert "optimus.patches.v0_12_128.remove_dedicated_codex_home" in lines
