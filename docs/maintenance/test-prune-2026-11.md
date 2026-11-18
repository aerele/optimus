# Test-prune campaign, 2026-11: traceability index

This campaign removed low-value unit tests from the Optimus suite, lane by lane. A test went only when a named keeper test, or a deliberate "no contract" finding, covered what it checked. Protected files and carried checks were proved by mutating the production code and watching a kept test turn red. Protected security files, the counting rule and the retention bar kept most tests in place. Each deleted test is listed below with its keeper, so any removal can be traced and reversed.

- Base: `d51a4c6` (develop).
- Head: the head of the PR branch (`chore/prune-test-suite`) at the time of writing.
- Declarations: 7,333 at the campaign baseline `852fe17`; 7,338 at develop `d51a4c6` (#155 added 5); 6,657 now. One counting method for all three: `grep -cE "^\s*(async )?def test_"` over `optimus/tests/test_*.py`.
- Collected tests: 11,675 to 10,974, as measured.
- Count toward the 12 % target: 594 of 880. The target was not reached. The cause is the three rules below, and the ledgers show that more volume would come only from tests those rules keep. Protected security files (46 files, 1,042 declarations, plus about 40 tests marked as security contracts) were cut only where a mutation proved a keeper. The counting rule gives 0 for a carried assertion, a parametrize fold, a rename or a move. The retention bar kept about 47 borderline tests that the controller moved from D back to R.
- Suite time: 237 s to 196 s.
- Coverage: lines 92.99 % to 93.11 %, branches 89.79 % to 89.93 %.

The index lists 715 declarations that no longer exist under their old name (AST diff of `d51a4c6` against the working tree, every file under `optimus/tests/`). Of these, 615 are D (deleted), 71 are C (deleted after the check was carried into a keeper), 11 are fold (merged into a parametrized keeper), 14 are rename and 4 are move. Only D rows count toward the 594, less the D rows whose keeper needed a new assertion and the preservation fixes (see the lane table).

## Recover a deleted test

1. Print the old file: `git show d51a4c6:optimus/tests/<file>`. A whole deleted file is recovered the same way.
2. Find the row: search this file for the test name. The row gives the mark, the keeper that now holds the check, and the lane.
3. Find the lane commit in the table below, or run `git log --oneline d51a4c6..HEAD -- optimus/tests/<file>`.
4. To restore a test, copy its function from the old file into the current file. Run the keeper first, then mutate the production code to see what the old test caught.

Marks: D = deleted, a keeper holds the contract or the finding is "no contract". C = carried: the assertion was moved into the keeper and the test deleted. fold = merged into one parametrized keeper (new nodeid given). rename = same test under a new name (new nodeid given; ledger mark F, repaired so it can fail). move = same test in another file. The keeper cell shows the first nodeid, then `(+N)`. A keeper cell with plain text names a class or a file, or says what holds the behaviour.

## Lane to commit

| Lane | Scope | Counted | Commit |
|---|---|---:|---|
| L1 | Repo-meta (docs, CHANGELOG, assets, review rounds, structure snapshot, CI guard, ai_eval, docs truth) | 31 | `13c7af0` |
| L2 | Analyze core (analyze.run, singleflight, background wait, realtime, cleanup) | 45 | `4ca38f4` |
| L3 | Frontend JS (public/js, doctype .js, JS harness tests) | 11 | `84c80bf` |
| L4 | Renderer and report template | 134 | `1e27855` |
| merge | Merge of develop `d51a4c6` (#155) into the branch | 0 | `022454c` |
| L5 | Capture, session, hooks, api | 66 | `c43bf3f` |
| L6 | line_profile (phase 2) | 29 | `a0acaa1` |
| L7 | Settings, install, patches, janitor, redis keys, maintenance | 41 | `8067634` |
| L8a | AI: ai_fix, providers, subscription CLI | 139 | `4afde67` |
| L8b | AI: ai_jobs, ai_refresh_* | 27 | `407b037` |
| L8c | AI: guardrails, privacy | 18 | `86492fb` |
| L9a | Analyzers: index recipes, index advice, index suggestions | 13 | `ac303fa` |
| L9b | Analyzers: all others, SQL dialects | 25 | `ed1b2e8` |
| L10 | Redaction, scrub, error-log mask | 13 | `3c83d50` |
| L5d | Deferred file `test_api_pill_state.py` (after #155 merged) | 8 | `e00d3fe` |
| fix | Preservation-review fixes (6 removals now covered by kept tests, counts 0) | -6 | `827ee11` |
| Total | | 594 | |

Still deferred: `test_api_phase2_endpoints.py`, `test_auto_arm_phase2.py`, `test_phase2_jobs.py`, `test_session_action_gate.py`, `test_phase2_queue_ui.py`, `test_session_form_buttons.py` (PRs #156 and #157 open).

## Production code removed

Each name had no caller outside `optimus/tests/`. The matching tests were deleted with it, or repointed to the real function first.

| Name | File | Why |
|---|---|---|
| `_FINDING_TYPE_HINTS` | `optimus/ai_fix.py` | alias of `ai_prompts.FINDING_TYPE_HINTS`; only a vacuous test read it |
| `_build_messages` | `optimus/ai_fix.py` | wrapper of `_build_fix_request`; callers moved to `_build_fix_request` |
| `_pg_milliseconds` | `optimus/ai_jobs.py` | alias of `sql_work.lock_timeout_ms`; its test now calls `lock_timeout_ms` |
| `_rq_job_active` | `optimus/analyze.py` | only a test called it; `_rq_status` error path carried into a keeper |
| `_own_source` | `optimus/analyze.py` | wrapper of `_frame_source`; only tests called it (dead guard removed) |
| `own_query_frame` (import) | `optimus/analyze.py` | unused after `_own_source` went; the name stays in `analyzers/base.py` |
| `redact_string_value` | `optimus/redaction.py` | no caller; `_redact_string` stays and is tested through its callers |
| `_build_executive_summary` | `optimus/renderer/_internal.py` | orphaned data layer, not wired into the report since v0.7.x |
| `is_server_script_filename` | `optimus/server_script_source.py` | no non-test caller |
| `_SERVER_SCRIPT_PREFIX` | `optimus/server_script_source.py` | used only by `is_server_script_filename` |

Also edited: `docs/AI-FIXING.md` (one row, `_build_messages` wrapper), `docs/HMAC-ENVELOPE.md` (test counts), `optimus/renderer/__init__.py` (a comment).

Kept on purpose, although no code calls them or only tests do:

- `ai_guardrails.stored_sql_violations`: kept seam: nothing calls it now (follow-up 1).
- `redis_keys.ai_refresh_inflight`: documented refresh lock; `KEY_PATTERNS` and `docs/REDIS-SCHEMA.md` still describe it.
- `analyze._ai_call_timeout`, `analyze._note_ai_selection`: callers removed in `e4db355`; kept seams (follow-ups 3, 4).
- `api._ai_session_gate`: kept seam: the live refill never runs the check (follow-up 5).
- `index_recipes.HOOK_EVENTS`, `_ENSURE_FUNCTION`, `_valued`: test-only names, not on the plan.
- `settings.read_schema_sentinel`: documented future hook, not on the plan.
- `analyze._session_increment_query(by=)` parameter: test-only parameter, not on the plan.
- `error_log_mask._TITLE_LIMIT`: off-plan constant, kept.

## Index

One table per test file, in path order. Node ids are `optimus/tests/<file>::<Class>::<test>`. The keeper column holds nodeids that exist at the head commit.

### test_action_context.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_action_context.py::TestModuleFromFilename::test_frappe_core_path` | D | `optimus/tests/test_action_context.py::TestModuleFromFilename::test_app_relative_path` | L4 |
| `optimus/tests/test_action_context.py::TestAttachActionContext::test_resolved_used_when_form_dict_has_no_name` | D | `optimus/tests/test_action_context.py::TestAttachActionContext::test_temp_name_resolved_from_recording_resolved_target_doc` (+1) | L4 |
| `optimus/tests/test_action_context.py::TestResolvedDocSidecarWiring::test_after_request_writes_resolved_doc_to_sidecar` | D | `optimus/tests/test_hooks_request_job_flow.py::TestAfterRequest::test_new_doc_name_from_the_response_is_stashed_for_analyze` | L4 |
| `optimus/tests/test_action_context.py::TestResolvedDocSidecarWiring::test_after_request_does_not_rmw_recorder_hash_for_resolved_doc` | D | `optimus/tests/test_hooks_request_job_flow.py::TestAfterRequest::test_new_doc_name_from_the_response_is_stashed_for_analyze` | L4 |

### test_action_entry_callsite.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_action_entry_callsite.py::TestActionEntryCallsite::test_resolves_http_api_method_action` | D | `optimus/tests/test_action_entry_callsite.py::TestActionEntryCallsite::test_resolves_background_job_action` (+1) | L4 |
| `optimus/tests/test_action_entry_callsite.py::TestActionEntryCallsite::test_none_for_empty_path` | D | `optimus/tests/test_action_entry_callsite.py::TestActionDottedEntry::test_empty_missing_or_bad_input` (+1) | L4 |
| `optimus/tests/test_action_entry_callsite.py::TestActionEntryCallsite::test_none_when_dotted_resolves_to_a_module` | D | `optimus/tests/test_action_entry_callsite.py::TestResolveDottedToCode::test_none_when_path_is_a_module_not_a_function` (+1) | L4 |
| `optimus/tests/test_action_entry_callsite.py::TestActionEntryCallsite::test_none_for_builtin_no_code_object` | D | `optimus/tests/test_action_entry_callsite.py::TestResolveDottedToCode::test_none_for_builtin_without_code_object` (+1) | L4 |
| `optimus/tests/test_action_entry_callsite.py::TestSkipDecoratorsToDef::test_single_decorator_is_skipped_to_def` | D | `optimus/tests/test_action_entry_callsite.py::TestSkipDecoratorsToDef::test_multi_line_decorator_args_walked_over` | L4 |

### test_action_plan.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_action_plan.py::TestSizing::test_returns_fewer_when_input_is_smaller` | D | `optimus/tests/test_action_plan.py::TestStepShape::test_step_carries_position_gain_and_callsite` | L4 |

### test_actionable_findings_split.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_actionable_findings_split.py::test_render_splits_findings_into_actionable_and_observational` | D | `optimus/tests/test_app_bucketing_render.py::test_observations_also_bucketed_by_app` (+1) | L4 |
| `optimus/tests/test_actionable_findings_split.py::test_template_has_observations_subsection_inside_findings` | D | `optimus/tests/test_collapsible_sections.py::test_observations_is_nested_inside_findings` | L4 |

### test_aggregate_hot_frames.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_aggregate_hot_frames.py::test_framework_variant_displays_cumulative_time` | D | `optimus/tests/test_hot_frames_self_time_display.py::TestBuilder::test_framework_rows_unchanged` | L4 |
| `optimus/tests/test_aggregate_hot_frames.py::test_hot_frame_display_name_has_no_placeholder_suffix` | D | `optimus/tests/test_hot_frames_self_time_display.py::TestRendered::test_new_row_shows_total_and_self` | L4 |

### test_ai_aerele_provider.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_aerele_provider.py::TestAereleProviderDisabled::test_aerele_not_in_provider_select_options` | D | `optimus/tests/test_ai_aerele_provider.py::TestProviderSelectMatchesDefaults::test_select_options_match_provider_defaults` | L8a |

### test_ai_auto_send_help_text.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_auto_send_help_text.py::test_auto_suggest_is_on_by_default` | D | `optimus/tests/test_settings_ai.py::TestAiConfigDefaults::test_defaults` (+1) | L7 |
| `optimus/tests/test_ai_auto_send_help_text.py::test_modified_is_bumped_so_migrate_reloads_the_descriptions` | D | no contract | L7 |

### test_ai_claude_cli.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_claude_cli.py::TestDiscriminator::test_switching_the_field_flips_the_provider_identity` | D | `optimus/tests/test_ai_claude_cli.py::TestDiscriminator::test_a_queued_job_is_refused_once_the_saved_cli_changed` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestDiscriminator::test_a_snapshot_that_still_says_claude_is_valid` | D | `optimus/tests/test_ai_subscription_cli.py::TestProvider::test_dispatch_proceeds_when_the_snapshot_still_matches` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestSettingsOptions::test_every_backend_display_name_is_its_option_label` | D | no contract | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestInstalledCheck::test_a_writable_native_target_is_rejected_as_untrusted` | D | `optimus/tests/test_ai_subscription_cli.py::TestCliInstalled::test_group_or_world_writable_file_is_rejected` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestInstalledCheck::test_a_target_owned_by_someone_else_is_rejected` | D | `optimus/tests/test_ai_subscription_cli.py::TestCliInstalled::test_file_owned_by_someone_else_is_rejected` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestInstalledCheck::test_nothing_on_path_is_not_found` | D | `optimus/tests/test_ai_subscription_cli.py::TestCliResolve::test_nothing_on_path_is_not_found` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestSandbox::test_the_sandbox_neither_creates_nor_tightens_the_operators_claude_dir` | D | `optimus/tests/test_ai_subscription_cli.py::TestCliSandbox::test_the_sandbox_neither_creates_nor_tightens_the_operators_codex_dir` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestSandbox::test_anthropic_and_provider_secrets_never_reach_the_child_env` | D | `optimus/tests/test_ai_subscription_cli.py::TestCliEnv::test_the_secret_and_provider_variables_are_stripped_from_the_child` (+1) | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestArgv::test_never_bare_bypass_budget_or_a_model_by_default` | D | `optimus/tests/test_ai_claude_cli.py::TestArgv::test_the_isolation_flags_are_exactly_the_planned_ones` (+1) | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestArgv::test_no_shell_metacharacters_and_no_prompt_on_argv` | D | `optimus/tests/test_ai_claude_cli.py::TestExec::test_the_prompt_goes_on_stdin_and_never_on_argv` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestArgv::test_the_probe_budget_is_ignored_because_claude_has_no_features_probe` | D | `optimus/tests/test_ai_claude_cli.py::TestExec::test_no_features_probe_runs_for_claude` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestStatusIsPiiFree::test_the_stored_connect_message_is_the_status_message_only` | D | `optimus/tests/test_ai_claude_cli.py::TestStatusIsPiiFree::test_connected_status_carries_no_account_detail_even_when_the_cli_prints_it` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestStatusIsPiiFree::test_only_the_two_fields_are_read` | D | `optimus/tests/test_ai_claude_cli.py::TestLoginCheck::test_a_subscription_login_is_connected` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestSessionStatus::test_blocked_when_the_guard_is_off` | D | `optimus/tests/test_ai_subscription_cli.py::TestSessionStatus::test_blocked_when_guard_off` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestSessionStatus::test_availability_needs_the_guard_and_an_installed_claude` | D | `optimus/tests/test_ai_subscription_cli.py::TestAvailability::test_needs_guard_and_cli` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestExec::test_the_guard_off_refuses_before_any_process` | D | `optimus/tests/test_ai_subscription_cli.py::TestCallLocalCli::test_guard_off_refuses_before_any_process` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestExec::test_a_success_counts_one_known_zero_cost_call` | D | `optimus/tests/test_ai_subscription_cli.py::TestCallLocalCli::test_success_counts_one_known_zero_cost_call` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestExec::test_a_timeout_kills_the_whole_process_group_and_is_not_fatal` | D | `optimus/tests/test_ai_subscription_cli.py::TestCallLocalCli::test_timeout_kills_the_whole_process_group_and_is_not_fatal` (+1) | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestExec::test_the_exec_failure_is_logged_with_event_version_label_and_code_only` | D | `optimus/tests/test_ai_subscription_cli.py::TestCallLocalCli::test_exec_failure_is_logged_with_event_version_label_and_code_only` (+1) | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestFailuresInTheReply::test_a_403_is_a_quota_stop_only_when_its_text_says_so` | D | `optimus/tests/test_ai_claude_cli.py::TestFailuresInTheReply::test_an_is_error_result_is_a_classified_error_whatever_the_exit_code` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestFailuresInTheReply::test_a_context_limit_text_is_the_per_prompt_context_label_without_a_status` | D | `optimus/tests/test_ai_claude_cli.py::TestFailuresInTheReply::test_an_is_error_result_is_a_classified_error_whatever_the_exit_code` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestFailuresInTheReply::test_an_is_error_result_with_exit_zero_is_never_returned_as_a_suggestion` | D | `optimus/tests/test_ai_claude_cli.py::TestFailuresInTheReply::test_an_is_error_result_is_a_classified_error_whatever_the_exit_code` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestFailuresInTheReply::test_a_usage_limit_is_a_fatal_quota_error_that_stops_a_bulk` | D | `optimus/tests/test_ai_claude_cli.py::TestFailuresInTheReply::test_an_is_error_result_is_a_classified_error_whatever_the_exit_code` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestFailuresInTheReply::test_the_truncation_marker_survives_an_unterminated_pem_block` | D | `optimus/tests/test_ai_subscription_cli.py::TestCallLocalCli::test_truncation_marker_survives_an_unterminated_pem_block` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestUnfinishedReplies::test_suggest_fix_never_surfaces_a_refusal_or_an_aborted_stream` | D | `optimus/tests/test_ai_claude_cli.py::TestUnfinishedReplies::test_an_unfinished_reply_is_a_classified_failure_not_a_suggestion` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestFailureLog::test_the_result_text_never_reaches_the_line_or_the_error` | D | `optimus/tests/test_ai_claude_cli.py::TestFailureLog::test_the_line_carries_the_specific_label_status_and_terminal` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestFailureLog::test_a_codex_failure_line_is_unchanged` | D | `optimus/tests/test_ai_subscription_cli.py::TestInterruptsAreNotSwallowed::test_diagnostic_carries_only_the_whitelisted_fields` (+1) | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestKindsAndBulk::test_the_refresh_plan_is_bounded_with_the_claude_choice` | D | `optimus/tests/test_ai_bulk_batch_cap.py::test_the_refresh_plan_shows_the_bounded_cap_in_subscription_mode` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestBackendThreading::test_a_direct_call_without_a_key_still_follows_the_cached_settings` | D | `optimus/tests/test_ai_claude_cli.py::TestLoginCheck::test_the_dispatch_uses_the_claude_check_for_the_claude_choice` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestBackendThreading::test_the_provider_carries_the_key_the_dispatch_passes_on` | D | `optimus/tests/test_ai_claude_cli.py::TestBackendThreading::test_the_exec_runs_the_backend_the_validated_provider_names_not_the_cached_settings` | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestSharedShellIsBackendNeutral::test_the_helpers_the_claude_path_calls_take_the_backend_without_a_codex_default` | D | `optimus/tests/test_ai_claude_cli.py::TestSharedShellIsBackendNeutral::test_the_claude_login_failure_carries_no_codex_wording` (+1) | L8a |
| `optimus/tests/test_ai_claude_cli.py::TestDocs::test_the_changelog_has_an_entry_for_the_current_version` | C | `optimus/tests/test_docs_truth.py::test_the_changelog_has_an_entry_for_the_current_version` | L8a |

### test_ai_error_log_rows.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_error_log_rows.py::test_the_row_text_carries_attempts_and_dropped` | D | `optimus/tests/test_ai_error_log_rows.py::test_a_real_ladder_failure_writes_attempts_and_dropped_into_the_error_log_row` | L8a |
| `optimus/tests/test_ai_error_log_rows.py::test_operators_are_told_to_search_for_the_title_a_provider_failure_row_has` | C | `optimus/tests/test_ai_error_log_titles.py::test_every_pointer_names_the_prefix` | L8a |

### test_ai_error_log_titles.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_error_log_titles.py::test_the_docs_say_to_search_for_the_prefix` | D | no contract; behaviour in `optimus/tests/test_ai_error_log_titles.py::test_every_pointer_names_the_prefix` | L8a |
| `optimus/tests/test_ai_error_log_titles.py::test_the_runbook_spells_fatal_as_the_rows_do` | D | `optimus/tests/test_ai_error_log_rows.py::test_the_runbook_has_a_row_per_kind_and_marks_the_fatal_ones` | L8a |

### test_ai_eval_live.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_eval_live.py::test_main_refuses_existing_output_directory` | D | `optimus/tests/test_ai_eval_live.py::test_main_refuses_before_touching_frappe` | L1 |
| `optimus/tests/test_ai_eval_live.py::test_run_case_gates_pre_l5_redundant_calls_with_the_real_gate` | D | `optimus/tests/test_ai_eval_live.py::test_run_case_outcomes` (+1) | L1 |

### test_ai_eval_report.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_eval_report.py::test_ddl_in_a_bash_block_scores_harmful_as_code` | D | `optimus/tests/test_ai_eval_answer.py::test_index_ddl_counts_as_code_in_any_block` (+1) | L1 |
| `optimus/tests/test_ai_eval_report.py::test_empty_bar_cannot_pass` | C | `optimus/tests/test_ai_eval_report.py::test_bar_rules` | L1 |

### test_ai_eval_semgrep.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_eval_semgrep.py::test_scanner_skips_moved_lines_and_flags_new_ones` | C | `optimus/tests/test_ai_eval_semgrep.py::test_moved_or_quoted_lines_are_not_introduced` | L1 |

### test_ai_fix.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_fix.py::TestBuildMessages::test_returns_system_and_single_user_message` | D | `optimus/tests/test_ai_fix.py::TestBuildMessages::test_system_prompt_is_the_static_v2_prompt` | L8a |
| `optimus/tests/test_ai_fix.py::TestBuildMessages::test_handles_minimal_finding_without_detail` | D | `optimus/tests/test_ai_fix.py::TestSuggestFix::test_missing_model_raises_before_any_http` | L8a |
| `optimus/tests/test_ai_fix.py::TestOpenAiCall::test_content_as_list_of_parts` | D | `optimus/tests/test_ai_fix.py::TestOpenAiCall::test_content_list_with_a_none_or_non_str_text_keeps_the_rest` | L8a |
| `optimus/tests/test_ai_fix.py::TestOpenAiCall::test_retries_without_temperature_on_400_temperature_error` | D | `optimus/tests/test_ai_error_kinds.py::test_temperature_is_dropped_before_max_tokens_is_renamed` (+1) | L8a |
| `optimus/tests/test_ai_fix.py::TestOpenAiCall::test_non_temperature_400_is_not_retried` | D | `optimus/tests/test_ai_reliability_core.py::test_context_failure_never_enters_parameter_retry` (+1) | L8a |
| `optimus/tests/test_ai_fix.py::TestUsageNormalization::test_openai_passthrough` | D | `optimus/tests/test_ai_fix.py::TestSuggestFix::test_includes_tokens_when_usage_present` | L8a |
| `optimus/tests/test_ai_fix.py::TestUsageNormalization::test_openai_total_falls_back_to_sum` | D | `optimus/tests/test_ai_fix.py::test_usage_total_preserves_the_sum_of_valid_counts` (+1) | L8a |
| `optimus/tests/test_ai_fix.py::TestUsageNormalization::test_anthropic_maps_input_output` | D | `optimus/tests/test_ai_fix.py::test_usage_total_preserves_the_sum_of_valid_counts` (+1) | L8a |
| `optimus/tests/test_ai_fix.py::TestUsageNormalization::test_missing_usage_is_zero` | D | `optimus/tests/test_ai_fix.py::TestUsageNormalization::test_malformed_usage_is_zero_and_never_raises` | L8a |
| `optimus/tests/test_ai_fix.py::TestAnthropicCall::test_suggest_fix_reports_a_non_string_text_as_an_empty_response` | D | `optimus/tests/test_ai_fix.py::TestAnthropicEndToEnd::test_blank_text_raises_bad_response_with_usage` (+1) | L8a |
| `optimus/tests/test_ai_fix.py::TestHttpErrorMapping::test_timeout` | D | `optimus/tests/test_ai_error_kinds.py::test_a_timeout_names_the_whole_budget_not_what_was_left` | L8a |
| `optimus/tests/test_ai_fix.py::TestHttpErrorMapping::test_connection_error` | D | `optimus/tests/test_ai_error_kinds.py::test_a_transport_failure_is_translated_and_names_only_the_error_type` | L8a |
| `optimus/tests/test_ai_fix.py::TestHttpErrorMapping::test_generic_http_error_includes_body_detail` | D | `optimus/tests/test_ai_error_kinds.py::test_status_messages_are_translated_with_a_next_step` (+1) | L8a |
| `optimus/tests/test_ai_fix.py::TestHttpErrorMapping::test_non_json_body` | D | `optimus/tests/test_ai_error_kinds.py::test_an_unusable_reply_is_translated_with_a_next_step` | L8a |
| `optimus/tests/test_ai_fix.py::TestResolveProvider::test_deepseek_ignores_base_url_override` | D | `optimus/tests/test_ai_fix.py::TestResolveProvider::test_base_url_override_ignored_for_hosted_provider` | L8a |
| `optimus/tests/test_ai_fix.py::TestSuggestFix::test_happy_path_returns_payload` | D | `optimus/tests/test_ai_fix.py::TestGuardedCompletion::test_clean_answer_is_not_reasked_and_result_has_the_contract_keys` (+1) | L8a |
| `optimus/tests/test_ai_fix.py::TestSuggestFix::test_anthropic_dispatch` | D | `optimus/tests/test_ai_fix.py::TestAnthropicEndToEnd::test_clean_answer_end_to_end` | L8a |
| `optimus/tests/test_ai_fix.py::TestSuggestFix::test_empty_response_raises` | D | `optimus/tests/test_ai_error_kinds.py::test_an_empty_reply_says_what_to_do` | L8a |
| `optimus/tests/test_ai_fix.py::TestHumanizeSteps::test_humanize_steps_empty_response_raises` | D | `optimus/tests/test_ai_error_kinds.py::test_an_empty_reply_says_what_to_do` | L8a |
| `optimus/tests/test_ai_fix.py::test_eligible_finding_types_is_a_frozenset_of_known_types` | D | `optimus/tests/test_ai_recipe_eligibility.py::TestEligibleSet::test_exact_eligible_set` | L8a |
| `optimus/tests/test_ai_fix.py::TestIsAvailableSection::test_all_sections_on_when_config_valid` | D | `optimus/tests/test_ai_fix.py::TestIsAvailableSection` | L8a |
| `optimus/tests/test_ai_fix.py::TestCallChokepointCollectsUsage::test_openai_call_records_total_tokens` | D | `optimus/tests/test_ai_fix.py::TestOpenAiCall::test_populates_usage_out_when_provided` | L8a |
| `optimus/tests/test_ai_fix.py::TestCallChokepointCollectsUsage::test_anthropic_call_records_total_tokens` | D | `optimus/tests/test_ai_fix.py::TestAnthropicEndToEnd::test_clean_answer_end_to_end` (+1) | L8a |
| `optimus/tests/test_ai_fix.py::TestGuardedCompletion::test_over_budget_first_call_skips_reask` | D | `optimus/tests/test_ai_fix.py::TestGuardedCompletion::test_reask_needs_under_half_the_budget` | L8a |
| `optimus/tests/test_ai_fix.py::TestPromptBlocksWired::test_blocks_are_in_the_system_prompt` | C | `optimus/tests/test_ai_fix.py::TestBuildMessages::test_system_prompt_is_the_static_v2_prompt` | L8a |
| `optimus/tests/test_ai_fix.py::TestPromptBlocksWired::test_system_prompt_within_char_budget` | D | `optimus/tests/test_ai_prompts.py::test_system_prompt_fits_the_char_budget` | L8a |

### test_ai_guardrails.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_guardrails.py::test_actions_are_the_four_known_values` | D | `optimus/tests/test_ai_guardrails.py::test_tiers_are_exactly_the_agreed_sets` | L8c |

### test_ai_hook_runbook.py (file deleted)

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_hook_runbook.py::test_documented_hook_check_accepts_tuple_doc_events` | C | `optimus/tests/test_docs_truth.py::test_documented_hook_check_command_matches_the_registered_hook` | L1 |
| `optimus/tests/test_ai_hook_runbook.py::test_the_three_documents_agree` | D | no contract | L1 |
| `optimus/tests/test_ai_hook_runbook.py::test_no_document_still_says_a_redirect_is_never_followed` | D | no contract | L1 |

### test_ai_html_sanitizer.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_html_sanitizer.py::test_allowed_host_with_path_keeps_href` | D | `optimus/tests/test_ai_html_sanitizer.py::test_allowed_links_are_kept` (+1) | L8c |
| `optimus/tests/test_ai_html_sanitizer.py::test_github_frappe_subpath_keeps_href` | D | `optimus/tests/test_ai_html_sanitizer.py::test_allowed_links_are_kept` | L8c |
| `optimus/tests/test_ai_html_sanitizer.py::test_github_non_frappe_path_is_rejected` | D | `optimus/tests/test_ai_html_sanitizer.py::test_attribute_filter_agrees_with_ai_link_allowed` (+1) | L8c |
| `optimus/tests/test_ai_html_sanitizer.py::test_subdomain_spoof_is_rejected` | D | `optimus/tests/test_ai_html_sanitizer.py::test_attribute_filter_agrees_with_ai_link_allowed` (+1) | L8c |
| `optimus/tests/test_ai_html_sanitizer.py::test_host_prefix_spoof_is_rejected` | D | `optimus/tests/test_ai_html_sanitizer.py::test_attribute_filter_agrees_with_ai_link_allowed` (+1) | L8c |
| `optimus/tests/test_ai_html_sanitizer.py::test_whitespace_control_and_non_ascii_are_rejected` | D | `optimus/tests/test_ai_html_sanitizer.py::test_attribute_filter_agrees_with_ai_link_allowed` | L8c |
| `optimus/tests/test_ai_html_sanitizer.py::test_non_http_scheme_is_rejected` | D | `optimus/tests/test_ai_html_sanitizer.py::test_attribute_filter_agrees_with_ai_link_allowed` | L8c |

### test_ai_interrupt_guard.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_interrupt_guard.py::TestTheGuard::test_nothing_recorded_raises_nothing` | D | `optimus/tests/test_ai_interrupt_guard.py::TestTheGuard::test_without_base_a_non_exception_interrupt_goes_through` (+1) | L8a |

### test_ai_key_handling.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_key_handling.py::TestAiFixErrorShape::test_carries_kind_and_status` | D | `optimus/tests/test_ai_fix.py::TestProviderContext::test_too_small_window_fails_before_any_http` | L8a |
| `optimus/tests/test_ai_key_handling.py::TestRequestsCarryTheKeyOnlyInAuth::test_non_latin_key_fails_before_any_http` | D | `optimus/tests/test_ai_key_handling.py::TestAKeylessProviderIgnoresAnUnsendableKey::test_a_provider_that_needs_a_key_still_rejects_it` | L8a |

### test_ai_log_audit.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_log_audit.py::test_the_helper_is_used_where_the_skeleton_was` | D | no contract; behaviour in `optimus/tests/test_ai_log_audit.py::test_no_logging_inside_except_handlers_on_the_ai_surface` (+1) | L8a |
| `optimus/tests/test_ai_log_audit.py::test_scanned_modules_and_wrappers_exist` | D | no contract | L8a |

### test_ai_log_failure.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_log_failure.py::TestLogAiFailure::test_no_breadcrumb_when_the_row_is_written` | D | `optimus/tests/test_ai_log_failure.py::TestLogAiFailure::test_no_breadcrumb_when_the_requeue_works_or_is_not_needed` | L8a |
| `optimus/tests/test_ai_log_failure.py::TestLogAiFailure::test_an_exception_is_logged_once` | D | `optimus/tests/test_ai_error_log_rows.py::test_a_caller_logging_the_same_failure_again_adds_no_second_row_and_no_duplicate_lines` | L8a |
| `optimus/tests/test_ai_log_failure.py::TestHttpFailurePath::test_credentials_in_the_base_url_are_refused_before_http` | D | `optimus/tests/test_ai_url_security.py::test_refuses_unsafe_url_without_echo` (+1) | L8a |
| `optimus/tests/test_ai_log_failure.py::TestHttpFailurePath::test_http_error_row_never_contains_the_response_body` | D | `optimus/tests/test_ai_transport_security.py::test_provider_body_is_only_in_admin_detail_never_public_error_or_log` | L8a |
| `optimus/tests/test_ai_log_failure.py::TestHttpFailurePath::test_http_row_references_the_explicit_session` | D | `optimus/tests/test_ai_spend.py::test_an_http_failure_row_is_filed_under_the_calls_own_session` (+1) | L8a |
| `optimus/tests/test_ai_log_failure.py::TestARedirectIsNeverFollowed::test_a_302_to_another_host_is_a_bad_response_and_the_key_never_reaches_it` | D | `optimus/tests/test_ai_log_failure.py::TestOnlyASameOriginRedirectIsFollowed::test_any_other_redirect_is_a_bad_response_and_the_key_stays_home` | L8a |
| `optimus/tests/test_ai_log_failure.py::TestOnlyASameOriginRedirectIsFollowed::test_three_redirects_then_the_reply` | D | `optimus/tests/test_ai_log_failure.py::TestOnlyASameOriginRedirectIsFollowed::test_at_most_three_redirects_are_followed` (+1) | L8a |
| `optimus/tests/test_ai_log_failure.py::TestAJobTimeoutIsNeverSwallowed::test_log_http_error_does_not_read_a_worker_local_session_marker` | D | no contract; behaviour in `optimus/tests/test_ai_spend.py::test_an_http_failure_row_is_filed_under_the_calls_own_session` (+1) | L8a |
| `optimus/tests/test_ai_log_failure.py::TestLogAiStepFailure::test_threads_the_title_and_the_session` | D | `optimus/tests/test_ai_log_failure.py::TestLogAiStepFailure::test_writes_one_referenced_row` | L8a |
| `optimus/tests/test_ai_log_failure.py::TestAPassedInJobTimeoutStopsTheJob::test_a_timeout_stops_an_ai_loop_after_its_first_item` | D | `optimus/tests/test_ai_log_failure.py::TestAPassedInJobTimeoutStopsTheJob::test_it_is_logged_then_raised_fresh` | L8a |
| `optimus/tests/test_ai_log_failure.py::TestAPassedInJobTimeoutStopsTheJob::test_other_errors_still_never_raise` | D | `optimus/tests/test_ai_log_failure.py::TestLogAiFailure::test_returns_true_only_once_log_error_returned` (+1) | L8a |
| `optimus/tests/test_ai_log_failure.py::TestARaisedAgainTimeoutIsLoggedOnce::test_a_failed_write_still_leaves_its_breadcrumb` | C | `optimus/tests/test_ai_log_failure.py::TestARaisedAgainTimeoutIsLoggedOnce::test_a_timeout_that_was_not_written_is_not_marked` | L8a |

### test_ai_loop_facts.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_loop_facts.py::test_prompt_version_bumped` | D | `optimus/tests/test_ai_prompts.py::test_prompt_version_is_4` | L8a |

### test_ai_output_recovery.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_output_recovery.py::test_deepseek_uses_requested_output_budget` | D | `optimus/tests/test_ai_fix.py::TestGuardedCompletion::test_compact_completion_budget_and_provider_options` | L8a |
| `optimus/tests/test_ai_output_recovery.py::test_budget_uses_remaining_context_and_margin` | C | `optimus/tests/test_ai_budget.py::test_output_tokens_reserves_context_margin_without_quality_cap` | L8a |

### test_ai_payload_grounding_window.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_payload_grounding_window.py::test_decorators_and_the_outer_function_are_included` | D | `optimus/tests/test_ai_grounding.py::TestGroundingWindow::test_decorators_and_the_outer_function_are_included` | L8c |

### test_ai_pre_callsite_fix_gate.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_pre_callsite_fix_gate.py::TestPreL5Gate::test_any_other_stamp_value_is_pre_fix` | D | `optimus/tests/test_ai_grounding_gate.py::test_callsite_stamp_is_exact_in_both_finding_shapes` (+1) | L8c |
| `optimus/tests/test_ai_pre_callsite_fix_gate.py::test_only_redundant_call_findings_can_be_pre_fix` | D | `optimus/tests/test_ai_grounding_gate.py::test_callsite_stamp_is_exact_in_both_finding_shapes` | L8c |

### test_ai_privacy.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_privacy.py::TestExcludeParsing::test_idempotent_re_parse` | D | `optimus/tests/test_ai_privacy.py::TestExcludeParsing::test_parses_one_type_per_line` (+1) | L8c |
| `optimus/tests/test_ai_privacy.py::TestSuggestFixRefuses::test_suggest_fix_no_http_call_when_excluded` | D | `optimus/tests/test_ai_privacy.py::TestSuggestFixRefuses::test_suggest_fix_short_circuits_for_excluded_type` | L8c |
| `optimus/tests/test_ai_privacy.py::TestSettings::test_timeout_floor_clamps_zero` | rename | `optimus/tests/test_ai_privacy.py::TestSettings::test_timeout_floor_clamps_low_values_and_keeps_valid_ones` | L8c |
| `optimus/tests/test_ai_privacy.py::TestSettings::test_timeout_floor_clamps_negative` | fold | `optimus/tests/test_ai_privacy.py::TestSettings::test_timeout_floor_clamps_low_values_and_keeps_valid_ones` | L8c |
| `optimus/tests/test_ai_privacy.py::TestSettings::test_timeout_floor_leaves_valid_values_alone` | fold | `optimus/tests/test_ai_privacy.py::TestSettings::test_timeout_floor_clamps_low_values_and_keeps_valid_ones` | L8c |

### test_ai_quality_workflow.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_quality_workflow.py::test_the_header_lists_every_job` | D | no contract | L1 |

### test_ai_queued_only.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_queued_only.py::test_ambient_spend_marker_has_no_production_reader_or_writer` | D | `optimus/tests/test_ai_queued_only.py::test_usage_collection_cannot_write_to_a_previous_workers_session` (+1) | L8b |

### test_ai_recipe_eligibility.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_recipe_eligibility.py::TestEligibleSet::test_framework_n_plus_one_is_not_ai_eligible` | D | `optimus/tests/test_ai_recipe_eligibility.py::TestEligibleSet::test_exact_eligible_set` | L8a |
| `optimus/tests/test_ai_recipe_eligibility.py::TestBackfillPath::test_backfill_skips_framework_n_plus_one_and_gated_hot_lines` | D | `optimus/tests/test_ai_recipe_eligibility.py::TestAutoSuggestPath` | L8a |

### test_ai_recipe_prompt_hints.py (file deleted)

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_recipe_prompt_hints.py::test_hints_exist_only_for_eligible_types` | D | `optimus/tests/test_ai_prompts.py::test_hints_cover_exactly_the_eligible_types` | L8a |
| `optimus/tests/test_ai_recipe_prompt_hints.py::test_user_message_has_no_ddl_section` | D | `optimus/tests/test_ai_fix.py::TestBuildFixRequest::test_suggested_ddl_and_candidate_are_not_sent` | L8a |

### test_ai_refresh_contracts.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_refresh_contracts.py::test_the_engine_is_in_the_title_audit` | D | `optimus/tests/test_ai_error_log_titles.py::test_the_docs_list_exactly_the_titles_the_code_writes` (+1) | L8b |
| `optimus/tests/test_ai_refresh_contracts.py::test_the_engine_uses_the_shared_interrupt_guard_and_helper` | D | `optimus/tests/test_ai_refresh_contracts.py::test_a_job_timeout_in_the_worker_check_or_the_enqueue_escapes_fresh` | L8b |
| `optimus/tests/test_ai_refresh_contracts.py::test_the_engine_calls_only_public_names_of_the_store` | D | no contract | L8b |
| `optimus/tests/test_ai_refresh_contracts.py::test_the_payload_builder_says_the_principal_is_reserved` | D | no contract | L8b |
| `optimus/tests/test_ai_refresh_contracts.py::test_the_changelog_documents_the_stored_answer_shape` | D | no contract | L8b |
| `optimus/tests/test_ai_refresh_contracts.py::test_the_changelog_lists_refresh_state_with_the_engines_functions` | D | no contract; behaviour in `optimus/tests/test_ai_refresh_queue.py::test_refresh_state_shows_the_sessions_run_and_nothing_for_another_session` | L8b |
| `optimus/tests/test_ai_refresh_contracts.py::test_the_docs_describe_the_refresh_count_as_one_bump_at_acceptance` | D | ai_refresh_count asserts in test_ai_refresh_sql.py, test_ai_refresh_journal.py | L8b |
| `optimus/tests/test_ai_refresh_contracts.py::test_the_recorded_kinds_are_ai_fixs_own` | D | `optimus/tests/test_ai_refresh_contracts.py::test_every_ai_fix_kind_is_recorded_under_its_own_name` (+1) | L8b |

### test_ai_refresh_coverage.py (file deleted)

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_refresh_coverage.py::test_an_excluded_type_raises_not_eligible` | C | `optimus/tests/test_ai_privacy.py::TestSuggestFixRefuses::test_suggest_fix_short_circuits_for_excluded_type` | L8a |

### test_ai_refresh_inputs.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_refresh_inputs.py::test_selection_uses_missing_then_outdated_and_keeps_current` | D | `optimus/tests/test_ai_selection_contracts.py::test_order_is_state_then_severity_then_impact_then_age` (+3) | L8b |
| `optimus/tests/test_ai_refresh_inputs.py::test_nothing_stored_is_missing` | D | `optimus/tests/test_ai_selection_contracts.py::test_fix_state_is_the_one_definition` | L8b |
| `optimus/tests/test_ai_refresh_inputs.py::test_failed_empty_and_unparseable_records_are_outdated_not_missing` | D | `optimus/tests/test_ai_selection_contracts.py::test_fix_state_is_the_one_definition` (+1) | L8b |
| `optimus/tests/test_ai_refresh_inputs.py::test_bad_dates_and_impacts_do_not_crash_or_hide_work` | D | `optimus/tests/test_ai_selection_contracts.py::test_a_bad_stored_time_never_hides_work` (+1) | L8b |
| `optimus/tests/test_ai_refresh_inputs.py::test_excluded_types_and_pre_fix_calls_never_reach_the_model` | C | `optimus/tests/test_ai_selection_contracts.py::test_the_selection_carries_the_gated_and_excluded_counts` | L8b |
| `optimus/tests/test_ai_refresh_inputs.py::test_dict_gate_input_is_equivalent_to_a_child_row` | D | `optimus/tests/test_ai_selection_contracts.py::test_there_is_one_row_accessor` | L8b |
| `optimus/tests/test_ai_refresh_inputs.py::test_bundle_is_loaded_only_once_per_slice_and_never_unpickled` | D | `optimus/tests/test_ai_selection_contracts.py::test_a_second_call_with_the_memo_does_not_read_the_file_again` (+2) | L8b |
| `optimus/tests/test_ai_refresh_inputs.py::test_missing_bundle_and_malformed_entries_are_empty` | D | `optimus/tests/test_ai_selection_contracts.py::test_a_bundle_of_any_other_shape_yields_nothing` | L8b |
| `optimus/tests/test_ai_refresh_inputs.py::test_selection_handles_malformed_severity` | D | `optimus/tests/test_ai_selection_contracts.py::test_a_blank_severity_ranks_as_low_and_an_unrecognised_one_goes_last` | L8b |

### test_ai_refresh_journal.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_refresh_journal.py::test_the_finish_of_a_run_has_no_counting_keyword` | D | `optimus/tests/test_ai_refresh_journal.py::test_no_ending_of_a_run_counts_a_refresh` | L8b |

### test_ai_refresh_queue.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_refresh_queue.py::test_admission_honors_the_planned_site_configuration_names` | D | `optimus/tests/test_ai_refresh_queue.py::test_settings_written_by_set_config_without_p_are_read_as_numbers` | L8b |
| `optimus/tests/test_ai_refresh_queue.py::test_serialization_conflicts_have_a_bounded_sql_only_retry` | D | `optimus/tests/test_ai_refresh_conflicts.py::test_retry_sql_retries_every_conflict_in_short_transactions` (+1) | L8b |
| `optimus/tests/test_ai_refresh_queue.py::test_nonserialization_errors_are_not_retried_as_admissions` | D | `optimus/tests/test_ai_refresh_conflicts.py::test_retry_sql_never_retries_other_errors` | L8b |

### test_ai_refresh_schema.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_refresh_schema.py::test_cumulative_session_usage_can_hold_the_sum_of_valid_provider_counts` | D | `optimus/tests/test_optimus_session_layout.py::test_no_data_field_added_removed_or_retyped` | L7 |
| `optimus/tests/test_ai_refresh_schema.py::test_the_steps_token_field_keeps_its_32_bit_type` | D | `optimus/tests/test_optimus_session_layout.py::test_no_data_field_added_removed_or_retyped` | L7 |

### test_ai_refresh_work.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_refresh_work.py::test_auto_refresh_preserves_human_written_steps` | D | `optimus/tests/test_ai_refresh_work.py::test_notes_a_person_wrote_are_kept_by_an_analyze_time_run_only` (+1) | L8b |

### test_ai_selection_contracts.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_selection_contracts.py::test_a_version_is_current_does_not_accept_is_outdated` | D | `optimus/tests/test_ai_selection_contracts.py::test_fix_state_is_the_one_definition` (+1) | L8b |
| `optimus/tests/test_ai_selection_contracts.py::test_light_recordings_use_the_shared_shape_reader` | D | `optimus/tests/test_ai_selection_contracts.py::test_a_legacy_bare_uuid_map_keeps_only_json_recordings` (+1) | L8b |

### test_ai_slow_background_job.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_slow_background_job.py::TestGate::test_eligible_type` | D | `optimus/tests/test_ai_recipe_eligibility.py::TestEligibleSet::test_exact_eligible_set` | L8a |
| `optimus/tests/test_ai_slow_background_job.py::TestGate::test_suggest_fix_refuses_a_gated_job_before_any_request` | D | `optimus/tests/test_ai_recipe_eligibility.py::TestSuggestFixGuard::test_refuses_before_any_provider_or_http_call` (+1) | L8a |
| `optimus/tests/test_ai_slow_background_job.py::TestReport::test_stored_suggestion_renders_on_the_job_card` | D | `optimus/tests/test_finding_card_ai_fix.py::TestAiFixBlockRendering::test_block_renders_when_llm_fix_present` | L8a |
| `optimus/tests/test_ai_slow_background_job.py::TestReport::test_no_block_without_a_stored_suggestion` | D | `optimus/tests/test_finding_card_ai_fix.py::TestAiFixBlockRendering::test_no_block_when_llm_fix_absent` | L8a |

### test_ai_spend.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_spend.py::test_ambient_counter_query_matches_the_session_uuid` | D | no contract; behaviour in `optimus/tests/test_ai_spend.py::test_counter_query_adds_in_database_with_null_coalescing` | L8a |
| `optimus/tests/test_ai_spend.py::test_an_unattributed_call_never_charges_a_previous_worker_session` | fold | `optimus/tests/test_ai_spend.py::test_no_attribution_shape_makes_ai_fix_charge_a_session` | L8a |
| `optimus/tests/test_ai_spend.py::test_explicit_attribution_never_charges_the_ambient_session` | rename | `optimus/tests/test_ai_spend.py::test_no_attribution_shape_makes_ai_fix_charge_a_session` | L8a |
| `optimus/tests/test_ai_spend.py::test_an_explicit_caller_records_its_spend_once_into_its_own_session` | D | `optimus/tests/test_ai_spend.py::test_spend_takes_a_usage_dict_total` (+2) | L8a |
| `optimus/tests/test_ai_spend.py::test_an_explicit_billed_failure_carries_its_usage_and_charges_nothing` | D | `optimus/tests/test_ai_reliability_core.py::test_truncated_thinking_preserves_reported_usage_on_error` | L8a |
| `optimus/tests/test_ai_spend.py::test_an_internal_call_with_an_explicit_session_never_charges_the_ambient_one` | D | `optimus/tests/test_ai_spend.py::test_no_attribution_shape_makes_ai_fix_charge_a_session` | L8a |
| `optimus/tests/test_ai_spend.py::test_each_billed_call_of_a_reask_is_collected_once_for_sql_settlement` | D | `optimus/tests/test_ai_fix.py::TestGuardedCompletion::test_reask_tokens_accumulate` | L8a |
| `optimus/tests/test_ai_spend.py::test_humanize_without_usage_out_never_charges_an_unattributed_session` | D | `optimus/tests/test_ai_spend.py::test_no_attribution_shape_makes_ai_fix_charge_a_session` | L8a |
| `optimus/tests/test_ai_spend.py::test_an_empty_session_or_docname_is_unattributed` | fold | `optimus/tests/test_ai_spend.py::test_no_attribution_shape_makes_ai_fix_charge_a_session` | L8a |
| `optimus/tests/test_ai_spend.py::test_public_call_explicit_session_reaches_protocol` | D | `optimus/tests/test_ai_fix.py::TestGuardedCompletion::test_timeout_argument_caps_the_budget` | L8a |
| `optimus/tests/test_ai_spend.py::test_the_recording_query_treats_chained_wheres_like_one_anded_where` | D | no contract | L8a |
| `optimus/tests/test_ai_spend.py::test_an_explicit_call_never_borrows_the_ambient_session_for_its_row` | D | `optimus/tests/test_ai_spend.py::test_an_http_failure_row_is_filed_under_the_calls_own_session` | L8a |
| `optimus/tests/test_ai_spend.py::test_the_ambient_and_the_explicit_session_differ_and_each_row_follows_its_call` | D | `optimus/tests/test_ai_spend.py::test_an_http_failure_row_is_filed_under_the_calls_own_session` | L8a |
| `optimus/tests/test_ai_spend.py::test_an_internal_call_with_only_a_docname_is_explicit_for_charge_and_row` | D | `optimus/tests/test_ai_spend.py::test_no_attribution_shape_makes_ai_fix_charge_a_session` (+1) | L8a |
| `optimus/tests/test_ai_spend.py::test_an_unreadable_spend_marker_files_the_row_without_a_session` | D | no contract | L8a |

### test_ai_sql_policy.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_sql_policy.py::test_changed_arguments_under_kept_opener_are_rejected` | D | `optimus/tests/test_ai_guardrails.py::test_introduced_lines_and_node_spans` (+1) | L8c |
| `optimus/tests/test_ai_sql_policy.py::test_completion_status_precedes_sql_policy` | D | `optimus/tests/test_ai_guardrail_gaps.py::test_completion_status_wins_over_new_saved_checks` | L8c |
| `optimus/tests/test_ai_sql_policy.py::test_refresh_selects_only_affected_answer_and_never_in_missing_only_mode` | D | `optimus/tests/test_ai_guardrail_gaps.py::test_saved_answer_render_and_refresh_preserve_private_original` (+1) | L8c |
| `optimus/tests/test_ai_sql_policy.py::test_one_reask_preserves_both_answers_and_counts_usage` | D | `optimus/tests/test_ai_guardrail_gaps.py::test_permission_repair_is_single_and_keeps_both_paid_answers` (+1) | L8c |

### test_ai_subscription_cli.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_subscription_cli.py::TestBuildArgv::test_argv_has_no_shell_metacharacters_from_data` | D | `optimus/tests/test_ai_subscription_cli.py::TestBuildArgv::test_the_isolation_flags_are_exactly_the_shipped_ones_under_the_real_login` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestBuildArgv::test_argv_hardening_flags` | D | `optimus/tests/test_ai_subscription_cli.py::TestToolIsolation::test_every_file_command_agent_and_web_tool_is_disabled` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestBuildArgv::test_the_operators_skill_list_is_kept_out_of_the_model_prompt` | D | `optimus/tests/test_ai_subscription_cli.py::TestToolIsolation::test_every_file_command_agent_and_web_tool_is_disabled` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestCliEnv::test_the_dedicated_home_parameter_and_helpers_are_gone` | D | no contract; behaviour in `optimus/tests/test_ai_subscription_cli.py::TestCallLocalCli::test_the_exec_never_touches_the_credential_or_the_retired_dedicated_home` (+1) | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestCliInstalled::test_codex_candidate_resolving_to_codex_js_is_accepted_with_mocked_resolution` | D | `optimus/tests/test_ai_subscription_cli.py::TestCliInstalled::test_npm_style_codex_symlink_to_codex_js_is_accepted` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestCliInstalled::test_codex_symlink_to_a_shell_is_rejected` | D | `optimus/tests/test_ai_subscription_cli.py::TestCliResolve::test_untrusted_and_entrypoint_reasons` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestCliInstalled::test_candidate_named_sh_is_rejected_even_if_it_resolves_to_codex` | D | `optimus/tests/test_ai_subscription_cli.py::TestCliInstalled::test_basename_must_be_codex` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestCliInstalled::test_node_entrypoint_still_has_to_be_trusted` | D | `optimus/tests/test_ai_subscription_cli.py::TestCliInstalled::test_group_or_world_writable_file_is_rejected` (+1) | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestCliInstalled::test_blank_path_rejects_a_path_hit_owned_by_someone_else` | D | `optimus/tests/test_ai_subscription_cli.py::TestCliInstalled::test_blank_path_rejects_a_writable_path_hit` (+1) | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestCliInstalled::test_blank_path_rejects_a_relative_path_hit` | D | `optimus/tests/test_ai_subscription_cli.py::TestCliInstalled::test_relative_path_is_rejected` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestCliSandbox::test_the_retired_dedicated_home_is_never_created_or_looked_up` | D | no contract; behaviour in `optimus/tests/test_ai_subscription_cli.py::TestCallLocalCli::test_the_exec_never_touches_the_credential_or_the_retired_dedicated_home` (+1) | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestCliResolve::test_reasons_for_a_bad_configured_path` | D | `optimus/tests/test_ai_claude_cli.py::TestInstalledCheck::test_reasons_for_a_bad_configured_path` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestCliResolve::test_the_call_and_the_status_show_the_reason` | D | `optimus/tests/test_ai_claude_cli.py::TestInstalledCheck::test_the_call_and_the_status_show_the_reason` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestCallLocalCli::test_scratch_tree_is_deleted_afterwards` | D | `optimus/tests/test_ai_claude_cli.py::TestExec::test_the_scratch_tree_is_deleted_after_success_and_after_failure` (+1) | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestCallLocalCli::test_scratch_tree_is_deleted_after_a_failure` | D | `optimus/tests/test_ai_subscription_cli.py::TestCliSandbox::test_tree_is_removed_when_the_body_raises` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestCallLocalCli::test_budget_covers_the_login_check` | D | `optimus/tests/test_ai_subscription_cli.py::TestCallLocalCli::test_probe_time_comes_out_of_the_exec_budget` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestCallLocalCli::test_over_cap_reply_file_is_cut_and_flagged_truncated` | D | `optimus/tests/test_ai_subscription_cli.py::TestCallLocalCli::test_over_cap_reply_behind_a_long_run_of_leading_blanks_is_still_flagged` (+1) | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestCallLocalCli::test_over_cap_stdout_fallback_is_cut_and_flagged_truncated` | D | `optimus/tests/test_ai_subscription_cli.py::TestCallLocalCli::test_leading_blanks_on_the_stdout_fallback_do_not_hide_an_overflow` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestCallLocalCli::test_over_cap_reply_comes_back_scrubbed_and_marked_truncated` | D | `optimus/tests/test_ai_claude_cli.py::TestFailuresInTheReply::test_the_result_is_capped_after_the_parse_and_marked_after_the_scrub` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestLoginProbe::test_login_failure_kinds` | D | `optimus/tests/test_ai_subscription_cli.py::TestClassifyStderr::test_every_kind_used_is_known_and_only_terminal_ones_are_fatal` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestLoginProbe::test_an_operator_who_has_not_signed_in_yet_gets_signed_out_not_an_error` | D | `optimus/tests/test_ai_subscription_cli.py::TestLoginProbe::test_a_missing_codex_dir_is_signed_out_without_running_the_cli` (+1) | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestLoginProbe::test_connect_with_a_codex_home_error_says_plain_codex_login` | D | `optimus/tests/test_ai_subscription_cli.py::TestLoginProbe::test_the_real_codex_home_error_text_is_signed_out_not_a_transient_error` (+1) | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestLoginProbe::test_the_not_signed_in_message_says_plain_codex_login` | D | `optimus/tests/test_ai_subscription_cli.py::TestLoginProbe::test_connect_with_no_real_login_is_not_connected_and_says_plain_codex_login` (+1) | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestLoginProbe::test_success_is_memoized_and_failure_is_not` | D | `optimus/tests/test_ai_claude_cli.py::TestLoginCheck::test_a_success_is_memoized_for_thirty_seconds_and_a_failure_never_is` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestChildDiesWithTheWorker::test_run_cli_passes_the_hook_to_popen` | D | `optimus/tests/test_ai_subscription_cli.py::TestRunCli::test_grandchild_of_a_leader_that_exited_normally_is_killed` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestProvider::test_api_mode_is_unchanged` | D | `optimus/tests/test_ai_claude_cli.py::TestDiscriminator::test_api_mode_is_unchanged_by_the_field` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestProvider::test_snapshot_query_reads_the_connection_type` | D | `optimus/tests/test_ai_claude_cli.py::TestSnapshotQuery::test_the_select_binds_one_value_per_placeholder_and_reads_the_cli_choice` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestAvailability::test_available_when_guard_on_and_cli_present` | D | `optimus/tests/test_ai_subscription_cli.py::TestAvailability::test_sections_follow_their_toggle` (+1) | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestSubscriptionConstant::test_ai_fix_uses_the_same_constant` | D | `optimus/tests/test_ai_subscription_cli.py::TestSubscriptionConstant::test_constant_is_a_doctype_option` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestSubscriptionConstant::test_subscription_mode_active_follows_constant` | D | `optimus/tests/test_api_cli_session.py::test_subscription_mode_active_tolerates_whitespace` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestToolIsolation::test_each_disable_flag_is_paired_with_a_feature_name` | D | `optimus/tests/test_ai_subscription_cli.py::TestToolIsolation::test_on_capability_features_from_the_probe_are_disabled_too` (+1) | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestToolIsolation::test_a_probed_browser_use_external_is_in_the_list_alongside_the_floor` | D | `optimus/tests/test_ai_subscription_cli.py::TestToolIsolation::test_on_capability_features_from_the_probe_are_disabled_too` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestToolIsolation::test_other_hardening_flags_are_kept_with_a_probe` | D | `optimus/tests/test_ai_subscription_cli.py::TestBuildArgv::test_the_isolation_flags_are_exactly_the_shipped_ones_under_the_real_login` | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestRunCli::test_timeout_raises_and_kills_the_group` | D | `optimus/tests/test_ai_subscription_cli.py::TestCallLocalCli::test_timeout_kills_the_whole_process_group_and_is_not_fatal` (+1) | L8a |
| `optimus/tests/test_ai_subscription_cli.py::TestConcurrencyAndFollowUps::test_a_non_zero_exit_with_no_recognised_cause_is_sent_unknown_and_logged` | D | `optimus/tests/test_ai_subscription_cli.py::TestCallLocalCli::test_exit_error_maps_to_a_kind_and_never_leaks_stderr` | L8a |

### test_ai_transport_security.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_transport_security.py::test_http_interface_is_unchanged` | D | every other _http_post caller (test_ai_transport_certainty.py, key_handling) | L8a |

### test_ai_usage_observation.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_ai_usage_observation.py::test_unattributed_call_collects_usage_without_ambient_spending` | D | `optimus/tests/test_ai_usage_observation.py::test_openai_usage_presence_is_not_inferred_from_normalized_zero` | L8a |

### test_analyze_concurrency.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_analyze_concurrency.py::TestAcquireSingleFlight::test_free_acquires_and_marks_holder` | D | `optimus/tests/test_analyze_singleflight_redis.py::test_a_run_whose_flag_lapsed_leaves_the_new_holder_alone` (+1) | L2 |
| `optimus/tests/test_analyze_concurrency.py::TestAcquireSingleFlight::test_own_uuid_reenters` | D | `optimus/tests/test_analyze_singleflight_redis.py::test_a_re_enqueued_holder_refreshes_its_own_flag` | L2 |
| `optimus/tests/test_analyze_concurrency.py::TestAcquireSingleFlight::test_busy_reenqueues_and_returns_false` | C | `optimus/tests/test_analyze_singleflight_redis.py::test_a_busy_flag_re_enqueues_and_is_left_alone` | L2 |
| `optimus/tests/test_analyze_concurrency.py::TestAcquireSingleFlight::test_deadline_passed_degrades_to_proceed` | D | `optimus/tests/test_analyze_singleflight_redis.py::test_a_degraded_run_never_steals_the_flag_and_takes_it_once_free` | L2 |
| `optimus/tests/test_analyze_concurrency.py::TestReleaseSingleFlight::test_releases_only_when_owner` | D | `optimus/tests/test_analyze_singleflight_redis.py::test_a_plain_run_releases_its_flag` (+1) | L2 |
| `optimus/tests/test_analyze_concurrency.py::TestReleaseSingleFlight::test_does_not_clobber_other_holder` | D | `optimus/tests/test_analyze_singleflight_redis.py::test_a_run_whose_flag_lapsed_leaves_the_new_holder_alone` (+1) | L2 |
| `optimus/tests/test_analyze_concurrency.py::TestRunWiring::test_run_gates_on_singleflight_before_fetch` | D | `optimus/tests/test_analyze_delivery_guards.py::test_ready_duplicate_has_no_mutations_or_input_cleanup` | L2 |
| `optimus/tests/test_analyze_concurrency.py::TestEnqueueAnalyzeAsync::test_enqueue_analyze_has_no_is_job_enqueued_dedup` | D | `optimus/tests/test_analyze_concurrency.py::TestEnqueueAnalyzeAsync::test_async_enqueue_is_anonymous_and_always_fires` | L2 |

### test_analyze_delivery_guards.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_analyze_delivery_guards.py::test_genuine_empty_capture_still_finalizes` | D | `optimus/tests/test_analyze_delivery_guards.py::test_empty_capture_renders_report_before_ready` | L2 |
| `optimus/tests/test_analyze_delivery_guards.py::test_empty_capture_publishes_ready_event` | C | `optimus/tests/test_analyze_delivery_guards.py::test_empty_capture_publishes_ready_event_flagged_empty` | L2 |

### test_analyze_fetch.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_analyze_fetch.py::test_fetch_recordings_loads_legacy_unsigned_tree` | D | `optimus/tests/test_recording_tree_security.py::test_explicit_legacy_override_keeps_old_redis_formats` | L2 |
| `optimus/tests/test_analyze_fetch.py::test_cleanup_redis_deletes_tree_and_sidecar_keys` | D | `optimus/tests/test_cleanup_redis_order.py::test_every_per_recording_key_goes_including_infra` | L2 |
| `optimus/tests/test_analyze_fetch.py::test_a_high_volume_own_session_is_not_an_error` | D | `optimus/tests/test_analyze_fetch.py::test_no_drop_volume_is_logged_above_debug` (+1) | L2 |
| `optimus/tests/test_analyze_fetch.py::test_the_volume_alert_threshold_is_gone` | D | no contract; behaviour in `optimus/tests/test_analyze_fetch.py::test_no_drop_volume_is_logged_above_debug` | L2 |
| `optimus/tests/test_analyze_fetch.py::test_own_source_names_the_deciding_frame_not_a_pass_through_wrapper_inside_it` | D | `optimus/tests/test_analyze_fetch.py::test_a_log_names_the_deciding_frame_of_each_dropped_call` | L2 |
| `optimus/tests/test_analyze_fetch.py::test_own_source_is_unknown_without_a_deciding_frame_or_a_stack` | D | no contract | L2 |
| `optimus/tests/test_analyze_fetch.py::test_own_source_skips_malformed_frames_and_a_non_string_function` | D | `optimus/tests/test_analyze_fetch.py::test_one_malformed_frame_or_call_never_fails_the_own_call_strip` | L2 |

### test_analyze_memory.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_analyze_memory.py::TestRunGcCollect::test_run_gc_collect_after_analyzers` | rename | `optimus/tests/test_analyze_memory.py::TestRunGcCollect::test_run_collects_garbage_after_the_analyzers_unless_the_knob_is_off` | L2 |
| `optimus/tests/test_analyze_memory.py::TestNoRecordingDroppedForPerformance::test_analyze_has_no_recording_cap` | D | `optimus/tests/test_recording_order.py::test_run_hands_every_stage_the_chronological_list` | L2 |
| `optimus/tests/test_analyze_memory.py::TestNoRecordingDroppedForPerformance::test_run_does_not_cap_recordings` | D | `optimus/tests/test_recording_order.py::test_run_hands_every_stage_the_chronological_list` | L2 |

### test_analyze_run_v5_wiring.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_analyze_run_v5_wiring.py::test_analyze_imports_v5_analyzers` | D | `optimus/tests/test_analyze_run_v5_wiring.py::test_builtin_analyzers_list_includes_v5` | L2 |
| `optimus/tests/test_analyze_run_v5_wiring.py::test_run_loads_frontend_data_into_context` | C | `optimus/tests/test_analyze_delivery_guards.py::test_run_attaches_infra_and_frontend_data_before_the_analyzers` | L2 |
| `optimus/tests/test_analyze_run_v5_wiring.py::test_run_attaches_infra_to_recordings` | C | `optimus/tests/test_analyze_delivery_guards.py::test_run_attaches_infra_and_frontend_data_before_the_analyzers` | L2 |
| `optimus/tests/test_analyze_run_v5_wiring.py::test_persist_writes_v5_aggregate_json` | C | `optimus/tests/test_analyze_run_v5_wiring.py::test_persist_keeps_the_tail_of_each_v5_aggregate_list_and_warns` | L2 |

### test_analyze_runtime.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_analyze_runtime.py::TestApplyNice::test_run_applies_nice_gated_to_async` | rename | `optimus/tests/test_analyze_runtime.py::TestApplyNice::test_run_renices_only_on_the_async_path` | L2 |

### test_analyze_singleflight_heartbeat.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_analyze_singleflight_heartbeat.py::test_a_touch_refreshes_only_a_flag_this_session_may_hold` | D | `optimus/tests/test_analyze_singleflight_redis.py::test_a_run_whose_flag_lapsed_leaves_the_new_holder_alone` (+2) | L2 |
| `optimus/tests/test_analyze_singleflight_heartbeat.py::test_a_cache_hiccup_never_fails_the_touch` | D | `optimus/tests/test_analyze_singleflight_redis.py::test_a_failing_heartbeat_logs_one_line_naming_the_error` | L2 |

### test_analyzer_base_metrics.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_analyzer_base_metrics.py::test_percentile_p95_at_least_p50_for_ascending_input` | D | `optimus/tests/test_analyzer_base_metrics.py::test_percentile_monotonic_across_percentages` | L9b |

### test_analyzer_notes_section.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_analyzer_notes_section.py::test_clean_session_also_renders_no_section` | D | `optimus/tests/test_analyzer_notes_section.py::test_analyzer_notes_section_is_not_rendered_even_with_warnings` | L4 |

### test_api_ai_endpoints.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_api_ai_endpoints.py::test_the_ai_library_functions_stay` | D | `optimus/tests/test_ai_usage_observation.py::test_known_zero_survives_public_result` (+1) | L5 |

### test_api_cli_session.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_api_cli_session.py::test_subscription_mode_active_true_for_subscription` | D | `optimus/tests/test_api_cli_session.py::test_subscription_mode_active_tolerates_whitespace` | L8a |

### test_api_health.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_api_health.py::test_health_empty_site` | D | `optimus/tests/test_api_health.py::test_health_assembles_helper_results` | L5 |

### test_api_pill_state.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_api_pill_state.py::test_finished_tracked_session_stays_without_a_new_recording` | D | `optimus/tests/test_api_pill_state.py::test_known_session_status_maps_to_phase` | L5d |
| `optimus/tests/test_api_pill_state.py::test_ready_session_is_never_reported_analyzing` | D | `optimus/tests/test_api_pill_state.py::test_known_session_status_maps_to_phase` | L5d |
| `optimus/tests/test_api_pill_state.py::test_unknown_session_is_idle` | D | `optimus/tests/test_api_pill_state.py::test_other_users_session_is_invisible` | L5d |
| `optimus/tests/test_api_pill_state.py::test_the_in_flight_lookup_skips_a_recent_recording_row` | C | `optimus/tests/test_api_pill_state.py::test_a_recording_row_younger_than_the_pointer_lifetime_is_not_ended` | L5d |
| `optimus/tests/test_api_pill_state.py::test_expired_row_that_is_still_the_active_session_is_not_ended` | D | `optimus/tests/test_api_pill_state.py::test_a_live_pointer_still_reports_recording` | L5d |
| `optimus/tests/test_api_pill_state.py::test_expired_row_with_a_different_active_session_is_ended` | D | `optimus/tests/test_api_pill_state.py::test_tracking_an_expired_session_while_another_is_active_reports_the_old_one_ended` | L5d |
| `optimus/tests/test_api_pill_state.py::test_expired_row_with_the_pointer_gone_is_ended` | D | `optimus/tests/test_api_pill_state.py::test_a_recording_row_without_its_pointer_has_ended` | L5d |
| `optimus/tests/test_api_pill_state.py::test_row_ended_true_when_active_pointer_is_another_session` | D | `optimus/tests/test_api_pill_state.py::test_tracking_an_expired_session_while_another_is_active_reports_the_old_one_ended` | L5d |
| `optimus/tests/test_api_pill_state.py::test_row_ended_true_when_pointer_gone` | D | `optimus/tests/test_api_pill_state.py::test_a_recording_row_without_its_pointer_has_ended` | L5d |

### test_api_session_lifecycle.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_api_session_lifecycle.py::TestStop::test_without_active_session_reports_nothing_stopped` | D | `optimus/tests/test_api_session_lifecycle.py::TestStopAfterTheIdleExpiry::test_with_no_pointer_and_no_row_nothing_is_stopped` | L5 |
| `optimus/tests/test_api_session_lifecycle.py::TestRetryAnalyze::test_failed_session_is_reset_and_requeued_asynchronously` | D | `optimus/tests/test_api_session_actions.py::test_owner_retries_a_failed_session` | L5 |

### test_api_start_kwargs.py (file deleted)

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_api_start_kwargs.py::test_start_signature_accepts_capture_python_tree` | D | `optimus/tests/test_api_session_lifecycle.py::TestStart::test_creates_recording_row_meta_and_activates_user` (+1) | L5 |
| `optimus/tests/test_api_start_kwargs.py::test_stop_session_calls_force_stop_inflight` | C | `optimus/tests/test_api_session_lifecycle.py::TestStop::test_async_stop_marks_stopping_and_queues_analyze` | L5 |
| `optimus/tests/test_api_start_kwargs.py::test_start_calls_force_stop_inflight` | C | `optimus/tests/test_api_session_lifecycle.py::TestStart::test_creates_recording_row_meta_and_activates_user` | L5 |
| `optimus/tests/test_api_start_kwargs.py::test_start_persists_capture_python_tree_in_meta` | D | `optimus/tests/test_api_session_lifecycle.py::TestStart::test_capture_python_tree_false_is_recorded` | L5 |
| `optimus/tests/test_api_start_kwargs.py::test_require_profiler_user_does_not_recurse` | move | `optimus/tests/test_api_session_lifecycle.py::test_require_profiler_user_does_not_recurse` | L5 |
| `optimus/tests/test_api_start_kwargs.py::test_require_profiler_user_throws_for_user_without_role` | move | `optimus/tests/test_api_session_lifecycle.py::test_require_profiler_user_throws_for_user_without_role` (restored, then moved) | L5 |

### test_app_bucketing.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_app_bucketing.py::TestAppFromFinding::test_framework_path` | D | `optimus/tests/test_app_bucketing.py::TestAppFromFinding::test_short_form` | L4 |
| `optimus/tests/test_app_bucketing.py::TestOtherBucketSuppressed::test_other_bucket_absent_when_no_uncallsite_findings` | D | `optimus/tests/test_app_bucketing.py::TestBucketing::test_single_app_one_bucket` | L4 |
| `optimus/tests/test_app_bucketing.py::TestBucketContents::test_bucket_count_and_impact_totals_correct` | D | `optimus/tests/test_app_bucketing.py::TestBucketing::test_single_app_one_bucket` | L4 |

### test_app_bucketing_render.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_app_bucketing_render.py::test_finding_without_callsite_goes_to_other_bucket` | D | `optimus/tests/test_app_bucketing_render.py::TestIgnoredAppsFilter::test_findings_with_no_callsite_are_suppressed_in_render` (+1) | L4 |

### test_app_priority_split.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_app_priority_split.py::TestPerActionSplit::test_tracked_apps_routes_erpnext_to_framework` | D | `optimus/tests/test_app_priority_split.py::TestIsFrameworkApp::test_tracked_apps_inclusion_mode_flips_semantics` (+1) | L4 |

### test_background_job_capture.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_background_job_capture.py::TestEnqueueRegistersPendingJob::test_no_registration_without_active_session` | D | `optimus/tests/test_bg_job_phase1.py::TestNestedAndForeignJobs::test_no_session_no_marker` | L5 |
| `optimus/tests/test_background_job_capture.py::TestEnqueueRegistersPendingJob::test_does_not_track_the_analyze_job_itself` | D | `optimus/tests/test_background_job_capture.py::TestEnqueueRegistersPendingJob::test_analyze_job_never_carries_a_session_marker` | L5 |
| `optimus/tests/test_background_job_capture.py::TestEnqueueRegistersPendingJob::test_an_app_named_like_optimus_is_still_tagged` | D | `optimus/tests/test_internal_jobs.py::TestOptimusMethods::test_an_app_that_only_starts_with_optimus_is_tagged` (+1) | L5 |
| `optimus/tests/test_background_job_capture.py::TestEnqueueRegistersPendingJob::test_inline_job_returns_no_handle_nothing_to_track` | D | `optimus/tests/test_bg_job_phase1.py::TestSynchronousEnqueue::test_now_true_gets_no_marker_and_no_tracking` | L5 |
| `optimus/tests/test_background_job_capture.py::TestRqJobActive::test_unknown_job_is_not_active` | C | `optimus/tests/test_bg_job_phase1.py::TestWaitCapLabels::test_active_label_from_rq_status` | L5 |
| `optimus/tests/test_background_job_capture.py::test_stop_session_arms_the_draining_window` | D | `optimus/tests/test_api_session_lifecycle.py::TestStop::test_background_jobs_open_a_draining_window_and_status` (+1) | L5 |
| `optimus/tests/test_background_job_capture.py::test_before_job_honours_the_draining_window` | D | `optimus/tests/test_hooks_request_job_flow.py::TestBeforeJob::test_late_job_is_recorded_while_the_stopped_session_is_draining` (+1) | L5 |
| `optimus/tests/test_background_job_capture.py::test_analyze_run_waits_for_pending_jobs` | C | `optimus/tests/test_analyze_delivery_guards.py::test_run_waits_for_the_background_jobs_and_yields_when_the_wait_re_enqueued_it` | L5 |

### test_background_job_label.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_background_job_label.py::test_report_actions_mark_a_background_job_event_as_bg` | D | `optimus/tests/test_background_job_label.py::test_action_rows_carry_one_job_flag_from_the_shared_predicate` | L4 |
| `optimus/tests/test_background_job_label.py::test_source_resolution_reads_the_job_path` | D | `optimus/tests/test_background_job_label.py::test_source_resolution_strips_the_query_from_a_job_path` (+1) | L4 |
| `optimus/tests/test_background_job_label.py::test_actions_carry_no_inline_finding_field` | D | no contract | L4 |

### test_background_jobs_section.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_background_jobs_section.py::TestBuildBackgroundJobsStatusMerge::test_job_both_captured_and_tracked_is_not_duplicated` | D | `optimus/tests/test_background_jobs_section.py::TestBuildBackgroundJobsStatusMerge::test_completed_captured_job_keeps_rich_data_and_status` | L4 |

### test_bg_job_phase1.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_bg_job_phase1.py::TestNestedAndForeignJobs::test_same_job_tracked_twice_is_one_entry` | D | `optimus/tests/test_background_job_capture.py::TestSessionBackgroundJobHelpers::test_pending_jobs_round_trip` | L5 |
| `optimus/tests/test_bg_job_phase1.py::TestSchedulerJobsAreNotCaptured::test_before_job_without_marker_never_tracks_or_records` | D | `optimus/tests/test_hooks_request_job_flow.py::TestBeforeJob::test_job_without_marker_is_ignored` | L5 |
| `optimus/tests/test_bg_job_phase1.py::TestAiJobEnqueues::test_unwrapped_enqueue_is_untouched_and_untracked` | D | `optimus/tests/test_internal_jobs.py::TestOptimusMethods::test_an_optimus_method_is_never_tagged` | L5 |
| `optimus/tests/test_bg_job_phase1.py::TestAiJobEnqueues::test_without_session_ai_enqueue_is_not_marked_or_tracked` | D | `optimus/tests/test_internal_jobs.py::TestOptimusMethods::test_an_optimus_method_is_never_tagged` | L5 |

### test_bg_job_tracking.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_bg_job_tracking.py::TestHookWiring::test_before_job_calls_track_bg_job_started_after_marker_pop` | C | `optimus/tests/test_hooks_request_job_flow.py::TestBeforeJob::test_job_of_a_superseded_session_is_dropped` | L5 |
| `optimus/tests/test_bg_job_tracking.py::TestHookWiring::test_after_job_calls_track_bg_job_finished_in_finally` | C | `optimus/tests/test_hooks_request_job_flow.py::TestAfterJob::test_registers_links_and_finalises_the_job` | L5 |
| `optimus/tests/test_bg_job_tracking.py::TestHookWiring::test_before_job_stashes_bg_session_uuid_for_after_job` | D | `optimus/tests/test_hooks_request_job_flow.py::TestBeforeJob::test_active_session_job_is_recorded_and_tracked` | L5 |
| `optimus/tests/test_bg_job_tracking.py::TestHookWiring::test_after_job_reads_bg_session_uuid_with_fallback` | D | `optimus/tests/test_hooks_request_job_flow.py::TestAfterJob::test_orphan_job_still_gets_a_terminal_status_via_the_bg_stash` (+1) | L5 |
| `optimus/tests/test_bg_job_tracking.py::TestLateFinishDocTypeFallback::test_late_finish_does_not_clobber_already_completed_row` | D | `optimus/tests/test_bg_job_tracking.py::TestLateFinishDocTypeFallback::test_late_finish_keeps_a_terminal_row` | L5 |

### test_boot_session.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_boot_session.py::TestBootSession::test_enabled_flag_attached_when_settings_enabled` | D | `optimus/tests/test_boot_session.py::TestBootSession::test_returns_bool_not_truthy_value` | L5 |
| `optimus/tests/test_boot_session.py::TestWidgetGuard::test_widget_checks_profiler_enabled_before_mount` | D | `optimus/tests/test_boot_session.py::TestWidgetGuard::test_widget_mounts_only_for_profilers` | L5 |

### test_call_tree_findings.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_call_tree_findings.py::test_pure_helper_production_payload_all_filtered` | C | `optimus/tests/test_call_tree_findings.py::test_pure_helper_filters_decorator_wrappers` | L9b |
| `optimus/tests/test_call_tree_findings.py::test_pure_helper_still_keeps_application_frappe_code` | D | `optimus/tests/test_call_tree_findings.py::test_is_pure_helper_frame_keeps_application_frappe_code` | L9b |
| `optimus/tests/test_call_tree_findings.py::test_is_pure_helper_frame_keeps_user_code` | D | `optimus/tests/test_call_tree_findings.py::test_is_pure_helper_frame_keeps_application_frappe_code` | L9b |

### test_call_tree_render.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_call_tree_render.py::test_ct_is_other_frame_matches_both_synthetic_formats` | D | `optimus/tests/test_call_tree_render.py::test_other_frames_node_is_dropped` (+1) | L4 |
| `optimus/tests/test_call_tree_render.py::test_panel_excludes_background_jobs` | D | `optimus/tests/test_call_tree_render.py::test_slow_job_does_not_steal_a_top_n_slot` | L4 |

### test_call_tree_self_time.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_call_tree_self_time.py::TestSelfTimeReliability::test_flag_on_the_rows_follows_the_check` | D | `optimus/tests/test_call_tree_self_time.py::TestSelfBasedFindingsFollowTheReliabilityFlag::test_repeated_hot_frame_fires_when_self_time_is_consistent` (+2) | L9b |

### test_callsite_deep_link.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_callsite_deep_link.py::test_link_points_to_correct_file_and_line` | D | `optimus/tests/test_callsite_deep_link.py::test_raw_mode_wraps_callsite_in_vscode_link` | L4 |

### test_callsite_normalization.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_callsite_normalization.py::TestNormalizeCallsite::test_string_with_absolute_path` | D | `optimus/tests/test_callsite_normalization.py::TestNormalizeCallsite::test_string_file_and_lineno` | L4 |
| `optimus/tests/test_callsite_normalization.py::TestNormalizeCallsite::test_none_returns_none` | D | `optimus/tests/test_callsite_normalization.py::TestNormalizeCallsite::test_empty_string_returns_none` | L4 |
| `optimus/tests/test_callsite_normalization.py::TestAppFromFindingRegression::test_dict_callsite_still_works` | D | `optimus/tests/test_app_bucketing.py::TestAppFromFinding::test_bench_relative_path` | L4 |
| `optimus/tests/test_callsite_normalization.py::TestAppFromFindingRegression::test_missing_callsite_routes_to_other` | D | `optimus/tests/test_app_bucketing.py::TestAppFromFinding::test_no_callsite_falls_back_to_other` | L4 |
| `optimus/tests/test_callsite_normalization.py::TestAppFromFindingRegression::test_empty_string_callsite_routes_to_other` | D | `optimus/tests/test_app_bucketing.py::TestAppFromFinding::test_empty_callsite_dict_falls_back_to_other` (+1) | L4 |
| `optimus/tests/test_callsite_normalization.py::TestFindingToDictNormalizes::test_dict_callsite_passes_through_unchanged` | D | `optimus/tests/test_callsite_normalization.py::TestNormalizeCallsite::test_dict_form_passes_through` (+1) | L4 |

### test_cap_drop_capture.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_cap_drop_capture.py::test_a_borrowed_recorder_is_not_cleaned_up` | C | `optimus/tests/test_cap_drop_capture.py::test_a_request_that_borrowed_frappes_recorder_is_never_suppressed_or_purged` | L5 |

### test_capture_pii.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_capture_pii.py::test_identify_args_has_permission_keeps_doctype_and_ptype` | D | `optimus/tests/test_capture_pii.py::test_identify_args_has_permission_correct_signature` | L5 |

### test_capture_wraps.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_capture_wraps.py::test_wrap_preserves_original_via_attribute` | D | `optimus/tests/test_capture_wraps.py::test_wrap_chains_preexisting_wrap` (+1) | L5 |
| `optimus/tests/test_capture_wraps.py::test_uninstall_wraps_restores_originals` | D | `optimus/tests/test_capture_wraps.py::test_install_wraps_idempotent` | L5 |

### test_cleanup_redis_order.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_cleanup_redis_order.py::test_a_recording_registered_after_the_analyze_snapshot_is_cleaned_too` | D | `optimus/tests/test_cleanup_redis_order.py::test_a_uuid_only_the_snapshot_has_is_cleaned_along_with_one_only_the_set_has` | L2 |

### test_collapsible_sections.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_collapsible_sections.py::test_observations_subsection_exists` | D | `optimus/tests/test_collapsible_sections.py::test_observations_is_nested_inside_findings` | L4 |

### test_correlation_header.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_correlation_header.py::test_inject_correlation_header_sets_both_headers` | D | `optimus/tests/test_correlation_header_v15_compat.py::TestV16Path::test_writes_header_to_local_dict_when_present` | L5 |
| `optimus/tests/test_correlation_header.py::test_correlation_header_gated_on_profiler_session_id` | D | `optimus/tests/test_hooks_request_job_flow.py::TestAfterRequest::test_request_outside_a_session_registers_nothing_and_adds_no_header` | L5 |
| `optimus/tests/test_correlation_header.py::test_inject_correlation_header_noop_without_response_headers` | D | `optimus/tests/test_correlation_header_v15_compat.py::TestV15Path::test_no_response_and_no_local_dict_is_silent_noop` | L5 |
| `optimus/tests/test_correlation_header.py::test_hooks_callbacks_invokes_snapshot` | D | `optimus/tests/test_hooks_request_job_flow.py::TestBeforeRequest::test_active_session_activates_recorder_infra_and_pyinstrument` (+3) | L5 |
| `optimus/tests/test_correlation_header.py::test_stop_session_force_stops_infra_inflight` | C | `optimus/tests/test_api_session_lifecycle.py::TestStop::test_async_stop_marks_stopping_and_queues_analyze` | L5 |
| `optimus/tests/test_correlation_header.py::test_after_request_injects_correlation_header` | D | `optimus/tests/test_hooks_request_job_flow.py::TestAfterRequest::test_registers_recording_dumps_state_and_cleans_up` | L5 |

### test_dedup_findings_across_actions.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_dedup_findings_across_actions.py::test_empty_findings_list_is_safe` | D | no contract | L2 |
| `optimus/tests/test_dedup_findings_across_actions.py::test_dedup_dropped_findings_actually_removed_from_list` | D | `optimus/tests/test_dedup_findings_across_actions.py::test_two_per_action_findings_same_key_merge_to_one` | L2 |

### test_dialect_postgres.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_dialect_postgres.py::test_only_eligible_ai_hints_remain_for_either_dialect` | D | `optimus/tests/test_ai_prompts.py::test_hints_cover_exactly_the_eligible_types` (+1) | L9b |

### test_display_name_for_node.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_display_name_for_node.py::TestProductionBug::test_empty_function_with_serverscript_filename` | D | `optimus/tests/test_display_name_for_node.py::TestServerScriptDetection::test_serverscript_filename_pattern` (+1) | L9b |

### test_doc_event_lifecycle.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_doc_event_lifecycle.py::TestDoctypeFromControllerPath::test_bench_relative_controller_path` | D | `optimus/tests/test_doc_event_lifecycle.py::TestDoctypeFromControllerPath::test_app_relative_controller_path` | L4 |
| `optimus/tests/test_doc_event_lifecycle.py::TestFindingLifecycleBindings::test_from_hook_events` | D | `optimus/tests/test_doc_event_lifecycle.py::TestFindingLifecycleBindings::test_multiple_hook_events` | L4 |

### test_drilldown_chain.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_drilldown_chain.py::TestFindNodeInTree::test_basename_match_only` | D | `optimus/tests/test_drilldown_chain.py::TestFindNodeInTree::test_finds_by_basename_and_function` | L4 |
| `optimus/tests/test_drilldown_chain.py::TestWalkDrilldownChain::test_picks_hottest_child` | D | `optimus/tests/test_drilldown_chain.py::TestWalkDrilldownChain::test_stops_at_framework_boundary` | L4 |
| `optimus/tests/test_drilldown_chain.py::TestFindNodePathInTree::test_root_node_path_is_itself` | D | `optimus/tests/test_drilldown_chain.py::TestFindNodePathInTree::test_returns_full_ancestry_path` | L4 |

### test_end_to_end_metrics.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_end_to_end_metrics.py::test_analyzer_chain_with_missing_frontend_data` | D | `optimus/tests/test_frontend_timings_analyzer.py::test_missing_frontend_data_attribute_is_safe` (+1) | L9b |

### test_enqueue_patch.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_enqueue_patch.py::test_patch_injects_session_id` | D | `optimus/tests/test_bg_job_phase1.py::TestSynchronousEnqueue::test_async_enqueue_keeps_marker` (+1) | L5 |
| `optimus/tests/test_enqueue_patch.py::test_patch_skips_without_active_session` | D | `optimus/tests/test_bg_job_phase1.py::TestNestedAndForeignJobs::test_no_session_no_marker` | L5 |

### test_envelope_rollout_phase2.py (file deleted)

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_envelope_rollout_phase2.py::TestRetentionBacklogEnvelope::test_janitor_writes_envelope_when_setting_backlog` | D | `optimus/tests/test_boundary_pins.py::test_a_full_batch_writes_the_backlog_and_a_short_one_resets_it` | L7 |
| `optimus/tests/test_envelope_rollout_phase2.py::TestRetentionBacklogEnvelope::test_janitor_source_uses_wrap_value` | D | `optimus/tests/test_boundary_pins.py::test_a_full_batch_writes_the_backlog_and_a_short_one_resets_it` | L7 |
| `optimus/tests/test_envelope_rollout_phase2.py::TestOnboardingSeenEnvelopeReadCompat::test_unwrap_of_new_envelope_returns_truthy` | D | `optimus/tests/test_redis_audit.py::TestWrapUnwrap::test_wrap_roundtrip_current_version` | L7 |
| `optimus/tests/test_envelope_rollout_phase2.py::TestOnboardingSeenEnvelopeReadCompat::test_unwrap_of_legacy_bare_string_returns_truthy` | D | `optimus/tests/test_redis_audit.py::TestWrapUnwrap::test_unwrap_legacy_returns_none_version` (+1) | L7 |
| `optimus/tests/test_envelope_rollout_phase2.py::TestOnboardingSeenEnvelopeReadCompat::test_unwrap_of_missing_key_returns_falsy` | D | `optimus/tests/test_redis_audit.py::TestWrapUnwrap::test_unwrap_none_value_returns_default` | L7 |
| `optimus/tests/test_envelope_rollout_phase2.py::TestOnboardingSeenEnvelopeWriteShape::test_api_source_uses_wrap_value_for_onboarding_seen_write` | C | `optimus/tests/test_onboarding_toast_state.py::test_mark_onboarding_seen_persists` | L7 |
| `optimus/tests/test_envelope_rollout_phase2.py::TestOnboardingSeenEnvelopeWriteShape::test_api_source_uses_unwrap_value_for_onboarding_seen_read` | D | `optimus/tests/test_onboarding_toast_state.py::test_mark_onboarding_seen_persists` (+1) | L7 |

### test_envelope_rollout_phase3.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_envelope_rollout_phase3.py::TestExplainCacheEnvelopeRoundTrip::test_list_of_dicts_roundtrip` | D | `optimus/tests/test_redis_audit.py::TestWrapUnwrap::test_wrap_roundtrip_current_version` | L7 |
| `optimus/tests/test_envelope_rollout_phase3.py::TestExplainCacheEnvelopeRoundTrip::test_empty_list_roundtrip` | D | `optimus/tests/test_redis_audit.py::TestWrapUnwrap::test_wrap_roundtrip_current_version` | L7 |
| `optimus/tests/test_envelope_rollout_phase3.py::TestExplainCacheEnvelopeRoundTrip::test_legacy_bare_list_passes_through` | D | `optimus/tests/test_redis_audit.py::TestWrapUnwrap::test_unwrap_legacy_returns_none_version` | L7 |
| `optimus/tests/test_envelope_rollout_phase3.py::TestAnalyzeSourceUsesEnvelope::test_explain_cache_write_wraps_via_redis_schema` | rename | `optimus/tests/test_envelope_rollout_phase3.py::test_a_fresh_explain_result_is_written_as_a_versioned_envelope` | L7 |
| `optimus/tests/test_envelope_rollout_phase3.py::TestAnalyzeSourceUsesEnvelope::test_explain_cache_read_unwraps_via_redis_schema` | rename | `optimus/tests/test_envelope_rollout_phase3.py::test_a_cached_envelope_or_legacy_bare_list_is_read_without_a_new_explain` | L7 |

### test_envelope_rollout_phase4.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_envelope_rollout_phase4.py::TestSessionMetaEnvelopeRead::test_get_session_meta_unwraps_new_envelope` | D | `optimus/tests/test_session_meta.py::test_set_session_meta_persists_capture_python_tree_true` | L7 |

### test_error_log_mask.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_error_log_mask.py::TestStaleProcess::test_the_placeholder_is_the_masking_s_own` | D | `optimus/tests/test_redaction.py::TestKeyLiterals::test_every_user_shares_the_one_helper` (+1) | L10 |

### test_executive_summary.py (file renamed to test_report_first_screen.py; the 17 deleted tests keep their base-commit nodeids)

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_executive_summary.py::TestHeadlinePace::test_fast_session` | D | no contract | L4 |
| `optimus/tests/test_executive_summary.py::TestHeadlinePace::test_moderate_session` | D | no contract | L4 |
| `optimus/tests/test_executive_summary.py::TestHeadlinePace::test_slow_session` | D | no contract | L4 |
| `optimus/tests/test_executive_summary.py::TestHeadlinePace::test_headline_includes_seconds_queries_operations` | D | no contract | L4 |
| `optimus/tests/test_executive_summary.py::TestHeadlinePace::test_headline_below_threshold_keeps_ms` | D | no contract | L4 |
| `optimus/tests/test_executive_summary.py::TestHeadlinePace::test_headline_respects_custom_threshold` | D | no contract | L4 |
| `optimus/tests/test_executive_summary.py::TestTopBullets::test_top_three_by_impact` | D | no contract | L4 |
| `optimus/tests/test_executive_summary.py::TestTopBullets::test_less_than_three_findings_shows_what_exists` | D | no contract | L4 |
| `optimus/tests/test_executive_summary.py::TestTopBullets::test_no_findings_no_bullets` | D | no contract | L4 |
| `optimus/tests/test_executive_summary.py::TestTopBullets::test_bullets_carry_severity` | D | no contract | L4 |
| `optimus/tests/test_executive_summary.py::TestInfraSignal::test_large_memory_growth_flagged` | D | no contract | L4 |
| `optimus/tests/test_executive_summary.py::TestInfraSignal::test_memory_shrink_still_flagged` | D | no contract | L4 |
| `optimus/tests/test_executive_summary.py::TestInfraSignal::test_swap_flagged` | D | no contract | L4 |
| `optimus/tests/test_executive_summary.py::TestInfraSignal::test_small_memory_not_flagged` | D | no contract | L4 |
| `optimus/tests/test_executive_summary.py::TestShowFlag::test_clean_session_no_infra_signal_hides_card` | D | no contract | L4 |
| `optimus/tests/test_executive_summary.py::TestShowFlag::test_findings_present_shows_card` | D | no contract | L4 |
| `optimus/tests/test_executive_summary.py::TestShowFlag::test_only_infra_signal_still_shows_card` | D | no contract | L4 |
| `optimus/tests/test_executive_summary.py::TestEndToEndRender::test_card_renders_into_html` | rename | `optimus/tests/test_report_first_screen.py::TestEndToEndRender::test_kpi_issues_cell_renders` | L4 |
| `optimus/tests/test_executive_summary.py::TestEndToEndRender::test_clean_session_no_card` | rename | `optimus/tests/test_report_first_screen.py::TestEndToEndRender::test_clean_session_shows_nothing_to_fix_hero` | L4 |

### test_explain_flags.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_explain_flags.py::test_filesort_above_floor_still_flagged` | D | `optimus/tests/test_explain_flags.py::test_filesort_detected` (+1) | L9b |

### test_export_session_v3.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_export_session_v3.py::test_install_before_uninstall_calls_uninstall_wraps` | D | `optimus/tests/test_install_seed_and_cleanup.py::TestUninstallCleanup::test_before_uninstall_restores_the_wrapped_frappe_functions` | L5 |

### test_failure_log_lines_and_path_cut.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_failure_log_lines_and_path_cut.py::test_the_summary_line_says_one_more_table_for_one` | C | `optimus/tests/test_failure_log_lines_and_path_cut.py::test_evidence_failures_past_the_cap_get_one_summary_line` | L1 |
| `optimus/tests/test_failure_log_lines_and_path_cut.py::test_no_summary_line_when_every_failure_was_listed` | C | `optimus/tests/test_failure_log_lines_and_path_cut.py::test_evidence_failures_past_the_cap_get_one_summary_line` | L1 |
| `optimus/tests/test_failure_log_lines_and_path_cut.py::test_a_run_that_never_held_the_flag_does_not_say_it_no_longer_holds_it` | D | no contract; behaviour in `optimus/tests/test_analyze_singleflight_heartbeat.py::test_a_degraded_run_never_takes_the_flag_from_its_holder` | L1 |
| `optimus/tests/test_failure_log_lines_and_path_cut.py::test_uninstall_text_names_the_right_bench_commands` | D | no contract | L1 |
| `optimus/tests/test_failure_log_lines_and_path_cut.py::test_the_file_map_lists_the_split_modules` | D | no contract | L1 |
| `optimus/tests/test_failure_log_lines_and_path_cut.py::test_the_runbook_names_the_lock_wait_cause_and_the_console_line` | D | no contract | L1 |
| `optimus/tests/test_failure_log_lines_and_path_cut.py::test_the_docs_say_the_postgres_restore_is_committed` | D | no contract | L1 |

### test_finding_card_ai_fix.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_finding_card_ai_fix.py::TestMarkdownToSafeHtml::test_converts_basic_markdown` | D | `optimus/tests/test_finding_card_ai_fix.py::TestMarkdownToSafeHtml::test_strips_script_tags` (+1) | L4 |
| `optimus/tests/test_finding_card_ai_fix.py::TestHighlightDiffHtml::test_no_pre_blocks_is_noop` | D | `optimus/tests/test_finding_card_ai_fix.py::TestHighlightDiffHtml::test_regular_code_block_left_alone` | L4 |
| `optimus/tests/test_finding_card_ai_fix.py::TestAiFixBlockRendering::test_no_block_when_suggestion_blank` | D | `optimus/tests/test_grouped_ai_visibility.py::test_hidden_answers_create_no_boxes_indicators_or_links` | L4 |

### test_finding_card_smoking_gun.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_finding_card_smoking_gun.py::TestSourceSnippetRendering::test_snippet_always_rendered` | D | `optimus/tests/test_finding_card_smoking_gun.py::TestSourceSnippetRendering::test_snippet_lines_appear_in_raw_mode` | L4 |
| `optimus/tests/test_finding_card_smoking_gun.py::TestFindingsRefinements::test_actionable_finding_without_fix_hint_has_no_fallback` | D | `optimus/tests/test_finding_card_smoking_gun.py::TestDocEventHookInsideSmokingGun::test_hook_line_lives_inside_smoking_gun` (+1) | L4 |

### test_finding_impact_invariants.py (file deleted)

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_finding_impact_invariants.py::test_slow_hot_path_estimated_impact_bounded_by_action_wall` | D | `optimus/tests/test_call_tree_severity_clamp.py::test_estimated_impact_ms_clamped_to_action_wall` | L9b |
| `optimus/tests/test_finding_impact_invariants.py::test_findings_with_action_ref_respect_action_wall` | C | `optimus/tests/test_call_tree_severity_clamp.py::test_estimated_impact_ms_clamped_to_action_wall` | L9b |

### test_finding_types_allowlist.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_finding_types_allowlist.py::test_framework_n_plus_one_is_in_options` | D | `optimus/tests/test_finding_types_allowlist.py::test_every_emitted_finding_type_is_in_doctype_options` | L1 |

### test_fixture_builders.py (file deleted)

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_fixture_builders.py::test_build_call_produces_analyzer_ready_dict` | D | no contract | L1 |
| `optimus/tests/test_fixture_builders.py::test_build_recording_defaults` | D | no contract | L1 |
| `optimus/tests/test_fixture_builders.py::test_build_recording_computes_totals` | D | no contract | L1 |
| `optimus/tests/test_fixture_builders.py::test_builder_output_flows_through_n_plus_one` | D | `optimus/tests/test_n_plus_one.py::test_n_plus_one_detected_from_n_plus_one_fixture` (+1) | L1 |
| `optimus/tests/test_fixture_builders.py::test_build_explain_row_with_all_flags` | D | no contract | L1 |

### test_forced_recorder_config.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_forced_recorder_config.py::TestRecordForced::test_without_the_override_the_old_failure_is_reproduced` | D | `optimus/tests/test_forced_recorder_config.py::TestRecordForced::test_job_with_jobs_off_in_the_cached_config_is_a_background_job` | L5 |
| `optimus/tests/test_forced_recorder_config.py::TestRecordForced::test_forces_the_recorder` | D | `optimus/tests/test_hooks_request_job_flow.py::TestBeforeRequest::test_active_session_activates_recorder_infra_and_pyinstrument` (+1) | L5 |
| `optimus/tests/test_forced_recorder_config.py::TestNonRecordingRecorderIsNotReused::test_request_still_reuses_a_recording_recorder` | D | `optimus/tests/test_hooks_request_job_flow.py::TestBeforeRequest::test_existing_standalone_recorder_is_reused_not_replaced` | L5 |
| `optimus/tests/test_forced_recorder_config.py::TestNonRecordingRecorderIsNotReused::test_job_still_reuses_a_recording_recorder` | D | `optimus/tests/test_hooks_request_job_flow.py::TestBeforeJob::test_existing_recorder_is_reused` | L5 |
| `optimus/tests/test_forced_recorder_config.py::TestStaleUiConfigDoesNotChangeTheForcedRecorder::test_sql_is_recorded_and_no_profiler_runs_for_a_job` | D | `optimus/tests/test_forced_recorder_config.py::TestRetrievePatch::test_defaults_are_returned_while_forcing` | L5 |
| `optimus/tests/test_forced_recorder_config.py::TestStaleUiConfigDoesNotChangeTheForcedRecorder::test_sql_is_recorded_and_no_profiler_runs_for_a_request` | D | `optimus/tests/test_forced_recorder_config.py::TestRetrievePatch::test_defaults_are_returned_while_forcing` (+1) | L5 |
| `optimus/tests/test_forced_recorder_config.py::TestStaleUiConfigDoesNotChangeTheForcedRecorder::test_without_the_override_sql_is_lost_and_a_profiler_runs` | D | `optimus/tests/test_forced_recorder_config.py::TestRetrievePatch::test_defaults_are_returned_while_forcing` | L5 |

### test_format_duration_ms.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_format_duration_ms.py::TestBelowThreshold::test_integer_ms_default_zero_decimals` | D | `optimus/tests/test_humanize_duration_ms.py::TestBelowThreshold::test_integer_ms_default_zero_decimals` | L4 |
| `optimus/tests/test_format_duration_ms.py::TestBelowThreshold::test_decimals_two_preserves_sub_ms` | D | `optimus/tests/test_humanize_duration_ms.py::TestBelowThreshold::test_decimals_two_preserves_sub_ms` | L4 |
| `optimus/tests/test_format_duration_ms.py::TestBelowThreshold::test_decimals_two_pads_to_two` | D | `optimus/tests/test_humanize_duration_ms.py::TestBelowThreshold::test_decimals_two_pads_to_two` | L4 |
| `optimus/tests/test_format_duration_ms.py::TestAboveThreshold::test_typical_slow_action` | D | `optimus/tests/test_humanize_duration_ms.py::TestAboveThreshold::test_typical_slow_action` | L4 |
| `optimus/tests/test_format_duration_ms.py::TestAboveThreshold::test_decimals_arg_ignored_in_seconds` | D | `optimus/tests/test_humanize_duration_ms.py::TestAboveThreshold::test_decimals_arg_ignored_in_seconds` | L4 |
| `optimus/tests/test_format_duration_ms.py::TestAboveThreshold::test_large_value` | D | `optimus/tests/test_humanize_duration_ms.py::TestAboveThreshold::test_large_value` | L4 |
| `optimus/tests/test_format_duration_ms.py::TestDisabled::test_threshold_negative_keeps_ms` | D | no contract | L4 |
| `optimus/tests/test_format_duration_ms.py::TestDefensive::test_string_input` | D | `optimus/tests/test_humanize_duration_ms.py::TestDefensive::test_string_input` | L4 |
| `optimus/tests/test_format_duration_ms.py::TestDefensive::test_negative_value_below_threshold_absolute` | D | `optimus/tests/test_humanize_duration_ms.py::TestDefensive::test_negative_below_threshold_absolute` | L4 |

### test_framework_apps_classifier.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_framework_apps_classifier.py::TestFrameworkAppsMembership::test_frappe_core_in_set` | D | `optimus/tests/test_framework_apps_classifier.py::TestFrameworkAppsMembership::test_all_official_apps_present` (+1) | L9b |
| `optimus/tests/test_framework_apps_classifier.py::TestThirdPartyLibSetsUnified::test_call_tree_reuses_base_lib_set` | D | `optimus/tests/test_framework_apps_classifier.py::TestWidenedLibraryCoverage::test_sql_and_hotframe_classifiers_agree_on_widened_libs` (+1) | L9b |

### test_frontend_assets.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_frontend_assets.py::test_widget_has_visibility_listener` | D | `optimus/tests/test_pill_state_js.py::test_widget_resyncs_from_server_and_polls` | L3 |
| `optimus/tests/test_frontend_assets.py::test_widget_role_check` | D | `optimus/tests/test_boot_session.py::TestWidgetGuard::test_widget_mounts_only_for_profilers` | L3 |
| `optimus/tests/test_frontend_assets.py::test_form_js_does_not_render_analyzer_warnings_intro` | D | `optimus/tests/test_session_form_failure_reason.py::test_no_intro_unless_a_failed_saved_session_has_a_reason` | L3 |
| `optimus/tests/test_frontend_assets.py::test_widget_start_has_error_callback` | D | `optimus/tests/test_pill_state_js.py::test_widget_polling_and_resync_in_node` | L3 |
| `optimus/tests/test_frontend_assets.py::test_widget_stop_has_error_callback` | D | `optimus/tests/test_pill_state_js.py::test_widget_polling_and_resync_in_node` | L3 |
| `optimus/tests/test_frontend_assets.py::test_widget_stop_handles_no_active_session` | D | `optimus/tests/test_pill_state_js.py::test_widget_polling_and_resync_in_node` | L3 |
| `optimus/tests/test_frontend_assets.py::test_widget_css_has_print_safety` | D | no contract; behaviour in `optimus/tests/test_frontend_assets.py::test_widget_css_selectors` | L3 |
| `optimus/tests/test_frontend_assets.py::test_widget_logs_when_the_realtime_socket_wait_gives_up` | D | `optimus/tests/test_session_ready_popup.py::test_realtime_retry_gives_up_without_a_socket` | L3 |

### test_frontend_timings_ranges.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_frontend_timings_ranges.py::test_a_malformed_duration_is_dropped_not_counted_as_zero` | D | `optimus/tests/test_frontend_timings_hardening.py::test_a_bad_row_does_not_hide_good_rows_in_the_same_payload` | L4 |

### test_function_body_snippet.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_function_body_snippet.py::TestFindHeaderDefLine::test_def_line_returns_itself` | D | `optimus/tests/test_function_body_snippet.py::TestDecoratorThroughDefRows::test_undecorated_def_anchor_single_line` | L4 |
| `optimus/tests/test_function_body_snippet.py::TestFindHeaderDefLine::test_decorator_scans_to_def` | D | `optimus/tests/test_function_body_snippet.py::TestDecoratorThroughDefRows::test_single_decorator` | L4 |
| `optimus/tests/test_function_body_snippet.py::TestFindHeaderDefLine::test_non_header_returns_none` | D | `optimus/tests/test_function_body_snippet.py::TestDecoratorThroughDefRows::test_body_line_anchor_does_not_grab_next_function` | L4 |
| `optimus/tests/test_function_body_snippet.py::TestFindHeaderDefLine::test_class_before_def_returns_none` | D | `optimus/tests/test_function_body_snippet.py::TestDecoratorThroughDefRows::test_class_before_def_is_not_grabbed` | L4 |

### test_high_pass_guardrails.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_high_pass_guardrails.py::test_get_all_cannot_replace_permission_checked_get_list` | D | `optimus/tests/test_ai_guardrails.py::test_permission_downgrade` | L8c |

### test_hmac_envelope_versioning.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_hmac_envelope_versioning.py::TestSignUnsignRoundTrip::test_v1_round_trip_pickle_shaped_payload` | D | `optimus/tests/test_hmac_envelope_versioning.py::TestSignUnsignRoundTrip::test_v1_round_trip_returns_original_payload` (+1) | L5 |
| `optimus/tests/test_hmac_envelope_versioning.py::TestLegacyShapeBackwardCompat::test_legacy_v0_blob_starting_with_pickle_opcode_works` | D | `optimus/tests/test_hmac_envelope_versioning.py::TestLegacyShapeBackwardCompat::test_legacy_v0_blob_verifies_and_returns_payload` | L5 |

### test_hook_gate_order.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_hook_gate_order.py::TestBeforeRequestOrder::test_a_disabled_site_with_a_session_never_starts_the_recorder` | C | `optimus/tests/test_hooks_request_job_flow.py::TestBeforeRequest::test_master_kill_switch_wins_over_an_active_session` | L5 |
| `optimus/tests/test_hook_gate_order.py::TestBeforeRequestOrder::test_a_skipped_user_with_a_session_is_not_recorded` | D | `optimus/tests/test_hooks_request_job_flow.py::TestBeforeRequest::test_skip_listed_user_is_not_recorded` | L5 |
| `optimus/tests/test_hook_gate_order.py::TestBeforeJobOrder::test_a_disabled_site_still_pops_the_marker` | D | `optimus/tests/test_hooks_request_job_flow.py::TestBeforeJob::test_disabled_profiler_still_strips_the_marker` | L5 |
| `optimus/tests/test_hook_gate_order.py::TestBeforeJobOrder::test_an_enabled_tagged_job_is_tracked` | D | `optimus/tests/test_hooks_request_job_flow.py::TestBeforeJob::test_active_session_job_is_recorded_and_tracked` (+1) | L5 |

### test_hot_frames_app_vs_framework.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_hot_frames_app_vs_framework.py::TestRendererSplit::test_only_one_kind` | D | `optimus/tests/test_hot_frames_app_vs_framework.py::TestRenderedGroups::test_only_framework_has_no_app_heading` | L4 |
| `optimus/tests/test_hot_frames_app_vs_framework.py::TestRendererSplit::test_end_to_end_both_modes` | D | `optimus/tests/test_hot_frames_app_vs_framework.py::TestKeyCollision::test_app_row_lands_in_app_group` | L4 |
| `optimus/tests/test_hot_frames_app_vs_framework.py::TestKeyCollision::test_per_group_cap_still_applies` | D | `optimus/tests/test_hot_frames_app_vs_framework.py::TestPerGroupCap::test_framework_group_not_starved_by_app_frames` | L4 |

### test_hot_frames_self_time_display.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_hot_frames_self_time_display.py::TestBuilder::test_gen_a_row_total_is_total_ms_and_self_unavailable` | D | `optimus/tests/test_hot_frames_self_time_display.py::TestRendered::test_gen_a_row_shows_total_and_a_dash` | L4 |
| `optimus/tests/test_hot_frames_self_time_display.py::TestBuilder::test_gen_b_row_total_reads_cumulative_never_zero` | D | `optimus/tests/test_hot_frames_self_time_display.py::TestRendered::test_gen_b_row_shows_total_never_0ms` | L4 |
| `optimus/tests/test_hot_frames_self_time_display.py::TestBuilder::test_new_row_has_both_values` | D | `optimus/tests/test_hot_frames_self_time_display.py::TestRendered::test_new_row_shows_total_and_self` | L4 |
| `optimus/tests/test_hot_frames_self_time_display.py::TestBuilder::test_measured_zero_self_stays_zero_not_none` | D | `optimus/tests/test_hot_frames_self_time_display.py::TestRendered::test_measured_zero_self_shows_0ms` | L4 |
| `optimus/tests/test_hot_frames_self_time_display.py::TestBuilder::test_user_rows_sort_by_total` | D | `optimus/tests/test_hot_frames_self_time_display.py::TestRendered::test_user_rows_render_in_total_order` | L4 |
| `optimus/tests/test_hot_frames_self_time_display.py::TestBuilder::test_user_rows_sort_by_self_when_every_row_has_it` | D | `optimus/tests/test_hot_frames_self_time_display.py::TestRendered::test_user_rows_render_in_self_order_when_measured` | L4 |
| `optimus/tests/test_hot_frames_self_time_display.py::TestSelfNodesAreGone::test_call_tree_has_no_self_row_and_shows_the_self_label` | rename | `optimus/tests/test_hot_frames_self_time_display.py::TestSelfNodesAreGone::test_call_tree_shows_the_derived_self_label` | L4 |
| `optimus/tests/test_hot_frames_self_time_display.py::test_legacy_rows_never_render_a_zero_total` | D | `optimus/tests/test_hot_frames_self_time_display.py::TestRendered::test_gen_b_row_shows_total_never_0ms` | L4 |

### test_humanize_duration_ms.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_humanize_duration_ms.py::TestRoundingBoundaryAgreement::test_raw_float_and_rounded_ms_agree` | C | `optimus/tests/test_humanize_duration_ms.py::TestRoundingBoundaryAgreement::test_fractional_ms_matches_its_rounded_int` | L9b |

### test_index_advice_correctness.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_index_advice_correctness.py::test_the_docs_and_changelog_carry_the_t12_texts` | C | `optimus/tests/test_docs_truth.py::test_the_doc_quotes_the_index_advice_text_the_code_shows` | L9a |
| `optimus/tests/test_index_advice_correctness.py::TestServedNeedsLimit::test_a_capture_time_explain_is_no_evidence` | D | `optimus/tests/test_index_advice_correctness.py::TestServedNeedsEvidence::test_battery4_402_a_listed_single_sort_index_gives_the_range_recipe` (+2) | L9a |

### test_index_advice_verdicts.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_index_advice_verdicts.py::test_the_docs_and_changelog_quote_these_texts` | C | `optimus/tests/test_docs_truth.py::test_the_doc_quotes_the_index_advice_text_the_code_shows` | L9a |

### test_index_ai_removed.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_index_ai_removed.py::test_index_ai_symbols_are_deleted` | D | no contract; behaviour in `optimus/tests/test_api_ai_endpoints.py::test_ai_capabilities_reports_the_toggles_without_writing` (+1) | L8a |
| `optimus/tests/test_index_ai_removed.py::test_indexes_section_flag_is_retired` | D | no contract; behaviour in `optimus/tests/test_ai_fix.py::TestIsAvailableSection::test_unknown_section_does_not_block` | L8a |
| `optimus/tests/test_index_ai_removed.py::test_refill_has_no_index_step` | D | no contract | L8a |
| `optimus/tests/test_index_ai_removed.py::test_ai_capabilities_reports_no_index_section` | D | `optimus/tests/test_api_ai_endpoints.py::test_ai_capabilities_reports_the_toggles_without_writing` | L8a |
| `optimus/tests/test_index_ai_removed.py::test_analyze_run_has_no_index_ai_step` | D | no contract | L8a |
| `optimus/tests/test_index_ai_removed.py::test_report_template_has_no_dead_ai_index_block` | D | no contract; behaviour in `optimus/tests/test_index_recipes_render.py::test_legacy_session_regenerates_clean` (+1) | L8a |

### test_index_recipe_executable.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_index_recipe_executable.py::test_a_failing_error_log_never_stops_the_next_entry` | D | `optimus/tests/test_index_recipe_executable.py::test_a_failed_error_log_write_never_fails_the_migrate` | L9a |
| `optimus/tests/test_index_recipe_executable.py::test_the_ps_route_entry_is_stamped_mariadb` | D | `optimus/tests/test_index_recipes.py::TestDatabaseStamp::test_entries_tied_to_one_database_name_it` (+1) | L9a |

### test_index_recipes.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_index_recipes.py::TestNoCode::test_search_index_ticked_is_no_proof_of_an_index` | C | `optimus/tests/test_index_advice_correctness.py::TestSearchIndexIsNoProof::test_search_index_alone_never_gives_no_code` | L9a |
| `optimus/tests/test_index_recipes.py::TestCardNote::test_no_card_note_points_at_a_call_above` | D | `optimus/tests/test_index_recipes.py::TestCardNote::test_the_finding_and_the_card_point_at_the_code_above_them` (+1) | L9a |
| `optimus/tests/test_index_recipes.py::test_the_docs_say_how_to_remove_an_entry_and_that_the_guards_are_silent` | D | no contract; behaviour in `optimus/tests/test_index_recipe_executable.py::test_an_entry_for_another_database_is_skipped_and_logged_once` (+1) | L9a |
| `optimus/tests/test_index_recipes.py::test_the_docs_show_the_title_order_and_limit_the_indexed_lookup_to_mariadb` | D | no contract; behaviour in `optimus/tests/test_index_recipe_executable.py::test_the_error_log_title_starts_with_the_index_and_names_the_error` (+1) | L9a |

### test_index_recipes_prompt.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_index_recipes_prompt.py::test_index_advice_comes_from_the_profiler_not_the_system_prompt` | C | `optimus/tests/test_ai_prompts.py::test_the_prompt_has_no_index_recipe_of_its_own` | L9a |

### test_index_recipes_render.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_index_recipes_render.py::test_postgres_ddl_never_rendered` | D | `optimus/tests/test_index_recipes_render.py::test_missing_index_on_another_apps_field_gets_its_ensure_indexes_entry` (+1) | L9a |
| `optimus/tests/test_index_recipes_render.py::test_recipe_failure_fails_closed` | C | `optimus/tests/test_index_recipes_render.py::test_a_failed_missing_index_is_neutral_and_says_what_to_do` | L9a |
| `optimus/tests/test_index_recipes_render.py::test_a_failed_card_gives_a_neutral_verdict_and_a_next_step` | C | `optimus/tests/test_recipe_resilience.py::test_a_failed_card_recipe_is_shown_and_logged` | L9a |
| `optimus/tests/test_index_recipes_render.py::test_a_render_parses_each_query_once_for_its_aliases` | C | `optimus/tests/test_recipe_resilience.py::test_the_render_parses_each_query_once` (+1) | L9a |

### test_index_recommendation.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_index_recommendation.py::TestRenderedIndexCandidatePanel::test_renders_recommendation_without_installation_code` | C | `optimus/tests/test_index_recipes_render.py::test_table_card_keeps_candidate_columns_without_installation_code` | L9a |
| `optimus/tests/test_index_recommendation.py::TestRenderedIndexCandidatePanel::test_ai_index_block_is_never_rendered` | D | `optimus/tests/test_index_recipes_render.py::test_legacy_session_regenerates_clean` (+1) | L9a |
| `optimus/tests/test_index_recommendation.py::TestRenderedIndexCandidatePanel::test_single_column_recommendation_keeps_its_card` | D | `optimus/tests/test_index_recipes_render.py::test_single_column_card_keeps_its_recommendation` | L9a |

### test_index_sort_regressions.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_index_sort_regressions.py::test_semicolon_does_not_turn_the_last_sort_column_into_an_expression` | D | `optimus/tests/test_index_sort_regressions.py::test_recipe_uses_clause_order_instead_of_select_mention_order` | L9a |

### test_index_suggestion_safety.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_index_suggestion_safety.py::test_postgres_skips_index_suggestions` | D | `optimus/tests/test_index_suggestions.py::test_optimizer_skipped_when_dialect_unsupported` | L9a |

### test_index_suggestions.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_index_suggestions.py::test_modified_column_is_never_suggested` | D | `optimus/tests/test_index_suggestions.py::test_frappe_metadata_columns_are_never_suggested` (+1) | L9a |
| `optimus/tests/test_index_suggestions.py::test_modified_by_column_is_never_suggested` | D | `optimus/tests/test_index_suggestions.py::test_classify_column_blacklists_every_frappe_metadata_column` (+1) | L9a |
| `optimus/tests/test_index_suggestions.py::test_high_per_query_savings_still_emits` | D | `optimus/tests/test_index_suggestions.py::test_per_query_floor_boundary_at_exactly_2ms` (+1) | L9a |

### test_infra_capture.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_infra_capture.py::test_rq_source_uses_frappe_cache_directly` | rename | `optimus/tests/test_infra_capture.py::test_rq_queues_use_frappe_cache_as_their_connection` | L5 |

### test_install_auto_role.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_install_auto_role.py::TestSeedIgnoredAppsWithFrameworkApps::test_empty_table_gets_seeded_with_frappe_org_apps` | D | `optimus/tests/test_install_seed_and_cleanup.py::TestIgnoredAppsSeed::test_seeds_only_installed_defaults_in_alphabetical_order` | L7 |
| `optimus/tests/test_install_auto_role.py::TestSeedIgnoredAppsWithFrameworkApps::test_non_empty_table_is_NOT_overwritten` | D | `optimus/tests/test_install_seed_and_cleanup.py::TestIgnoredAppsSeed::test_never_overwrites_operator_config` | L7 |
| `optimus/tests/test_install_auto_role.py::TestSeedIgnoredAppsWithFrameworkApps::test_no_doctype_yet_is_a_silent_noop` | D | `optimus/tests/test_install_seed_and_cleanup.py::TestIgnoredAppsSeed::test_skips_when_settings_doctype_is_not_migrated_yet` | L7 |
| `optimus/tests/test_install_auto_role.py::TestSeedIgnoredAppsWithFrameworkApps::test_only_installed_apps_get_seeded` | D | `optimus/tests/test_install_seed_and_cleanup.py::TestIgnoredAppsSeed::test_seeds_only_installed_defaults_in_alphabetical_order` | L7 |
| `optimus/tests/test_install_auto_role.py::TestSeedIgnoredAppsWithFrameworkApps::test_no_default_apps_installed_is_a_silent_noop` | D | `optimus/tests/test_install_seed_and_cleanup.py::TestIgnoredAppsSeed::test_no_installed_default_means_no_save` | L7 |
| `optimus/tests/test_install_auto_role.py::TestSeedIgnoredAppsWithFrameworkApps::test_partial_install_preserves_alphabetical_order` | D | `optimus/tests/test_install_seed_and_cleanup.py::TestIgnoredAppsSeed::test_seeds_only_installed_defaults_in_alphabetical_order` | L7 |

### test_install_seed_and_cleanup.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_install_seed_and_cleanup.py::TestRefuseLegacyInstall::test_clean_site_passes` | D | `optimus/tests/test_install_seed_and_cleanup.py::TestAfterInstall::test_creates_the_role_once_and_runs_every_seed` | L7 |

### test_janitor_batched_updates.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_janitor_batched_updates.py::TestSweepStuckAnalyzing::test_one_set_value_call_for_N_rows` | C | `optimus/tests/test_janitor_batched_updates.py::TestSweepStuckAnalyzing::test_live_singleflight_holder_is_not_failed` | L7 |
| `optimus/tests/test_janitor_batched_updates.py::TestSweepStaleStopping::test_wired_into_sweep_stale_sessions` | D | `optimus/tests/test_janitor_isolation_and_schema_sentinel.py::TestJanitorEntryPoints::test_one_failing_sweep_does_not_stop_the_other_stale_sweeps` | L7 |
| `optimus/tests/test_janitor_batched_updates.py::TestSweepStalePhase2Runs::test_no_set_value_when_no_phase2_rows` | D | `optimus/tests/test_janitor_batched_updates.py::TestSweepStalePhase2Runs::test_only_locked_expired_recordings_are_cleaned` | L7 |

### test_jarvis_promo.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_jarvis_promo.py::TestJarvisPromoRendering::test_block_renders_exactly_once` | D | `optimus/tests/test_renderer_structure_snapshot.py::TestStructureSnapshot::test_fingerprint_matches_golden` | L4 |

### test_lens_promo.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_lens_promo.py::TestLensPromoRendering::test_block_renders_exactly_once` | D | `optimus/tests/test_renderer_structure_snapshot.py::TestStructureSnapshot::test_fingerprint_matches_golden` | L4 |
| `optimus/tests/test_lens_promo.py::TestLensPromoRendering::test_block_positioned_before_jump_to_nav` | D | `optimus/tests/test_jarvis_promo.py::TestJarvisPromoRendering::test_block_positioned_before_jump_to_nav` | L4 |

### test_line_profile_analyzer.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_line_profile_analyzer.py::TestFunctionNotInvoked::test_empty_lines_emits_warning_not_finding` | D | `optimus/tests/test_line_profile_analyzer.py::TestFunctionNotInvoked::test_multiple_uninvoked_share_one_warning` | L6 |
| `optimus/tests/test_line_profile_analyzer.py::TestFunctionNotInvoked::test_all_zero_hits_emits_warning_not_finding` | D | `optimus/tests/test_line_profile_analyzer.py::TestFunctionNotInvoked::test_multiple_uninvoked_share_one_warning` | L6 |

### test_line_profile_auto_arm_skips.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_line_profile_auto_arm_skips.py::test_server_chosen_with_nothing_readable_raises_the_no_function_error` | D | `optimus/tests/test_line_profile_auto_arm_skips.py::test_nothing_capturable_carries_every_skip_for_the_caller` (+1) | L6 |
| `optimus/tests/test_line_profile_auto_arm_skips.py::test_prepared_size_is_counted_exactly` | D | `optimus/tests/test_line_profile_capture_limits.py::test_the_running_estimate_equals_the_stored_payload` | L6 |

### test_line_profile_budget.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_line_profile_budget.py::test_start_watchdog_none_when_budget_zero` | D | `optimus/tests/test_line_profile_budget.py::test_before_request_no_watchdog_when_budget_zero` (+1) | L6 |

### test_line_profile_capture.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_line_profile_capture.py::TestAggregateSamplesMerging::test_zero_hit_line_per_hit_us_is_zero` | D | `optimus/tests/test_line_profile_capture.py::TestAggregateSamplesShape::test_no_samples_yields_zeroed_lines_per_pick` | L6 |

### test_line_profile_diff.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_line_profile_diff.py::TestContentHash::test_same_text_same_hash` | D | `optimus/tests/test_line_profile_diff.py::TestContentHash::test_trailing_whitespace_ignored` (+1) | L6 |
| `optimus/tests/test_line_profile_diff.py::TestAlignFunction::test_empty_inputs_empty_output` | D | `optimus/tests/test_line_profile_diff.py::TestAlignFunction::test_added_row_has_no_old_lineno` (+1) | L6 |

### test_line_profile_picker.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_line_profile_picker.py::TestDeriveApp::test_extracts_app_from_apps_prefix` | D | `optimus/tests/test_line_profile_picker.py::TestBenchUnderAppsFolder::test_module_and_app_come_from_the_first_app_package` | L6 |
| `optimus/tests/test_line_profile_picker.py::TestPureHelperFiltering::test_frappe_app_application_dropped` | fold | `optimus/tests/test_line_profile_picker.py::TestPureHelperFiltering::test_frappe_plumbing_frames_are_dropped` | L6 |
| `optimus/tests/test_line_profile_picker.py::TestPureHelperFiltering::test_frappe_handler_handle_dropped` | fold | `optimus/tests/test_line_profile_picker.py::TestPureHelperFiltering::test_frappe_plumbing_frames_are_dropped` | L6 |
| `optimus/tests/test_line_profile_picker.py::TestPureHelperFiltering::test_frappe_recorder_record_sql_dropped` | fold | `optimus/tests/test_line_profile_picker.py::TestPureHelperFiltering::test_frappe_plumbing_frames_are_dropped` | L6 |
| `optimus/tests/test_line_profile_picker.py::TestPureHelperFiltering::test_frappe_utils_typing_validations_wrapper_dropped` | fold | `optimus/tests/test_line_profile_picker.py::TestPureHelperFiltering::test_frappe_plumbing_frames_are_dropped` | L6 |
| `optimus/tests/test_line_profile_picker.py::TestPureHelperFiltering::test_frappe_model_document_save_dropped` | fold | `optimus/tests/test_line_profile_picker.py::TestPureHelperFiltering::test_frappe_plumbing_frames_are_dropped` | L6 |
| `optimus/tests/test_line_profile_picker.py::TestPureHelperFiltering::test_frappe_model_meta_init_dropped` | fold | `optimus/tests/test_line_profile_picker.py::TestPureHelperFiltering::test_frappe_plumbing_frames_are_dropped` | L6 |
| `optimus/tests/test_line_profile_picker.py::TestPureHelperFiltering::test_frappe_qb_query_execute_dropped` | fold | `optimus/tests/test_line_profile_picker.py::TestPureHelperFiltering::test_frappe_plumbing_frames_are_dropped` | L6 |
| `optimus/tests/test_line_profile_picker.py::TestMultiTreePicker::test_surfaces_frames_from_multiple_trees` | D | `optimus/tests/test_line_profile_picker.py::TestMultiTreePicker::test_per_tree_budget_keeps_one_giant_tree_from_starving_others` (+1) | L6 |
| `optimus/tests/test_line_profile_picker.py::TestPickerDialogIndent::test_no_hardcoded_leaf_indent` | D | no contract | L6 |
| `optimus/tests/test_line_profile_picker.py::TestPickerDialogIndent::test_depth_stack_reconstruction_intact` | D | no contract; behaviour in `optimus/tests/test_line_profile_picker.py::TestExpandHotChain::test_descends_through_user_code_chain` | L6 |

### test_line_profile_recheck.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_line_profile_recheck.py::TestIdempotence::test_newest_run_wins_even_when_an_older_run_was_heavier` | D | `optimus/tests/test_line_profile_recheck.py::TestPersist::test_newest_ready_run_decides_regardless_of_row_order` (+1) | L6 |
| `optimus/tests/test_line_profile_recheck.py::TestHotLineMergePlan::test_rerun_updates_in_place_and_removes_legacy_duplicates` | D | `optimus/tests/test_line_profile_recheck.py::TestPersist::test_legacy_duplicate_hot_line_rows_collapse_to_one` (+1) | L6 |
| `optimus/tests/test_line_profile_recheck.py::TestHotLineMergePlan::test_profiled_without_new_finding_is_removed_unprofiled_kept` | D | `optimus/tests/test_line_profile_recheck.py::TestPersist::test_profiled_function_without_a_new_finding_loses_its_hot_line` (+1) | L6 |

### test_line_profile_source_capture.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_line_profile_source_capture.py::test_snapshot_equals_the_interpreters_own_function_source` | D | `optimus/tests/test_line_profile_source_capture.py::test_prepared_run_snapshots_the_whole_function` | L6 |
| `optimus/tests/test_line_profile_source_capture.py::test_corpus_has_many_functions_with_blank_lines` | D | `optimus/tests/test_line_profile_source_capture.py::test_every_function_of_real_modules_is_captured_whole` (+1) | L6 |
| `optimus/tests/test_line_profile_source_capture.py::test_source_unavailable_carries_its_reason` | D | `optimus/tests/test_line_profile_source_capture.py::test_open_paren_reason_says_it_could_not_be_parsed` (+3) | L6 |

### test_line_profiling_wording.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_line_profiling_wording.py::test_docs_have_no_phase_wording` | move | `optimus/tests/test_docs_truth.py::test_docs_have_no_phase_wording` | L3 |
| `optimus/tests/test_line_profiling_wording.py::test_line_profiling_js_messages_use_placeholders_not_concatenation` | D | `optimus/tests/test_js_translation_keys.py::test_every_translation_key_is_a_fixed_sentence` | L3 |

### test_maintenance_scrub.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_maintenance_scrub.py::TestScrubScanSize::test_a_huge_queue_never_skips_the_scan_of_a_small_table` | D | `optimus/tests/test_maintenance_scrub.py::TestScrubScanSize::test_counts_error_logs_and_every_deleted_document_but_not_the_queue` | L10 |
| `optimus/tests/test_maintenance_scrub.py::TestScrubScanSize::test_a_queue_that_cannot_be_read_leaves_the_size_known` | D | `optimus/tests/test_maintenance_scrub.py::TestScrubScanSize::test_counts_error_logs_and_every_deleted_document_but_not_the_queue` | L10 |
| `optimus/tests/test_maintenance_scrub.py::TestNoQueue::test_the_queue_code_is_gone` | D | `optimus/tests/test_maintenance_scrub.py::TestNoQueue::test_a_real_run_never_touches_the_queue_and_inserts_nothing` | L10 |
| `optimus/tests/test_maintenance_scrub.py::TestHooksCacheRefresh::test_it_names_the_handler_hooks_py_registers` | D | `optimus/tests/test_maintenance_scrub.py::TestHooksCacheRefresh::test_the_hooks_an_old_process_cached_are_replaced` (+1) | L10 |

### test_missing_trees_warning.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_missing_trees_warning.py::test_an_iso_time_with_an_offset_is_compared_without_error` | D | `optimus/tests/test_missing_trees_warning.py::test_a_recording_time_stored_as_text_is_read` (+1) | L2 |

### test_n_plus_one.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_n_plus_one.py::test_loop_within_single_request_still_detected` | D | `optimus/tests/test_n_plus_one.py::test_user_code_n_plus_one_still_emits_as_actionable` | L9b |
| `optimus/tests/test_n_plus_one.py::test_walk_callsite_bench_path_profiler_stack_returns_none` | D | `optimus/tests/test_n_plus_one.py::test_n_plus_one_bench_relative_profiler_stack_produces_no_finding` (+1) | L9b |
| `optimus/tests/test_n_plus_one.py::test_is_profiler_own_query_unit` | D | `optimus/tests/test_own_query_predicate.py::TestOwnHooksInsideUserFlows::test_profiler_snapshot_with_only_framework_frames_outside_is_own` (+4) | L9b |
| `optimus/tests/test_n_plus_one.py::test_walk_callsite_returns_none_for_profiler_only_stack` | D | `optimus/tests/test_n_plus_one.py::test_profiler_infra_capture_query_is_not_flagged` (+1) | L9b |

### test_n_plus_one_dedup.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_n_plus_one_dedup.py::test_single_variant_keeps_classic_title` | D | `optimus/tests/test_n_plus_one.py::test_the_error_log_hooks_key_read_never_hides_a_log_error_loop` (+1) | L9b |

### test_observability_wording.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_observability_wording.py::test_failure_line_without_pairs_is_the_old_line` | D | `optimus/tests/test_recipe_resilience.py::test_no_failure_writes_no_log_line` | L1 |
| `optimus/tests/test_observability_wording.py::test_each_advisor_site_reports_its_error_type` | D | `optimus/tests/test_recipe_resilience.py::test_failures_are_logged_once_after_the_recipes_ran` (+1) | L1 |
| `optimus/tests/test_observability_wording.py::test_evidence_read_lines_are_capped` | D | `optimus/tests/test_failure_log_lines_and_path_cut.py::test_evidence_failures_past_the_cap_get_one_summary_line` | L1 |
| `optimus/tests/test_observability_wording.py::test_app_scope_docstring_is_true_about_timeouts` | D | no contract | L1 |
| `optimus/tests/test_observability_wording.py::test_the_runbook_covers_each_signal` | D | no contract | L1 |

### test_operations_robustness.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_operations_robustness.py::test_a_log_error_that_raises_does_not_escape` | D | `optimus/tests/test_operations_robustness.py::test_a_failed_insert_releases_the_shared_slot_for_the_other_processes` (+1) | L7 |
| `optimus/tests/test_operations_robustness.py::test_the_import_time_probe_keeps_its_print_fallback` | rename | `optimus/tests/test_operations_robustness.py::test_the_import_time_probe_falls_back_to_print_before_frappe_is_ready` | L7 |

### test_optimus_session_layout.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_optimus_session_layout.py::test_json_blobs_stay_hidden_and_read_only` | D | `optimus/tests/test_optimus_session_layout.py::test_display_flags_unchanged` | L7 |

### test_own_query_predicate.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_own_query_predicate.py::TestUndecided::test_empty_and_malformed_stacks_are_not_own` | D | `optimus/tests/test_own_query_predicate.py::TestMalformedFramesAreSkipped::test_a_stack_that_is_not_a_list_is_not_own` (+1) | L9b |
| `optimus/tests/test_own_query_predicate.py::TestMalformedFunction::test_analyze_completes_with_a_malformed_function_in_a_stack` | D | `optimus/tests/test_own_query_predicate.py::TestMalformedFunction::test_unhashable_or_missing_function_does_not_raise` | L9b |
| `optimus/tests/test_own_query_predicate.py::TestSharedByEverySurface::test_queries_per_action_drops_an_own_hook_query_inside_a_user_flow` | D | `optimus/tests/test_own_query_predicate.py::TestOwnHooksInsideUserFlows::test_protect_file_get_all_inside_a_user_flow_is_own` (+1) | L9b |

### test_pdf_expand_collapsibles.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_pdf_expand_collapsibles.py::TestExpandCollapsibles::test_adds_open_to_details_with_id` | D | `optimus/tests/test_pdf_expand_collapsibles.py::TestFullReportPreprocess::test_nested_details_all_opened` | L4 |

### test_per_action.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_per_action.py::test_run_doc_method_distinct_buttons_produce_distinct_labels` | D | `optimus/tests/test_per_action.py::test_run_doc_method_appends_method_name_in_technical_label` (+1) | L9b |
| `optimus/tests/test_per_action.py::test_humanization_does_not_leak_into_per_action_table` | D | `optimus/tests/test_per_action.py::test_savedocs_save_action_appended_to_technical_label` | L9b |

### test_performance.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_performance.py::test_pyinstrument_start_stop_cycle_under_10ms` | D | no contract; behaviour in `optimus/tests/test_capture_wraps.py::test_start_pyi_session_starts_when_available` | L5 |

### test_permissions_file_hook.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_permissions_file_hook.py::test_recordings_file_allows_recording_user` | D | `optimus/tests/test_permissions_file_hook.py::test_recording_user_defers_true` (+2) | L5 |
| `optimus/tests/test_permissions_file_hook.py::test_recordings_file_allows_system_manager` | D | `optimus/tests/test_permissions_file_hook.py::test_system_manager_defers_true` (+1) | L5 |
| `optimus/tests/test_permissions_file_hook.py::test_non_optimus_file_allowed` | D | `optimus/tests/test_permissions_file_hook.py::test_non_optimus_session_file_defers_true` (+1) | L5 |
| `optimus/tests/test_permissions_file_hook.py::test_system_manager_allowed` | D | `optimus/tests/test_permissions_file_hook.py::test_system_manager_defers_true` | L5 |
| `optimus/tests/test_permissions_file_hook.py::test_owner_allowed` | D | `optimus/tests/test_permissions_file_hook.py::test_recording_user_defers_true` | L5 |
| `optimus/tests/test_permissions_file_hook.py::test_stranger_denied` | D | `optimus/tests/test_permissions_file_hook.py::test_stranger_denies_false` | L5 |
| `optimus/tests/test_permissions_file_hook.py::test_read_sharee_denied_recordings_file_through_controller_hooks` | D | `optimus/tests/test_permissions_file_hook.py::test_recordings_file_hook_denies_stranger` | L5 |
| `optimus/tests/test_permissions_file_hook.py::test_v15_private_file_of_another_user_denied` | D | `optimus/tests/test_permissions_file_hook.py::test_v15_defers_none` (+1) | L5 |
| `optimus/tests/test_permissions_file_hook.py::test_v15_own_shared_public_file_allowed` | D | `optimus/tests/test_permissions_file_hook.py::test_v15_defers_none` | L5 |
| `optimus/tests/test_permissions_file_hook.py::test_v15_gated_file_stranger_denied_before_core` | D | `optimus/tests/test_permissions_file_hook.py::test_v15_gate_still_denies_false` (+1) | L5 |

### test_persistent_cache_keys.py (file deleted)

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_persistent_cache_keys.py::test_profiler_keys_survive_clear_cache` | D | `optimus/tests/test_operations_robustness.py::test_every_optimus_key_pattern_is_persistent_but_the_settings_cache_is_not` | L7 |
| `optimus/tests/test_persistent_cache_keys.py::test_settings_cache_is_still_cleared` | D | `optimus/tests/test_operations_robustness.py::test_every_optimus_key_pattern_is_persistent_but_the_settings_cache_is_not` | L7 |

### test_phase2_integration.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_phase2_integration.py::TestPhase2Lifecycle::test_is_active_returns_run_uuid_after_start` | D | `optimus/tests/test_line_profile_job_capture.py::test_bg_job_line_capture_end_to_end` (+2) | L6 |
| `optimus/tests/test_phase2_integration.py::TestPhase2Lifecycle::test_is_active_returns_none_after_stop` | D | `optimus/tests/test_phase2_capture_security.py::test_stop_clears_only_matching_serialized_generation` (+1) | L6 |
| `optimus/tests/test_phase2_integration.py::TestPhase2Lifecycle::test_start_persists_picks_and_source_to_redis` | D | `optimus/tests/test_phase2_capture_security.py::test_competing_starts_publish_only_one_complete_capture` (+1) | L6 |

### test_phase2_own_code.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_phase2_own_code.py::TestWrapperChain::test_a_wrapped_pick_resolves_to_the_real_file_and_line` | D | `optimus/tests/test_phase2_own_code.py::TestHitsJoin::test_a_decorator_from_another_module_gets_hits_on_the_users_file` | L6 |
| `optimus/tests/test_phase2_own_code.py::TestOptimusCodeIsNotProfiled::test_a_typed_optimus_function_is_ineligible_with_a_fixed_reason` | D | `optimus/tests/test_phase2_own_code.py::TestOptimusCodeIsNotProfiled::test_the_reason_is_visible_in_the_prepared_metadata_without_paths` (+1) | L6 |
| `optimus/tests/test_phase2_own_code.py::TestOptimusCodeIsNotProfiled::test_a_frappe_style_function_wrapped_by_optimus_is_eligible_and_points_at_the_original` | D | `optimus/tests/test_phase2_own_code.py::TestHitsJoin::test_a_make_wrap_target_gets_hits_on_the_real_file` | L6 |
| `optimus/tests/test_phase2_own_code.py::TestOptimusCodeIsNotProfiled::test_a_user_module_with_optimus_in_its_path_stays_eligible` | D | `optimus/tests/test_phase2_own_code.py::TestHitsJoin::test_a_plain_function_and_a_user_optimus_folder_still_get_hits` (+1) | L6 |
| `optimus/tests/test_phase2_own_code.py::TestOptimusCodeIsNotProfiled::test_the_no_eligible_error_is_a_capture_error_so_the_manual_path_still_shows_it` | D | `optimus/tests/test_phase2_own_code.py::TestOptimusCodeIsNotProfiled::test_all_ineligible_lists_at_most_five_picks_and_no_filesystem_paths` (+3) | L6 |

### test_pill_state_js.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_pill_state_js.py::test_a_missing_node_fails_in_ci_and_skips_elsewhere_with_a_reason` | D | no contract | L3 |
| `optimus/tests/test_pill_state_js.py::test_widget_routes_phase_1_and_2_events_through_reducer` | D | `optimus/tests/test_realtime_ui.py::test_widget_deduplicates_room_delivery_and_follows_only_its_actor` | L3 |

### test_profiler_settings_validation.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_profiler_settings_validation.py::TestFrameworkAppWarning::test_warns_when_erpnext_added` | D | `optimus/tests/test_profiler_settings_validation.py::TestFrameworkAppWarning::test_warns_once_for_both_framework_apps` (+1) | L7 |
| `optimus/tests/test_profiler_settings_validation.py::TestFrameworkAppWarning::test_no_warning_for_empty_tracked_apps` | D | `optimus/tests/test_profiler_settings_validation.py::TestFrameworkAppWarning::test_no_warning_for_custom_apps_only` (+1) | L7 |
| `optimus/tests/test_profiler_settings_validation.py::TestNumericFloorClamp::test_unset_fields_ignored` | C | `optimus/tests/test_profiler_settings_validation.py::TestNumericFloorClamp::test_clamps_negative_sampler_interval_to_floor` | L7 |

### test_projected_post_fix_timing.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_projected_post_fix_timing.py::TestNplusOneProjection::test_n_plus_one_small_count_no_speedup_label` | C | `optimus/tests/test_projected_post_fix_timing.py::TestNplusOneProjection::test_n_plus_one_projects_batched_query_cost` | L9b |
| `optimus/tests/test_projected_post_fix_timing.py::TestRendering::test_finding_without_projection_does_not_render_line` | D | `optimus/tests/test_projected_post_fix_timing.py::TestRendering::test_projection_not_rendered_even_when_present` | L9b |

### test_provider_high_pass.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_provider_high_pass.py::test_reask_false_spellings` | C | `optimus/tests/test_ai_fix.py::TestGuardedCompletion::test_reask_knob` | L8a |

### test_queries_per_action_scope.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_queries_per_action_scope.py::test_user_frame_outside_an_optimus_frame_is_still_own` | D | `optimus/tests/test_own_query_predicate.py::TestOwnHooksInsideUserFlows::test_error_log_hook_read_inside_log_error_is_own` | L4 |
| `optimus/tests/test_queries_per_action_scope.py::test_strip_removes_only_own_calls_and_counts` | D | `optimus/tests/test_own_query_predicate.py::TestSharedByEverySurface::test_strip_keeps_frappe_only_wrapper_queries_and_drops_own_ones` | L4 |
| `optimus/tests/test_queries_per_action_scope.py::test_other_analyzers_never_see_optimus_queries` | D | `optimus/tests/test_own_query_predicate.py::TestSharedByEverySurface::test_top_queries_drops_an_own_hook_query_inside_a_user_flow_and_keeps_the_users` | L4 |
| `optimus/tests/test_queries_per_action_scope.py::test_section_callsite_is_user_frame_not_innermost_framework_frame` | D | `optimus/tests/test_queries_per_action_function_column.py::TestRowFunction::test_function_and_callsite_come_from_the_same_frame` | L4 |
| `optimus/tests/test_queries_per_action_scope.py::test_rendered_report_has_no_optimus_query_exclusion_mode` | D | `optimus/tests/test_queries_per_action_scope.py::test_rendered_report_inclusion_mode_hides_untracked_apps` | L4 |
| `optimus/tests/test_queries_per_action_scope.py::test_callsite_treats_myoptimus_app_as_user_code` | D | `optimus/tests/test_queries_per_action_scope.py::test_user_app_optimus_folder_is_not_the_profiler` | L4 |

### test_query_table_layout.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_query_table_layout.py::TestTemplateStructure::test_top_queries_table_has_query_table_class` | D | `optimus/tests/test_query_table_layout.py::TestEndToEndRender::test_top_queries_renders_with_long_callsite` | L4 |
| `optimus/tests/test_query_table_layout.py::TestTemplateStructure::test_top_queries_has_colgroup_with_widths` | D | `optimus/tests/test_query_table_layout.py::TestEndToEndRender::test_top_queries_renders_with_long_callsite` | L4 |
| `optimus/tests/test_query_table_layout.py::TestTemplateCSS::test_vitals_table_no_longer_overrides_scope_tag_display` | D | no contract | L4 |

### test_realtime_session_events.py (file deleted)

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_realtime_session_events.py::test_stop_session_publishes_stopping_event` | C | `optimus/tests/test_api_session_lifecycle.py::TestStop::test_async_stop_marks_stopping_and_queues_analyze` | L2 |
| `optimus/tests/test_realtime_session_events.py::test_analyze_run_publishes_analyzing_event` | C | `optimus/tests/test_analyze_delivery_guards.py::test_a_run_tells_the_widget_it_is_analyzing_and_then_how_it_ended` | L2 |
| `optimus/tests/test_realtime_session_events.py::test_analyze_run_publishes_failed_event` | C | `optimus/tests/test_analyze_delivery_guards.py::test_generic_failure_stores_its_type_for_the_session_form` | L2 |
| `optimus/tests/test_realtime_session_events.py::test_analyze_run_publishes_ready_event` | C | `optimus/tests/test_analyze_delivery_guards.py::test_a_run_tells_the_widget_it_is_analyzing_and_then_how_it_ended` | L2 |
| `optimus/tests/test_realtime_session_events.py::test_publish_session_event_helper_exists_in_both_layers` | D | `optimus/tests/test_realtime_audience.py::test_all_paths_use_recording_user_and_document_rooms` | L2 |
| `optimus/tests/test_realtime_session_events.py::test_publish_session_event_catches_exceptions` | C | `optimus/tests/test_realtime_audience.py::test_a_failing_realtime_bridge_never_breaks_the_caller` | L2 |
| `optimus/tests/test_realtime_session_events.py::test_widget_no_longer_polls_status_on_interval` | D | no contract; behaviour in `optimus/tests/test_pill_state_js.py::test_widget_polling_and_resync_in_node` | L2 |
| `optimus/tests/test_realtime_session_events.py::test_widget_has_no_polling_helpers` | D | no contract; behaviour in `optimus/tests/test_pill_state_js.py::test_widget_polling_and_resync_in_node` | L2 |
| `optimus/tests/test_realtime_session_events.py::test_widget_subscribes_to_all_realtime_events` | D | `optimus/tests/test_realtime_ui.py::test_widget_deduplicates_room_delivery_and_follows_only_its_actor` | L2 |
| `optimus/tests/test_realtime_session_events.py::test_widget_visibility_handler_only_refreshes_once` | D | `optimus/tests/test_pill_state_js.py::test_widget_resyncs_from_server_and_polls` (+1) | L2 |
| `optimus/tests/test_realtime_session_events.py::test_widget_init_calls_status_only_once` | D | no contract; behaviour in `optimus/tests/test_pill_state_js.py::test_widget_polling_and_resync_in_node` | L2 |

### test_recorder_redaction_patch.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_recorder_redaction_patch.py::TestPatchInstalled::test_patch_marker_on_register` | D | `optimus/tests/test_recorder_redaction_patch.py::TestPatchInstalled::test_patch_is_idempotent` (+1) | L10 |
| `optimus/tests/test_recorder_redaction_patch.py::TestPatchInstalled::test_patch_marker_on_init` | D | `optimus/tests/test_redaction_url_wiring.py::TestCapture::test_key_bearing_endpoint_masks_the_bare_key_in_form_dict` | L10 |
| `optimus/tests/test_recorder_redaction_patch.py::TestInitRedaction::test_form_dict_with_sensitive_keys_redacted` | D | `optimus/tests/test_redaction.py::TestRedactSensitive::test_flat_dict_redacts_default_keys` | L10 |
| `optimus/tests/test_recorder_redaction_patch.py::TestSettingsDrivenExtras::test_extra_form_keys_take_effect` | D | `optimus/tests/test_redaction.py::TestRedactSensitive::test_extra_keys_extend_redaction` | L10 |

### test_redaction.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_redaction.py::TestKeyLiterals::test_the_minimum_length_and_placeholder_are_public` | D | `optimus/tests/test_redaction.py::TestScrubSecrets::test_a_literal_of_exactly_the_minimum_length_is_masked` | L10 |

### test_redaction_logging_failsafe.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_redaction_logging_failsafe.py::TestReadExtrasLogging::test_a_failing_settings_read_is_logged_once` | C | `optimus/tests/test_sql_redaction_logging.py::test_unreadable_settings_are_logged_once_and_defaults_still_mask` | L10 |

### test_redaction_url_nested_and_capture.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_redaction_url_nested_and_capture.py::TestMinors::test_the_string_helper_is_public` | D | `optimus/tests/test_redaction_urls.py::TestTokenisedUrlPass::test_urls_inside_text_are_masked` | L10 |

### test_redaction_urls.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_redaction_urls.py::TestRedactSensitiveUrlPass::test_json_strings_keep_their_own_path` | D | `optimus/tests/test_redaction_urls.py::TestTokenisedUrlPass::test_valid_json_unchanged_stays_unchanged` | L10 |

### test_redis_sweeps.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_redis_sweeps.py::test_a_cleanup_that_failed_is_not_counted_or_logged_as_done` | C | `optimus/tests/test_redis_sweeps.py::test_a_failed_delete_keeps_the_set_for_the_next_run` | L7 |
| `optimus/tests/test_redis_sweeps.py::test_one_run_cleans_at_most_max_deletions_sessions` | D | `optimus/tests/test_redis_sweeps.py::TestOrphanSweepFairness::test_a_run_still_makes_at_most_max_attempts` | L7 |

### test_redundant_calls.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_redundant_calls.py::test_no_finding_below_threshold` | D | `optimus/tests/test_redundant_calls.py::test_a_bucket_below_the_threshold_is_dropped_without_a_warning` | L9b |
| `optimus/tests/test_redundant_calls.py::test_the_stamp_constants_live_in_analyzers_base` | D | `optimus/tests/test_ai_pre_callsite_fix_gate.py::test_fresh_analyzer_output_reaches_the_ai` | L9b |

### test_regenerate_dialog_text.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_regenerate_dialog_text.py::test_the_ai_switch_the_pointer_reads_is_the_one_the_ai_button_uses` | C | `optimus/tests/test_ai_refresh_ui.py::test_real_dialogs_preserve_opt_in_and_cancel_replies_cannot_regress_newer_state` | L3 |
| `optimus/tests/test_regenerate_dialog_text.py::test_the_dialog_text_is_translatable` | C | `optimus/tests/test_regenerate_dialog_text.py::test_the_dialog_says_what_regenerate_does_and_does_not_do` | L3 |

### test_regenerate_reports_api.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_regenerate_reports_api.py::test_render_helper_rejects_the_removed_ai_side_effect_option` | D | `optimus/tests/test_regenerate_reports_api.py::test_regenerate_reports_never_reaches_the_ai_provider` (+1) | L8b |
| `optimus/tests/test_regenerate_reports_api.py::test_the_provider_trap_is_armed` | D | `optimus/tests/test_regenerate_reports_api.py::test_regenerate_reports_never_reaches_the_ai_provider` | L8b |
| `optimus/tests/test_regenerate_reports_api.py::test_regenerate_reports_allows_failed_sessions` | D | `optimus/tests/test_regenerate_reports_api.py::test_regenerate_reports_never_reaches_the_ai_provider` | L8b |

### test_remove_dedicated_codex_home_patch.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_remove_dedicated_codex_home_patch.py::test_running_twice_is_safe` | D | `optimus/tests/test_remove_dedicated_codex_home_patch.py::test_the_orphaned_dedicated_home_and_its_token_are_removed` (+1) | L7 |

### test_rename_phase_two_doctype_patch.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_rename_phase_two_doctype_patch.py::TestRenamePhaseTwoDoctypePatch::test_no_op_when_new_already_exists_alone` | D | `optimus/tests/test_rename_phase_two_doctype_patch.py::TestRenamePhaseTwoDoctypePatch::test_no_op_when_neither_exists` | L7 |

### test_render_drilldown.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_render_drilldown.py::TestDrilldownRender::test_finding_without_matching_tree_node_renders_placeholder` | D | `optimus/tests/test_render_drilldown.py::TestDrilldownHeadingWithBothBlocks::test_neither_block_keeps_single_placeholder_heading` | L4 |

### test_render_duration_threshold.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_render_duration_threshold.py::TestDurationMarkers::test_marker_is_exact_prose_and_urls_untouched` | D | `optimus/tests/test_render_duration_threshold.py::TestUrlsAreNotMangled::test_real_duration_converts_url_stays_intact` | L4 |
| `optimus/tests/test_render_duration_threshold.py::TestDurationMarkers::test_format_durations_also_handles_free_prose` | D | `optimus/tests/test_render_duration_threshold.py::TestFindingTitleThreshold::test_title_rolls_to_seconds_at_default` | L4 |
| `optimus/tests/test_render_duration_threshold.py::TestHighThreshold::test_threshold_above_all_values_keeps_everything_in_ms` | D | `optimus/tests/test_render_duration_threshold.py::TestDefaultThreshold::test_slow_row_renders_in_seconds` | L4 |
| `optimus/tests/test_render_duration_threshold.py::TestCodeReviewFixes::test_legacy_raw_ms_title_agrees_with_badge` | D | `optimus/tests/test_render_duration_threshold.py::TestFindingTitleThreshold::test_title_rolls_to_seconds_at_default` | L4 |
| `optimus/tests/test_render_duration_threshold.py::TestFinalizeProse::test_scan_true_rolls_raw_ms_before_em_dash_sweep` | D | `optimus/tests/test_render_duration_threshold.py::TestCodeReviewFixes::test_note_duration_before_em_dash_rolls_over` | L4 |
| `optimus/tests/test_render_duration_threshold.py::TestFinalizeProse::test_scan_false_is_marker_only` | D | `optimus/tests/test_render_duration_threshold.py::TestCodeReviewFixes::test_stored_summary_fallback_preserves_threshold_caption` | L4 |

### test_render_phase2_panel.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_render_phase2_panel.py::TestRenderPhase2PanelRefinements::test_no_picks_line` | D | no contract | L4 |

### test_renderer_v3.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_renderer_v3.py::test_render_raw_shows_full_app_names` | D | `optimus/tests/test_renderer_v3.py::test_render_raw_with_donut_and_hot_frames` | L4 |

### test_report_a11y.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_report_a11y.py::test_back_to_top_links_removed_find_in_page_note_kept` | rename | `optimus/tests/test_report_a11y.py::test_find_in_page_note_kept_in_how_to_read` | L4 |

### test_report_context.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_report_context.py::TestWebVitalClass::test_cls_thresholds` | D | `optimus/tests/test_report_context.py::TestFrontendShape::test_web_vital_classes_computed` | L4 |
| `optimus/tests/test_report_context.py::TestTopLevelKeys::test_all_19_contract_keys_present` | D | `optimus/tests/test_report_data_schema.py::test_built_report_data_matches_schema` | L4 |
| `optimus/tests/test_report_context.py::TestTopLevelKeys::test_empty_session_renders_without_error` | D | `optimus/tests/test_report_context.py::TestSessionShape::test_session_has_contract_fields` (+2) | L4 |
| `optimus/tests/test_report_context.py::TestKpisShape::test_kpis_are_exactly_4` | D | `optimus/tests/test_report_context.py::TestKpisShape::test_kpi_labels_match_template_convention` | L4 |
| `optimus/tests/test_report_context.py::TestWaterfallShape::test_bg_kind_when_bg_flag_set` | D | `optimus/tests/test_report_a11y.py::test_waterfall_has_text_severity_labels` | L4 |
| `optimus/tests/test_report_context.py::TestHowToReadItems::test_omitted_for_now` | D | no contract | L4 |
| `optimus/tests/test_report_context.py::TestRowDangerThreshold::test_bar_kind_uses_the_fixed_threshold` | D | `optimus/tests/test_report_context.py::TestBarKindFor::test_between_300_and_1000_returns_warn` | L4 |
| `optimus/tests/test_report_context.py::TestResourceRowNumbers::test_no_gaps_starts_at_one` | D | `optimus/tests/test_report_context.py::TestResourceRowNumbers::test_gaps_keep_actions_table_numbers` | L4 |

### test_report_file_save.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_report_file_save.py::test_save_report_file_clears_request_around_insert` | D | `optimus/tests/test_report_file_save.py::test_save_recording_file_failure_is_logged_safely_and_remains_optional` (+1) | L2 |
| `optimus/tests/test_report_file_save.py::test_save_report_file_restore_is_in_finally` | D | `optimus/tests/test_report_file_save.py::test_save_recording_file_failure_is_logged_safely_and_remains_optional` (+1) | L2 |

### test_retry_phase2_batch.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_retry_phase2_batch.py::TestRetryPhase2BatchEndpoint::test_whitelisted` | D | `optimus/tests/test_api_gate_audit.py::test_api_passes_the_audit` | L6 |
| `optimus/tests/test_retry_phase2_batch.py::TestRetryPhase2BatchEndpoint::test_accepts_json_string_or_python_list` | C | `optimus/tests/test_api_additional_hardening.py::test_batch_retry_hides_exception_text_and_continues` (+2) | L6 |
| `optimus/tests/test_retry_phase2_batch.py::TestRetryPhase2BatchEndpoint::test_rejects_empty_or_non_list` | C | `optimus/tests/test_api_review_limits.py::test_batch_refuses_an_empty_or_malformed_payload` | L6 |
| `optimus/tests/test_retry_phase2_batch.py::TestRetryPhase2BatchEndpoint::test_isolates_per_row_failures` | D | `optimus/tests/test_api_additional_hardening.py::test_batch_retry_hides_exception_text_and_continues` | L6 |
| `optimus/tests/test_retry_phase2_batch.py::TestRetryPhase2BatchEndpoint::test_returns_aggregate_tallies` | C | `optimus/tests/test_api_phase2_endpoints.py::test_batch_isolates_a_foreign_run` (+1) | L6 |
| `optimus/tests/test_retry_phase2_batch.py::TestRetryPhase2BatchEndpoint::test_delegates_to_retry_phase2_analyze` | D | `optimus/tests/test_api_additional_hardening.py::test_batch_retry_hides_exception_text_and_continues` (+1) | L6 |

### test_review_fixes.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_review_fixes.py::TestDrainKeepsCapturingStatus::test_bg_wait_does_not_flip_to_analyzing` | C | `optimus/tests/test_background_job_capture.py::TestBgWaitForPendingJobs::test_reenqueues_when_jobs_still_running` | L1 |
| `optimus/tests/test_review_fixes.py::TestStopSetsCapturingStatus::test_stop_session_sets_status_and_commits` | D | `optimus/tests/test_api_session_lifecycle.py::TestStop::test_background_jobs_open_a_draining_window_and_status` | L1 |
| `optimus/tests/test_review_fixes.py::TestDrainProgressEndpoint::test_permission_gated_and_window_math` | C | `optimus/tests/test_api_session_lifecycle.py::TestDrainProgress` | L1 |
| `optimus/tests/test_review_fixes.py::TestLoadRecordingsBundleCorrupt::test_corrupt_gzip_returns_none` | C | `optimus/tests/test_recording_bundle_security.py::test_invalid_gzip_json_or_ambiguous_keys_are_rejected` | L1 |

### test_root_cause_grouping.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_root_cause_grouping.py::TestRootCauseKey::test_hot_line_uses_own_callsite` | D | `optimus/tests/test_root_cause_grouping.py::TestRootCauseKey::test_falls_back_to_callsite_when_no_chain` | L4 |
| `optimus/tests/test_root_cause_grouping.py::TestRootCauseKey::test_redundant_call_uses_own_callsite` | D | `optimus/tests/test_root_cause_grouping.py::TestRootCauseKey::test_falls_back_to_callsite_when_no_chain` | L4 |
| `optimus/tests/test_root_cause_grouping.py::TestGrouping::test_singleton_finding_unchanged` | D | `optimus/tests/test_root_cause_grouping.py::TestGrouping::test_different_leaves_do_not_group` | L4 |

### test_safe_call.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_safe_call.py::test_safe_call_holds_the_one_copy_of_the_interrupt_idiom` | D | no contract; behaviour in `optimus/tests/test_safe_call.py::test_a_job_timeout_escapes_as_a_fresh_instance` | L2 |

### test_scheduler_inline_fallback.py (file deleted)

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_scheduler_inline_fallback.py::test_enqueue_analyze_checks_scheduler` | D | `optimus/tests/test_api_session_lifecycle.py::TestEnqueueAnalyzeInlineCap::test_scheduler_probe_failure_falls_back_to_the_async_queue` (+1) | L5 |
| `optimus/tests/test_scheduler_inline_fallback.py::test_stop_returns_ran_inline_flag` | D | `optimus/tests/test_api_session_lifecycle.py::TestStop::test_async_stop_marks_stopping_and_queues_analyze` (+1) | L5 |
| `optimus/tests/test_scheduler_inline_fallback.py::test_enqueue_analyze_honors_inline_analyze_limit` | D | `optimus/tests/test_api_session_lifecycle.py::TestEnqueueAnalyzeInlineCap::test_oversized_session_is_marked_failed_with_retry_guidance_and_not_analyzed` (+1) | L5 |
| `optimus/tests/test_scheduler_inline_fallback.py::test_enqueue_analyze_passes_now_when_scheduler_disabled` | D | `optimus/tests/test_api_session_lifecycle.py::TestEnqueueAnalyzeInlineCap::test_site_config_can_raise_the_inline_cap` (+1) | L5 |
| `optimus/tests/test_scheduler_inline_fallback.py::test_retry_analyze_uses_scheduler_aware_enqueue` | D | `optimus/tests/test_api_session_actions.py::test_owner_retries_a_failed_session` | L5 |
| `optimus/tests/test_scheduler_inline_fallback.py::test_enqueue_analyze_swallows_inline_failure` | D | `optimus/tests/test_api_session_lifecycle.py::TestEnqueueAnalyzeInlineCap::test_inline_analyze_crash_is_swallowed_so_stop_does_not_500` | L5 |
| `optimus/tests/test_scheduler_inline_fallback.py::test_stop_returns_final_status_when_inline` | D | `optimus/tests/test_api_session_lifecycle.py::TestStop::test_stop_flags_an_empty_inline_session` (+1) | L5 |
| `optimus/tests/test_scheduler_inline_fallback.py::test_enqueue_analyze_blocks_huge_inline_session` | C | `optimus/tests/test_api_session_lifecycle.py::TestEnqueueAnalyzeInlineCap::test_oversized_session_is_marked_failed_with_retry_guidance_and_not_analyzed` | L5 |
| `optimus/tests/test_scheduler_inline_fallback.py::test_enqueue_analyze_cap_is_called_by_stop_session` | C | `optimus/tests/test_api_session_lifecycle.py::TestEnqueueAnalyzeInlineCap::test_oversized_session_is_marked_failed_with_retry_guidance_and_not_analyzed` | L5 |
| `optimus/tests/test_scheduler_inline_fallback.py::test_enqueue_analyze_cap_is_called_by_retry_analyze` | C | `optimus/tests/test_api_session_lifecycle.py::TestEnqueueAnalyzeInlineCap::test_oversized_session_is_marked_failed_with_retry_guidance_and_not_analyzed` | L5 |
| `optimus/tests/test_scheduler_inline_fallback.py::test_stop_flags_an_empty_inline_session` | move | `optimus/tests/test_api_session_lifecycle.py::TestStop::test_stop_flags_an_empty_inline_session` | L5 |

### test_scrub_properties.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_scrub_properties.py::TestMaskedRecord::test_a_shape_the_join_completes_is_masked` | D | `optimus/tests/test_maintenance_scrub.py::TestMaskedRecord::test_a_shape_the_long_title_and_the_error_complete_together_is_masked` (+1) | L10 |

### test_server_script_render.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_server_script_render.py::TestExtractScriptName::test_is_server_script_filename_covers_named_and_bare` | D | no contract | L4 |
| `optimus/tests/test_server_script_render.py::TestGetServerScriptLines::test_splits_body_into_lines` | D | `optimus/tests/test_source_path_resolution.py::test_every_source_reader_uses_the_newline_only_splitter` | L4 |
| `optimus/tests/test_server_script_render.py::TestCallTreeDisplay::test_display_label_for_bare_server_script_keeps_fallback` | D | `optimus/tests/test_display_name_for_node.py::TestServerScriptDetection::test_serverscript_filename_pattern` | L4 |
| `optimus/tests/test_server_script_render.py::TestAppBucketing::test_bare_server_script_buckets_to_server_scripts` | D | `optimus/tests/test_server_script_render.py::TestAppBucketing::test_named_server_script_buckets_to_server_scripts` | L4 |
| `optimus/tests/test_server_script_render.py::TestRendererSnippetAndLink::test_resolve_source_path_returns_server_script_sentinel` | D | `optimus/tests/test_server_script_render.py::TestRendererSnippetAndLink::test_read_source_snippet_returns_window_for_server_script` | L4 |
| `optimus/tests/test_server_script_render.py::TestServerScriptThroughDefExpansion::test_decorator_through_def_rows_reads_server_script_body` | D | `optimus/tests/test_server_script_render.py::TestServerScriptThroughDefExpansion::test_expand_self_time_snippet_inlines_server_script_body` | L4 |

### test_session_jobs.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_session_jobs.py::test_set_job_recording_and_status_merge` | D | `optimus/tests/test_session_jobs.py::TestAtomicMergeJobMeta::test_merge_preserves_existing_fields` (+1) | L5 |
| `optimus/tests/test_session_jobs.py::TestAtomicMergeJobMeta::test_setdefault_writes_when_field_absent` | D | `optimus/tests/test_session_jobs.py::TestAtomicMergeJobMeta::test_setdefault_preserves_first_write` | L5 |
| `optimus/tests/test_session_jobs.py::TestAtomicMergeJobMeta::test_empty_session_or_job_id_is_safe` | D | `optimus/tests/test_session_jobs.py::test_empty_session_uuid_is_safe` | L5 |

### test_session_meta.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_session_meta.py::test_set_session_meta_default_when_not_specified` | D | `optimus/tests/test_hooks_request_job_flow.py::TestBeforeRequest::test_active_session_activates_recorder_infra_and_pyinstrument` (+1) | L5 |

### test_session_ready_popup.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_session_ready_popup.py::test_popup_failure_is_logged_with_console_warn` | C | `optimus/tests/test_session_ready_popup.py::test_popup_failure_does_not_block_state_transition` | L3 |

### test_settings.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_settings.py::TestDisplayThresholdMs::test_returns_resolved_value` | D | `optimus/tests/test_settings.py::TestDisplayThresholdMs::test_preserves_zero` | L7 |

### test_settings_ai.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_settings_ai.py::TestAiConfigDefaults::test_defaults_dict_has_the_ai_keys` | D | `optimus/tests/test_settings.py::TestDefaults::test_config_has_defaults_when_doctype_missing` (+1) | L7 |

### test_settings_envelope_rollout.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_settings_envelope_rollout.py::TestSettingsEnvelopeReadHappyPath::test_hit_on_enveloped_value_returns_config` | D | `optimus/tests/test_settings_request_memo.py::test_a_redis_hit_is_memoized_without_touching_the_database` (+1) | L7 |

### test_settings_needs_key.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_settings_needs_key.py::test_provider_needs_key_reads_the_provider_table` | D | `optimus/tests/test_settings_needs_key.py::test_provider_needs_key_strips_and_defaults_to_true` (+1) | L7 |
| `optimus/tests/test_settings_needs_key.py::test_hosted_provider_without_a_key_warns` | D | `optimus/tests/test_settings_ai_config_warning.py::TestIncompleteAiConfigWarning::test_missing_key_is_reported_singular` | L7 |
| `optimus/tests/test_settings_needs_key.py::test_keyless_local_endpoint_is_not_asked_for_a_key` | D | `optimus/tests/test_settings_ai_config_warning.py::TestIncompleteAiConfigWarning::test_fully_configured_custom_provider_is_silent` | L7 |
| `optimus/tests/test_settings_needs_key.py::test_resolve_falls_back_to_the_defaults_for_blank_cli_fields` | C | `optimus/tests/test_settings_needs_key.py::test_resolve_defaults_the_cli_fields_to_api_key_mode` | L7 |

### test_settings_profile.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_settings_profile.py::TestProfileTable::test_custom_is_not_a_named_profile` | D | `optimus/tests/test_settings_profile.py::TestProfileResolution::test_custom_reads_stored_values` | L7 |
| `optimus/tests/test_settings_profile.py::TestProfileTable::test_every_sensitivity_key_is_categorised` | D | no contract | L7 |

### test_settings_request_memo.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_settings_request_memo.py::test_a_config_written_to_redis_is_memoized` | D | `optimus/tests/test_settings_commit_cache.py::test_lost_after_commit_callback_is_bounded_by_ttl` (+1) | L7 |

### test_settings_round6.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_settings_round6.py::TestOptimusConfigDefaults::test_round6_fields_have_sane_defaults` | C | `optimus/tests/test_settings.py::TestDefaults::test_config_has_defaults_when_doctype_missing` | L1 |
| `optimus/tests/test_settings_round6.py::TestParseSkipList::test_single_line` | D | `optimus/tests/test_ai_privacy.py::TestExcludeParsing::test_parses_one_type_per_line` | L1 |
| `optimus/tests/test_settings_round6.py::TestParseSkipList::test_blank_lines_dropped` | D | `optimus/tests/test_ai_privacy.py::TestExcludeParsing::test_strips_comments_and_blanks` | L1 |
| `optimus/tests/test_settings_round6.py::TestParseSkipList::test_hash_comments_dropped` | D | `optimus/tests/test_ai_privacy.py::TestExcludeParsing::test_strips_comments_and_blanks` | L1 |
| `optimus/tests/test_settings_round6.py::TestParseSkipList::test_returns_tuple_not_list` | D | `optimus/tests/test_ai_privacy.py::TestExcludeParsing::test_parses_one_type_per_line` | L1 |
| `optimus/tests/test_settings_round6.py::TestSlowQueryThresholdResolver::test_default_from_settings` | D | `optimus/tests/test_settings_round6.py::TestSlowQueryThresholdResolver::test_override_propagates` | L1 |
| `optimus/tests/test_settings_round6.py::TestRendererMinActionFilter::test_zero_threshold_keeps_all_actions` | D | `optimus/tests/test_settings_round6.py::TestRendererMinActionFilter::test_threshold_drops_short_actions` | L1 |
| `optimus/tests/test_settings_round6.py::TestExpandHotChainHonorsSettings::test_picker_respects_max_depth_arg` | D | `optimus/tests/test_line_profile_picker.py::TestExpandHotChain::test_stops_at_max_depth` | L1 |
| `optimus/tests/test_settings_round6.py::TestExpandHotChainHonorsSettings::test_picker_respects_min_ms_arg` | D | `optimus/tests/test_line_profile_picker.py::TestExpandHotChain::test_stops_at_min_ms_floor` | L1 |

### test_skip_instrumentation.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_skip_instrumentation.py::test_should_skip_frappe_profiler_status_poll` | D | `optimus/tests/test_skip_instrumentation.py::test_should_skip_all_frappe_profiler_api_methods` | L5 |
| `optimus/tests/test_skip_instrumentation.py::test_path_based_skip_for_frappe_recorder_methods` | D | `optimus/tests/test_skip_instrumentation.py::test_path_based_skip_for_v1_method_url` (+1) | L5 |
| `optimus/tests/test_skip_instrumentation.py::test_path_based_ignores_rest_resource_urls` | D | `optimus/tests/test_skip_instrumentation.py::test_extract_cmd_returns_empty_string_on_non_method_url` (+1) | L5 |
| `optimus/tests/test_skip_instrumentation.py::test_path_based_ignores_desk_app_urls` | D | `optimus/tests/test_skip_instrumentation.py::test_extract_cmd_returns_empty_string_on_non_method_url` (+1) | L5 |
| `optimus/tests/test_skip_instrumentation.py::test_path_based_handles_missing_request` | D | `optimus/tests/test_skip_instrumentation.py::test_should_not_skip_page_loads_without_cmd` (+1) | L5 |
| `optimus/tests/test_skip_instrumentation.py::test_before_request_early_exits_on_skipped_cmd` | C | `optimus/tests/test_hooks_request_job_flow.py::TestBeforeRequest::test_profiler_own_api_calls_are_not_recorded` | L5 |

### test_slow_hot_path_pin.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_slow_hot_path_pin.py::test_wrong_function_hot_line_does_not_pin` | D | `optimus/tests/test_slow_hot_path_pin.py::test_compute_leaf_without_related_finding_untouched` | L4 |

### test_slow_query_full_sql_render.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_slow_query_full_sql_render.py::test_capped_query_is_flagged_in_the_label_and_the_note_is_visible` | D | `optimus/tests/test_slow_query_full_sql_render.py::test_analyzer_output_renders_end_to_end_with_the_truncation_note` | L4 |
| `optimus/tests/test_slow_query_full_sql_render.py::TestRedactFindingSql::test_redacts_sensitive_literal` | D | `optimus/tests/test_slow_query_full_sql_render.py::test_slow_query_card_shows_entire_long_query_redacted_and_escaped` (+1) | L4 |

### test_source_path_resolution.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_source_path_resolution.py::TestResolveSourcePath::test_resolves_a_frappe_core_relative_path` | D | `optimus/tests/test_source_access_security.py::test_relative_and_parent_paths_use_real_boundary` | L4 |
| `optimus/tests/test_source_path_resolution.py::TestResolveSourcePath::test_passes_through_existing_absolute_path` | D | `optimus/tests/test_source_access_security.py::test_normal_app_template_and_library_sources_remain_readable` | L4 |
| `optimus/tests/test_source_path_resolution.py::TestResolveSourcePath::test_none_for_missing_absolute` | D | `optimus/tests/test_source_path_resolution.py::TestResolveSourcePath::test_none_for_nonexistent_app_relative` | L4 |

### test_sql_redaction_hardening.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_sql_redaction_hardening.py::test_ai_gate_names_are_exactly_the_old_list` | D | `optimus/tests/test_sql_redaction_integration.py::test_the_ai_gate_names_are_frozen_to_the_old_list` | L10 |
| `optimus/tests/test_sql_redaction_hardening.py::test_register_failure_path_uses_the_shared_constants` | D | `optimus/tests/test_sql_redaction_integration.py::test_register_stores_no_raw_sql_when_redaction_raises` | L10 |

### test_steps_to_reproduce.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_steps_to_reproduce.py::test_notes_field_is_text_editor` | D | no contract | L2 |
| `optimus/tests/test_steps_to_reproduce.py::test_notes_label_reflects_steps_purpose` | D | no contract | L2 |
| `optimus/tests/test_steps_to_reproduce.py::test_api_start_accepts_notes` | D | `optimus/tests/test_api_session_lifecycle.py::TestStart::test_creates_recording_row_meta_and_activates_user` (+1) | L2 |
| `optimus/tests/test_steps_to_reproduce.py::test_api_start_writes_notes_to_doc` | D | `optimus/tests/test_api_session_lifecycle.py::TestStart::test_creates_recording_row_meta_and_activates_user` (+1) | L2 |
| `optimus/tests/test_steps_to_reproduce.py::test_notes_are_bleach_sanitized_before_render` | D | `optimus/tests/test_notes_sanitizer_compat.py::test_rich_notes_survive_legacy_signature_with_resource_tags_removed` | L2 |
| `optimus/tests/test_steps_to_reproduce.py::test_json_shaped_xss_payload_is_sanitized` | D | `optimus/tests/test_notes_sanitizer_compat.py::test_rich_notes_survive_legacy_signature_with_resource_tags_removed` | L2 |
| `optimus/tests/test_steps_to_reproduce.py::test_renderer_passes_always_sanitize_true` | D | `optimus/tests/test_notes_sanitizer_compat.py::test_rich_notes_survive_legacy_signature_with_resource_tags_removed` | L2 |
| `optimus/tests/test_steps_to_reproduce.py::test_renderer_sanitizes_notes_before_template_context` | D | `optimus/tests/test_notes_sanitizer_compat.py::test_rich_notes_survive_legacy_signature_with_resource_tags_removed` | L2 |
| `optimus/tests/test_steps_to_reproduce.py::test_persist_auto_fills_notes_when_field_is_empty` | D | `optimus/tests/test_ai_analyze_isolation.py::test_persistence_writes_deterministic_steps_without_a_provider_call` | L2 |
| `optimus/tests/test_steps_to_reproduce.py::test_auto_notes_real_user_sequence_reads_naturally` | D | `optimus/tests/test_per_action.py::test_humanized_desk_getdoc_is_open_doctype` (+1) | L2 |
| `optimus/tests/test_steps_to_reproduce.py::test_start_dialog_no_longer_asks_for_notes` | D | no contract | L2 |

### test_submit_frontend_metrics.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_submit_frontend_metrics.py::test_submit_appends_to_existing_list` | D | `optimus/tests/test_submit_frontend_metrics.py::test_atomic_append_survives_simulated_race` | L5 |

### test_summary_html.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_summary_html.py::TestSummaryBulletShape::test_summary_wraps_in_ul` | C | `optimus/tests/test_summary_html.py::TestSummaryBulletShape::test_bullet_count_happy_path` | L2 |

### test_table_breakdown.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_table_breakdown.py::TestRenderedSection::test_old_stored_rows_without_write_fields_render` | D | `optimus/tests/test_table_breakdown.py::TestRenderedSection::test_render_handles_old_session_without_new_fields` | L9b |

### test_thinned_trees_warning.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_thinned_trees_warning.py::test_nothing_is_said_when_no_tree_was_thinned` | D | `optimus/tests/test_thinned_trees_warning.py::test_an_old_blob_without_the_attribute_is_not_thinned` (+1) | L2 |

### test_top_queries.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_top_queries.py::test_long_query_is_not_truncated` | D | `optimus/tests/test_top_queries.py::test_long_slow_query_kept_in_full_in_leaderboard_and_finding` | L9b |

### test_tree_size_cap.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_tree_size_cap.py::TestCompaction::test_estimate_is_at_least_the_real_pickle` | D | `optimus/tests/test_tree_size_cap.py::TestThinning::test_estimate_is_calibrated_to_the_real_pickle` | L5 |
| `optimus/tests/test_tree_size_cap.py::TestThinning::test_over_the_byte_budget_thins_until_it_fits` | D | `optimus/tests/test_tree_dump_cap.py::test_the_stored_blob_is_within_the_cap_and_round_trips` (+1) | L5 |
| `optimus/tests/test_tree_size_cap.py::TestPeriodicPatterns::test_thinning_a_jittered_periodic_pattern_keeps_shares` | C | `optimus/tests/test_tree_size_cap.py::TestPeriodicPatterns::test_thinning_a_periodic_pattern_keeps_each_share_within_a_few_points` | L5 |

### test_truncation_banner.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_truncation_banner.py::TestTruncationBannerRenders::test_banner_absent_on_clean_session` | D | `optimus/tests/test_truncation_banner.py::TestTruncationBannerRenders::test_banner_absent_when_warnings_are_not_truncation` | L4 |
| `optimus/tests/test_truncation_banner.py::TestBannerMarkerPrefix::test_warning_prefix_matches_renderer_check` | rename | `optimus/tests/test_truncation_banner.py::TestBannerMarkerPrefix::test_the_warning_analyze_emits_makes_the_banner_render` | L4 |

### test_v5_aggregate_cap.py (file deleted)

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_v5_aggregate_cap.py::test_persist_caps_infra_timeline` | C | `optimus/tests/test_analyze_run_v5_wiring.py::test_persist_keeps_the_tail_of_each_v5_aggregate_list_and_warns` | L2 |
| `optimus/tests/test_v5_aggregate_cap.py::test_persist_caps_frontend_xhr` | C | `optimus/tests/test_analyze_run_v5_wiring.py::test_persist_keeps_the_tail_of_each_v5_aggregate_list_and_warns` | L2 |
| `optimus/tests/test_v5_aggregate_cap.py::test_persist_caps_frontend_orphans` | C | `optimus/tests/test_analyze_run_v5_wiring.py::test_persist_keeps_the_tail_of_each_v5_aggregate_list_and_warns` | L2 |
| `optimus/tests/test_v5_aggregate_cap.py::test_aggregate_warnings_run_before_analyzer_warnings_assembly` | C | `optimus/tests/test_analyze_run_v5_wiring.py::test_persist_keeps_the_tail_of_each_v5_aggregate_list_and_warns` | L2 |

### test_v5_panels_render.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_v5_panels_render.py::test_frontend_panel_renders_full_urls` | D | `optimus/tests/test_v5_panels_render.py::test_frontend_xhr_url_keeps_the_docname` | L4 |
| `optimus/tests/test_v5_panels_render.py::test_raw_mode_keeps_docname_in_urls` | rename | `optimus/tests/test_v5_panels_render.py::test_frontend_xhr_url_keeps_the_docname` | L4 |

### test_waterfall.py

| Test | Mark | Keeper | Lane |
|---|---|---|---|
| `optimus/tests/test_waterfall.py::TestSizing::test_returns_fewer_when_input_is_smaller` | D | `optimus/tests/test_waterfall.py::TestSorting::test_sorted_by_duration_descending` | L4 |
| `optimus/tests/test_waterfall.py::TestPctScaling::test_pct_scales_to_displayed_slice_max` | D | `optimus/tests/test_waterfall.py::TestPctScaling::test_top_row_renders_at_100_percent` | L4 |

## Follow-ups found

1. `ai_guardrails.stored_sql_violations` has no caller; nothing runs the SQL-safety check (kept seam).
2. `redis_keys.ai_refresh_inflight` has no caller; `KEY_PATTERNS` and `docs/REDIS-SCHEMA.md` still describe the lock (kept seam).
3. `analyze._ai_call_timeout` lost its caller in `e4db355` (kept seam).
4. `analyze._note_ai_selection` lost its caller in `e4db355`; the gated or excluded warning is no longer shown (kept seam).
5. `api._ai_session_gate`: the live `refill_ai_suggestions` never runs `ai_fix.is_available()` (kept seam).
6. The two permission-downgrade checks (`ai_guardrails.py:726-732`, `ai_fix_checks.py:585`) mask each other; no test fails if only one is disabled.
7. `_persist` keeps the notes a user wrote; now tested (L2 R-2). No issue needed, mention in the PR.
8. Off-plan test-only names kept: `index_recipes.HOOK_EVENTS`, `_ENSURE_FUNCTION`, `_valued`; settings `read_schema_sentinel`; `analyze._session_increment_query(by=)`; `error_log_mask._TITLE_LIMIT`.
9. `CONTRIBUTING.md:36` says `test_pdf_export.py` has 3 tests; it has 12.
10. Test-order coupling: `test_lock_contention_plumbing.py::test_retried_job_regains_its_session_and_lock_events` depends on state left by `test_call_tree_findings.py::test_hooks_callbacks_before_*_snapshot_runs_before_pyi_start`.
11. `test_settings_envelope_rollout.py::TestSettingsEnvelopeDriftHandling::test_drift_falls_through_to_resolve` could not fail (fake cache `get_value` lacked `expires`); repaired in `827ee11`.
12. Pre-existing: the CLS poor and good thresholds (0.25 and 0.1) are asserted by no test.
13. Pre-existing: the floating widget `visibilitychange` refresh has no behaviour test.
14. Pre-existing AI weak spots: `ai_link_allowed` accepts `github.com/frappe-evil/x`; 0x7F is not in the bad-character tests; `_read_cli_reply` trailing-text and FIFO guards, `_cli_login_ok` Codex memo TTL, auth-retry cache clear and stale-sandbox owner check, `_session_call_metadata` ambient id and the `_kill_process_group` SIGKILL fallback are untested; `@` in `_AI_LINK_BAD_SUBSTRINGS` is redundant.
15. SECURITY, pre-existing, highest priority: the `export_session` ownership check in `optimus/api.py` (about line 1480, "You can only export your own sessions") has no behaviour test; replacing it with `pass` fails nothing. Add a test like `drain_progress`'s `test_permission_gate_runs_before_any_store_read`.
16. Pre-existing: the patched Recorder `sensitive_form_keys` setting (`optimus/__init__.py` about line 423) is tested by nothing; add a twin of `test_extra_sql_columns_take_effect`.
17. Pre-existing: `ai_link_allowed` accepts `github.com/frappe-evil/x` (same as item 14); add it to `_LOOKALIKES`.
18. Pre-existing: no test pins the Error Log titles "optimus scheduler check" and "optimus inline cap mark Failed", or the inline-analyze dedupe key (`api.py` about lines 703-741).
19. Pre-existing: duplicates between `_CLI_DISABLED_FEATURES` as a list and as a set are not caught.
20. Pre-existing: `ai_jobs.py:335`, a provider failure with `sent_unknown` becoming "uncertain" in the worker, is tested only at the store layer. Also untested: the `sql_work` rollback_error message, the janitor retention-delete failure log, and the `analyze.py:725` drain enqueue failure.
21. SECURITY, pre-existing: the `optimus/permissions.py` file hook (lines 112-123): removing `if not doc.attached_to_name: return False` or "create" from the ptype set fails nothing; `test_no_attached_to_name_denies_false` passes for the wrong reason (its fake `get_value` ignores the None name). Fix the fake and add a `ptype="create"` row.
