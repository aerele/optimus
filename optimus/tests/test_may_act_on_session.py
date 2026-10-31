# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""Decision matrix for ``permissions.may_act_on_session`` (design decision D1).

An action that changes an Optimus Session or spends AI tokens on it is allowed when the caller
can read the session AND is its owner or holds write on it (the System Manager role, or an
explicit write share). The seventh D1 row, "the permission engine raises", lives in
test_session_action_gate.py because only the api.py wrapper calls has_permission.
"""

import pytest

from optimus.permissions import may_act_on_session

OWNER = "owner@example.com"

MATRIX = [
	# label, user, owner, can_read, can_write, allowed
	("owner", OWNER, OWNER, True, False, True),
	("read_sharee", "sharee@example.com", OWNER, True, False, False),
	("write_sharee", "sharee@example.com", OWNER, True, True, True),
	("system_manager", "manager@example.com", OWNER, True, True, True),
	("stranger", "stranger@example.com", OWNER, False, False, False),
	("owner_read_revoked", OWNER, OWNER, False, False, False),
	("owner_case_insensitive", "owner@example.com", "Owner@Example.COM", True, False, True),
	("blank_owner_and_user", "", "", True, False, False),
	("whitespace_owner_and_user", "  ", "\t", True, False, False),
	("write_without_read", "sharee@example.com", OWNER, False, True, False),
]


@pytest.mark.parametrize(
	"label,user,owner,can_read,can_write,allowed", MATRIX, ids=[row[0] for row in MATRIX]
)
def test_may_act_on_session_matrix(label, user, owner, can_read, can_write, allowed):
	assert may_act_on_session(user=user, owner=owner, can_read=can_read, can_write=can_write) is allowed


def test_arguments_are_keyword_only():
	with pytest.raises(TypeError):
		may_act_on_session(OWNER, OWNER, True, False)
