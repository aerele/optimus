from optimus import ai_fix, ai_prompts


def test_hints_exist_only_for_eligible_types():
	assert set(ai_prompts.FINDING_TYPE_HINTS) <= set(ai_fix.AI_ELIGIBLE_FINDING_TYPES)
	assert not hasattr(ai_prompts, "POSTGRES_EXPLAIN_HINTS")


def test_user_message_has_no_ddl_section():
	finding = {"finding_type": "Slow Query", "technical_detail": {}}
	finding["technical_detail"]["suggested_ddl"] = "ALTER TABLE `tabX` ADD INDEX (a);"
	_system, messages = ai_fix._build_messages(finding)
	assert "ALTER TABLE" not in messages[-1]["content"]
