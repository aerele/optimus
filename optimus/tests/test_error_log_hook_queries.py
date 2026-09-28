# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The Error Log hook's own ``__Auth`` SELECT (its stored-key read, inside
the user's ``frappe.log_error``) as the analyzers and the session totals see
it. The analyzers recognise the hook's frame by a path suffix derived from
the hook module itself (``error_log_mask.HOOK_FRAME_SUFFIX``), so renaming
or moving the module cannot silently stop the match."""

from pathlib import Path

from optimus import error_log_mask
from optimus.analyzers import base


class TestTheHookFrameSuffix:
	def test_it_is_derived_from_the_module_and_matches_its_real_path(self):
		assert error_log_mask.HOOK_FRAME_SUFFIX == error_log_mask.__name__.replace(".", "/") + ".py"
		assert Path(error_log_mask.__file__).resolve().as_posix().endswith("/" + error_log_mask.HOOK_FRAME_SUFFIX)

	def test_the_analyzers_use_it(self):
		assert base._ERROR_LOG_HOOK_FRAME is error_log_mask.HOOK_FRAME_SUFFIX
		assert base._is_error_log_hook_frame(Path(error_log_mask.__file__).resolve().as_posix())
		assert not base._is_error_log_hook_frame("apps/optimus/optimus/error_log_mask_helpers.py")

	def test_the_analyzers_never_spell_the_path_out(self):
		source = Path(base.__file__).read_text(encoding="utf-8")
		assert '"optimus/error_log_mask.py"' not in source
