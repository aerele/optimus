# Copyright (c) 2026, Optimus contributors
# For license information, please see license.txt

"""The repo-read baseline: how many reads of a repository file each test module may have.

``test_lint_repo_reads`` scans ``optimus/tests/`` with ``repo_reads_scan`` (its docstring lists
the files it scans, what counts as a read and the blind spots) and fails when a file's count of
unmarked reads differs from ``BASELINE``, so the count can only go down. The entries are the
reads that were there when the gate came in (many are correct: doctype JSON, fixtures, corpora);
none is a licence to add one.

The rules:

- A new test file, or a new read in a listed file, fails the gate. Test the behaviour instead.
  Docs, CHANGELOG and wording pins belong only in ``test_docs_truth.py``.
- A read that is correct (a data file the code reads, a contract between a data file and the
  code) carries ``# test-lint: repo-read-ok <reason of at least 3 words>`` (see the placement
  rules in the ``repo_reads_scan`` docstring). It then leaves ``BASELINE`` and raises
  ``MARKED_TOTAL`` by one, so every new waiver is a visible line in the PR. A marker that covers
  no read is stale and fails.
- Remove a read and the gate fails until you lower the file's count (delete the key at 0). The
  failure message prints the lines to paste or delete. It never prints a higher number.
- Keys are paths relative to ``optimus/tests``, with ``/``, sorted. Only counts above 0 are listed.
- ``EXEMPT`` files are not scanned, and a marker in one is not read. Each key must be a file.
- After a rebase onto a develop that changed these counts, a red baseline may need the
  baseline owner to re-run the scan and update the counts. Never raise a count to hide a read.
"""

EXEMPT = {
	"test_docs_truth.py": "the one home for docs, CHANGELOG and wording pins",
}

BASELINE = {
	"conftest.py": 1,
	"test_action_context.py": 3,
	"test_action_entry_callsite.py": 1,
	"test_actionable_findings_split.py": 3,
	"test_ai_aerele_provider.py": 1,
	"test_ai_auto_send_help_text.py": 1,
	"test_ai_bulk_batch_cap.py": 1,
	"test_ai_claude_cli.py": 4,
	"test_ai_consent_schema.py": 1,
	"test_ai_error_log_rows.py": 1,
	"test_ai_error_log_titles.py": 5,
	"test_ai_eval_corpus.py": 4,
	"test_ai_eval_live.py": 1,
	"test_ai_eval_semgrep.py": 1,
	"test_ai_guardrails.py": 3,
	"test_ai_input_privacy.py": 1,
	"test_ai_interrupt_guard.py": 1,
	"test_ai_log_audit.py": 2,
	"test_ai_loop_fix_generation.py": 1,
	"test_ai_privacy.py": 1,
	"test_ai_prompts.py": 1,
	"test_ai_quality_workflow.py": 3,
	"test_ai_queued_only.py": 1,
	"test_ai_refresh_contracts.py": 4,
	"test_ai_refresh_retention.py": 1,
	"test_ai_refresh_schema.py": 8,
	"test_ai_refresh_settings.py": 2,
	"test_ai_refresh_sql.py": 4,
	"test_ai_spend.py": 1,
	"test_ai_subscription_cli.py": 3,
	"test_analyze_concurrency.py": 2,
	"test_analyze_memory.py": 1,
	"test_analyze_singleflight_heartbeat.py": 1,
	"test_api_ai_endpoints.py": 2,
	"test_api_gate_audit.py": 3,
	"test_api_session_actions.py": 2,
	"test_api_session_lifecycle.py": 1,
	"test_auto_arm_phase2.py": 1,
	"test_background_job_label.py": 2,
	"test_bg_job_phase1.py": 2,
	"test_boot_session.py": 2,
	"test_bump_cache_threshold_patch.py": 1,
	"test_call_tree_findings.py": 2,
	"test_call_tree_reconcile.py": 1,
	"test_call_tree_scroll.py": 1,
	"test_ci_skip_guard.py": 1,
	"test_collapsible_sections.py": 1,
	"test_end_to_end_metrics.py": 1,
	"test_error_log_hook_queries.py": 1,
	"test_error_log_mask.py": 1,
	"test_export_session_v3.py": 1,
	"test_finding_types_allowlist.py": 2,
	"test_frontend_assets.py": 24,
	"test_frontend_timings_analyzer.py": 1,
	"test_frontend_vitals_observer.py": 1,
	"test_hook_entry_registry.py": 2,
	"test_index_ai_removed.py": 2,
	"test_index_recipes.py": 1,
	"test_index_suggestions_db_calls.py": 7,
	"test_infra_pressure_analyzer.py": 1,
	"test_js_translation_keys.py": 2,
	"test_line_profile_auto_arm_skips.py": 1,
	"test_line_profile_monitoring.py": 1,
	"test_line_profile_picker.py": 1,
	"test_line_profile_source_capture.py": 1,
	"test_line_profiling_wording.py": 5,
	"test_maintenance_scrub.py": 3,
	"test_optimus_session_layout.py": 2,
	"test_pass_through_registry.py": 2,
	"test_pdf_export.py": 1,
	"test_permissions_file_hook.py": 1,
	"test_phase2_jobs.py": 1,
	"test_pill_state_js.py": 2,
	"test_queries_per_action_function_column.py": 1,
	"test_query_table_layout.py": 1,
	"test_ratelimit.py": 1,
	"test_redis_audit.py": 2,
	"test_regenerate_dialog_text.py": 1,
	"test_remove_dedicated_codex_home_patch.py": 1,
	"test_rename_phase_two_doctype_patch.py": 1,
	"test_render_drilldown.py": 1,
	"test_renderer_structure_snapshot.py": 1,
	"test_report_data_schema.py": 1,
	"test_retry_phase2_batch.py": 3,
	"test_review_fixes.py": 1,
	"test_safe_call.py": 3,
	"test_session_form_failure_reason.py": 1,
	"test_session_ready_popup.py": 1,
	"test_set_hide_framework_tables_default_patch.py": 2,
	"test_settings_intro_banner.py": 1,
	"test_settings_profile.py": 1,
	"test_sql_redaction_frappe_corpus.py": 1,
	"test_steps_to_reproduce.py": 1,
	"test_submit_frontend_metrics.py": 1,
	"test_timing_relations.py": 2,
	"test_truncation_banner.py": 1,
	"test_uuid_escaped_in_errors.py": 1,
	"test_waterfall.py": 1,
}

# Valid ``# test-lint: repo-read-ok`` markers in the scanned tree.
MARKED_TOTAL = 0
