# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Seed Ignored Apps on installs that predate the seed, so an empty table can mean no app is ignored."""

from optimus.install import _seed_ignored_apps_with_framework_apps


def execute():
	_seed_ignored_apps_with_framework_apps()
