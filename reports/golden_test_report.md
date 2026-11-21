# RecallLoop Test Report

Full suite: 77/77 passed
Golden cases: 25/25 passed

| Case or test | Scenario | Result | Detail |
|---|---|---|---|
| GL001 | Explicit first person commitment | PASS | passed |
| GL002 | Contracted explicit commitment | PASS | passed |
| GL003 | Offer remains proposed | PASS | passed |
| GL004 | Uncertain intent is preserved | PASS | passed |
| GL005 | Explicit refusal is not confirmed | PASS | passed |
| GL006 | Question does not create commitment | PASS | passed |
| GL007 | Vague actor remains ambiguous | PASS | passed |
| GL008 | Past action is not future commitment | PASS | passed |
| GL009 | Explicit correction supersedes earlier time | PASS | passed |
| GL010 | Tomorrow resolves from reference date | PASS | passed |
| GL011 | Today resolves in Kolkata timezone | PASS | passed |
| GL012 | Weekday resolves deterministically | PASS | passed |
| GL013 | Unsupported relative date stays unresolved | PASS | passed |
| GL014 | No deadline is invented | PASS | passed |
| GL015 | Fabricated due date is rejected | PASS | passed |
| GL016 | Invented actor is rejected | PASS | passed |
| GL017 | Evidence must quote transcript | PASS | passed |
| GL018 | Unsupported confirmation is rejected | PASS | passed |
| GL019 | Unknown status fails schema validation | PASS | passed |
| GL020 | Malformed JSON is rejected | PASS | passed |
| GL021 | Source identity is attached by interface | PASS | passed |
| GL022 | Unknown response fields fail closed | PASS | passed |
| GL023 | Duplicate extraction rows are rejected | PASS | passed |
| GL024 | Unresolvable deadline phrase remains unresolved | PASS | passed |
| GL025 | Records retain history and transcript provenance | PASS | passed |
| test_action_and_history_persist_after_app_restart | test_action_and_history_persist_after_app_restart | PASS | passed |
| test_action_creation_and_approval_policy | test_action_creation_and_approval_policy | PASS | passed |
| test_action_verification_requires_authorized_verifier_and_does_not_change_commitment | test_action_verification_requires_authorized_verifier_and_does_not_change_commitment | PASS | passed |
| test_agora_provider_errors_return_safe_http_errors | test_agora_provider_errors_return_safe_http_errors | PASS | passed |
| test_ambiguous_memory_returns_insufficient_evidence | test_ambiguous_memory_returns_insufficient_evidence | PASS | passed |
| test_approved_cancellation_action_updates_commitment_with_source_evidence | test_approved_cancellation_action_updates_commitment_with_source_evidence | PASS | passed |
| test_cancelled_action_cannot_execute_and_history_is_append_only | test_cancelled_action_cannot_execute_and_history_is_append_only | PASS | passed |
| test_changed_parameters_invalidate_approval | test_changed_parameters_invalidate_approval | PASS | passed |
| test_commitment_verification_requires_configured_authorized_verifier | test_commitment_verification_requires_configured_authorized_verifier | PASS | passed |
| test_completion_claim_stays_pending_until_supported_verification | test_completion_claim_stays_pending_until_supported_verification | PASS | passed |
| test_completion_is_not_inferred_from_a_promise | test_completion_is_not_inferred_from_a_promise | PASS | passed |
| test_conflicting_records_are_not_collapsed_into_an_answer | test_conflicting_records_are_not_collapsed_into_an_answer | PASS | passed |
| test_conflicts_report_deadlines_duplicates_contradictions_and_revisions | test_conflicts_report_deadlines_duplicates_contradictions_and_revisions | PASS | passed |
| test_consequential_execution_is_rejected_without_approval | test_consequential_execution_is_rejected_without_approval | PASS | passed |
| test_correction_history_keeps_both_revisions_and_source | test_correction_history_keeps_both_revisions_and_source | PASS | passed |
| test_correction_history_preserves_old_and_new_versions | test_correction_history_preserves_old_and_new_versions | PASS | passed |
| test_database_persistence_after_reopening | test_database_persistence_after_reopening | PASS | passed |
| test_dependencies_block_until_verified_and_report_missed_deadline | test_dependencies_block_until_verified_and_report_missed_deadline | PASS | passed |
| test_dependency_cycle_is_rejected_and_blocked_tasks_are_detected | test_dependency_cycle_is_rejected_and_blocked_tasks_are_detected | PASS | passed |
| test_dispute_and_verification_require_transcript_evidence | test_dispute_and_verification_require_transcript_evidence | PASS | passed |
| test_due_date_is_not_overdue_until_end_of_local_due_day | test_due_date_is_not_overdue_until_end_of_local_due_day | PASS | passed |
| test_exact_memory_retrieval_includes_original_quote_and_timestamp | test_exact_memory_retrieval_includes_original_quote_and_timestamp | PASS | passed |
| test_existing_task002_database_schema_is_upgraded_in_place | test_existing_task002_database_schema_is_upgraded_in_place | PASS | passed |
| test_failed_stop_remains_retryable | test_failed_stop_remains_retryable | PASS | passed |
| test_fastapi_routes_create_search_recall_commitment_and_history | test_fastapi_routes_create_search_recall_commitment_and_history | PASS | passed |
| test_health_endpoint_checks_database_and_is_public | test_health_endpoint_checks_database_and_is_public | PASS | passed |
| test_http_endpoints_return_validation_and_not_found_codes | test_http_endpoints_return_validation_and_not_found_codes | PASS | passed |
| test_intelligence_api_routes_are_registered_and_callable | test_intelligence_api_routes_are_registered_and_callable | PASS | passed |
| test_invalid_lifecycle_transition_is_rejected | test_invalid_lifecycle_transition_is_rejected | PASS | passed |
| test_lifecycle_transitions_store_acceptance_and_history | test_lifecycle_transitions_store_acceptance_and_history | PASS | passed |
| test_mark_overdue_changes_only_confirmed_past_due_records_and_preserves_history | test_mark_overdue_changes_only_confirmed_past_due_records_and_preserves_history | PASS | passed |
| test_missing_memory_returns_insufficient_evidence | test_missing_memory_returns_insufficient_evidence | PASS | passed |
| test_missing_vendor_credentials_fail_closed | test_missing_vendor_credentials_fail_closed | PASS | passed |
| test_mock_send_succeeds_once_and_is_idempotent | test_mock_send_succeeds_once_and_is_idempotent | PASS | passed |
| test_overdue_and_upcoming_respect_date_only_deadlines_and_kolkata_timezone | test_overdue_and_upcoming_respect_date_only_deadlines_and_kolkata_timezone | PASS | passed |
| test_production_api_requires_key_and_documents_header | test_production_api_requires_key_and_documents_header | PASS | passed |
| test_production_requires_api_key_configuration | test_production_requires_api_key_configuration | PASS | passed |
| test_recovery_options_are_suggestions_and_never_run_actions | test_recovery_options_are_suggestions_and_never_run_actions | PASS | passed |
| test_responsible_person_ambiguous_and_unassigned_views | test_responsible_person_ambiguous_and_unassigned_views | PASS | passed |
| test_retrieval_by_due_date_uses_fts_index | test_retrieval_by_due_date_uses_fts_index | PASS | passed |
| test_retrieval_by_person | test_retrieval_by_person | PASS | passed |
| test_retrieval_by_task | test_retrieval_by_task | PASS | passed |
| test_same_idempotency_key_does_not_create_duplicate_and_rejects_changed_request | test_same_idempotency_key_does_not_create_duplicate_and_rejects_changed_request | PASS | passed |
| test_simulated_failure_is_recorded_and_retry_needs_fresh_approval | test_simulated_failure_is_recorded_and_retry_needs_fresh_approval | PASS | passed |
| test_source_transcript_is_preserved_even_if_duplicate_submission_occurs | test_source_transcript_is_preserved_even_if_duplicate_submission_occurs | PASS | passed |
| test_ticket_tool_failure_and_success | test_ticket_tool_failure_and_success | PASS | passed |
| test_ticket_tool_supports_reassignment_after_ticket_creation | test_ticket_tool_supports_reassignment_after_ticket_creation | PASS | passed |
| test_transcript_survives_app_restart | test_transcript_survives_app_restart | PASS | passed |
| test_voice_action_proposal_cannot_skip_approval | test_voice_action_proposal_cannot_skip_approval | PASS | passed |
| test_voice_extraction_validates_exact_transcript_before_memory_storage | test_voice_extraction_validates_exact_transcript_before_memory_storage | PASS | passed |
| test_voice_recall_uses_existing_evidence_and_completion_policy | test_voice_recall_uses_existing_evidence_and_completion_policy | PASS | passed |
| test_voice_session_start_and_stop_over_http | test_voice_session_start_and_stop_over_http | PASS | passed |
