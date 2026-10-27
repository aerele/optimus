"""The public provider gate rejects explicitly excluded finding types."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from optimus import ai_fix


def _cfg(**kw):
	base = {
		"ai_enabled": True, "ai_auto_suggest": True, "ai_auto_suggest_max": 0, "ai_suggest_findings": True,
		"tracked_apps": (), "ai_excluded_finding_types": (),
	}
	base.update(kw)
	return SimpleNamespace(**base)



def test_an_excluded_type_raises_not_eligible():
	with patch("optimus.settings.get_config", return_value=_cfg(ai_excluded_finding_types=("Slow Query",))):
		with pytest.raises(ai_fix.AiFixError) as caught:
			ai_fix.suggest_fix({"finding_type": "Slow Query", "title": "x"})
	assert caught.value.kind == "not_eligible"
	assert "listed under Excluded finding types" in str(caught.value)
	assert ai_fix.AI_SKIP_KINDS == frozenset({"not_eligible"})
