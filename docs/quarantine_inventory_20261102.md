# Inventory of skipped tests — 2026-11-02

Collection found 74 explicitly skipped tests. The full suite reports 80 skips, including runtime skips. No quarantine entries were added or removed in this upgrade.

These reasons are inherited claims, not proof that the associated product behavior is correct. Each group still requires separate reproduction and review.

| Existing reason | Count |
|---|---:|
| QUARANTINE stale (pre-P0): dispatcher/speech repair đã đổi | 14 |
| QUARANTINE stale (pre-P0): kỳ vọng trạng thái job/tracker cũ | 3 |
| QUARANTINE stale (pre-P0): không hermetic: cache đĩa máy chính trả sẵn kết quả dịch | 3 |
| QUARANTINE stale (pre-P0): cơ chế cache/fail-fast đã đổi | 3 |
| QUARANTINE stale (pre-P0): divide-and-conquer/timeout đã đổi | 3 |
| QUARANTINE stale (pre-P0): ducking/mix đã đổi | 3 |
| QUARANTINE stale (pre-P0): OCR proxy đã đổi | 3 |
| QUARANTINE stale (pre-P0): cache voice/ocr đã đổi | 3 |
| QUARANTINE stale (pre-P0): flaky khi chạy song song (pass khi chạy đơn) | 2 |
| QUARANTINE stale (pre-P0): kỳ vọng retry/manifest cũ | 2 |
| QUARANTINE stale (pre-P0): kỳ vọng acceptance cũ | 2 |
| QUARANTINE stale (pre-P0): kỳ vọng preservation cũ | 2 |
| QUARANTINE stale (pre-P0): kỳ vọng whisper cache + telegram shutdown cũ | 2 |
| QUARANTINE stale (pre-P0): ASR đã chuyển sang whisper_sensevoice_hybrid (592276d) | 2 |
| QUARANTINE stale (pre-P0): format log stage đã đổi | 2 |
| QUARANTINE stale (pre-P0): stability kỳ vọng cũ | 2 |
| QUARANTINE stale (pre-P0): guard TTS đã đổi | 2 |
| QUARANTINE stale (pre-P0): kỳ vọng layout đường dẫn app cũ | 1 |
| QUARANTINE stale (pre-P0): kỳ vọng fast profile cũ | 1 |
| QUARANTINE stale (pre-P0): kỳ vọng resource optimization cũ | 1 |
| QUARANTINE stale (pre-P0): kỳ vọng fail-closed cũ | 1 |
| QUARANTINE stale (pre-P0): hành vi audit geometry đã đổi | 1 |
| QUARANTINE stale (pre-P0): hành vi audit timestamp đã đổi | 1 |
| QUARANTINE stale (pre-P0): kỳ vọng rc3 safety cũ | 1 |
| QUARANTINE stale (pre-P0): kỳ vọng review regression cũ | 1 |
| QUARANTINE stale (pre-P0): kỳ vọng stage2 QC cũ | 1 |
| QUARANTINE stale (pre-P0): kỳ vọng stage4 QC cũ | 1 |
| QUARANTINE stale (pre-P0): release gate kỳ vọng template/hành vi cũ | 1 |
| QUARANTINE stale (pre-P0): luồng delivery telegram đã đổi | 1 |
| QUARANTINE stale (pre-P0): voice pipeline đã đổi | 1 |
| QUARANTINE stale (pre-P0): GPU lock đã đổi | 1 |
| QUARANTINE stale (pre-P0): checkpoint đã đổi | 1 |
| QUARANTINE stale (pre-P0): flaky: phụ thuộc môi trường key | 1 |
| QUARANTINE stale (pre-P0): flaky: đếm frame GPU đổi theo tải máy | 1 |
| QUARANTINE stale (pre-P0): translation final kỳ vọng cũ | 1 |
| QUARANTINE stale (pre-P0): flaky: immutable snapshot | 1 |
| QUARANTINE stale (pre-P0): wireup đã đổi | 1 |
| QUARANTINE stale (pre-P0): v2 contracts kỳ vọng cũ | 1 |

## Individual tests

- `tests/test_app_paths.py::AppPathsTests::test_defaults_are_portable_and_outside_the_repository`: QUARANTINE stale (pre-P0): kỳ vọng layout đường dẫn app cũ
- `tests/test_asr_isolated.py::IsolatedTests::test_failure_does_not_publish`: QUARANTINE stale (pre-P0): flaky khi chạy song song (pass khi chạy đơn)
- `tests/test_asr_isolated.py::IsolatedTests::test_result_and_no_reuse`: QUARANTINE stale (pre-P0): flaky khi chạy song song (pass khi chạy đơn)
- `tests/test_atomic_windows_retry.py::AtomicRetryTests::test_other_io_errors_are_not_retried`: QUARANTINE stale (pre-P0): kỳ vọng retry/manifest cũ
- `tests/test_atomic_windows_retry.py::AtomicRetryTests::test_permanent_denial_is_bounded_and_keeps_old_manifest`: QUARANTINE stale (pre-P0): kỳ vọng retry/manifest cũ
- `tests/test_dashboard_and_tracker.py::TestDashboardAndTracker::test_dashboard_html_served`: QUARANTINE stale (pre-P0): kỳ vọng trạng thái job/tracker cũ
- `tests/test_dashboard_and_tracker.py::TestDashboardAndTracker::test_job_tracker_error_and_stop`: QUARANTINE stale (pre-P0): kỳ vọng trạng thái job/tracker cũ
- `tests/test_dashboard_and_tracker.py::TestDashboardAndTracker::test_job_tracker_lifecycle`: QUARANTINE stale (pre-P0): kỳ vọng trạng thái job/tracker cũ
- `tests/test_fast_profile.py::FastProfileTests::test_all_translation_providers_receive_duration_budgets`: QUARANTINE stale (pre-P0): kỳ vọng fast profile cũ
- `tests/test_final_acceptance.py::AcceptanceTests::test_gemini_failures_have_bounded_attempts_and_no_query_key`: QUARANTINE stale (pre-P0): kỳ vọng acceptance cũ
- `tests/test_final_acceptance.py::AcceptanceTests::test_vfr_proxy_retains_original_dimensions_and_gpu_encoder`: QUARANTINE stale (pre-P0): kỳ vọng acceptance cũ
- `tests/test_long_video_resource_optimization.py::TestLongVideoResourceOptimization::test_render_fallback_to_libx264_on_nvenc_failure`: QUARANTINE stale (pre-P0): kỳ vọng resource optimization cũ
- `tests/test_multi_llm_translation.py::TestMultiLLMTranslation::test_gemini_health_cache_and_retry_limits`: QUARANTINE stale (pre-P0): không hermetic: cache đĩa máy chính trả sẵn kết quả dịch
- `tests/test_multi_llm_translation.py::TestMultiLLMTranslation::test_prompt_builder_with_glossary_entity_speaker_and_cross_batch_context`: QUARANTINE stale (pre-P0): không hermetic: cache đĩa máy chính trả sẵn kết quả dịch
- `tests/test_multi_llm_translation.py::TestMultiLLMTranslation::test_translate_subtitles_forwards_glossary_and_maps`: QUARANTINE stale (pre-P0): không hermetic: cache đĩa máy chính trả sẵn kết quả dịch
- `tests/test_ocr_fail_closed.py::OCRFailureTests::test_both_telegram_ocr_handlers_raise`: QUARANTINE stale (pre-P0): kỳ vọng fail-closed cũ
- `tests/test_ocr_geometry_audit.py::SourceMaskAuditTests::test_source_lifetime_outside_speech_is_covered_without_extending_text`: QUARANTINE stale (pre-P0): hành vi audit geometry đã đổi
- `tests/test_ocr_runtime_audit.py::OCRTimestampTests::test_truncated_tail_does_not_stretch_last_frame_across_gap`: QUARANTINE stale (pre-P0): hành vi audit timestamp đã đổi
- `tests/test_pipeline_v2_preservation.py::LegacyPatchPreservationTests::test_local_renderer_uses_portable_environment_paths`: QUARANTINE stale (pre-P0): kỳ vọng preservation cũ
- `tests/test_pipeline_v2_preservation.py::LegacyPatchPreservationTests::test_telegram_network_updates_use_safe_wrapper`: QUARANTINE stale (pre-P0): kỳ vọng preservation cũ
- `tests/test_rc3_safety.py::RC3Checks::test_history_live_lock_cannot_be_stolen`: QUARANTINE stale (pre-P0): kỳ vọng rc3 safety cũ
- `tests/test_review_regressions.py::ApiStyleTests::test_api_request_passes_selected_font_into_pipeline`: QUARANTINE stale (pre-P0): kỳ vọng review regression cũ
- `tests/test_stage2_subtitle_and_qc.py::TestStage2SubtitleAndQC::test_landscape_subtitle_width_not_clamped_to_65_percent`: QUARANTINE stale (pre-P0): kỳ vọng stage2 QC cũ
- `tests/test_stage4_full_chain_qc.py::TestStage4FullChainQC::test_ocr_timeout_handling`: QUARANTINE stale (pre-P0): kỳ vọng stage4 QC cũ
- `tests/test_stage6_acceptance_matrix.py::TestStage6AcceptanceMatrix::test_release_gate_zero_exposed_chinese_text`: QUARANTINE stale (pre-P0): release gate kỳ vọng template/hành vi cũ
- `tests/test_telegram_bot_v2_delivery.py::TestTelegramBotV2Delivery::test_end_to_end_queue_to_delivery_preserves_chat_id`: QUARANTINE stale (pre-P0): luồng delivery telegram đã đổi
- `tests/test_top5_fixes.py::TestWhisperModelCache::test_whisper_model_cached_and_released`: QUARANTINE stale (pre-P0): kỳ vọng whisper cache + telegram shutdown cũ
- `tests/test_top5_fixes.py::TestTelegramGracefulShutdown::test_cmd_stop_graceful_sequence`: QUARANTINE stale (pre-P0): kỳ vọng whisper cache + telegram shutdown cũ
- `tests/test_translation_optimization.py::OptimizationTests::test_all_failed_fail_fast_during_cooldown`: QUARANTINE stale (pre-P0): cơ chế cache/fail-fast đã đổi
- `tests/test_translation_optimization.py::OptimizationTests::test_failed_model_skipped_next_video_and_recovers`: QUARANTINE stale (pre-P0): cơ chế cache/fail-fast đã đổi
- `tests/test_translation_optimization.py::OptimizationTests::test_transport_failure_does_not_poison_next_job`: QUARANTINE stale (pre-P0): cơ chế cache/fail-fast đã đổi
- `tests/test_translation_resilience.py::TranslationResilienceTests::test_divide_and_conquer_on_gemini_length_mismatch`: QUARANTINE stale (pre-P0): divide-and-conquer/timeout đã đổi
- `tests/test_translation_resilience.py::TranslationResilienceTests::test_divide_and_conquer_preserves_duration_budgets`: QUARANTINE stale (pre-P0): divide-and-conquer/timeout đã đổi
- `tests/test_translation_resilience.py::TranslationResilienceTests::test_gemini_configurable_timeout`: QUARANTINE stale (pre-P0): divide-and-conquer/timeout đã đổi
- `tests/test_v1_audio_refactor.py::AudioRefactorTests::test_ducking_preserves_late_audio_marker`: QUARANTINE stale (pre-P0): ducking/mix đã đổi
- `tests/test_v1_audio_refactor.py::AudioRefactorTests::test_float_mix_does_not_flatten_overlapping_voice_peaks`: QUARANTINE stale (pre-P0): ducking/mix đã đổi
- `tests/test_v1_audio_refactor.py::AudioRefactorTests::test_missing_voice_is_an_error`: QUARANTINE stale (pre-P0): ducking/mix đã đổi
- `tests/test_v1_auto_voice.py::TestV1AutoVoice::test_06_dubbing_audio_voice_purity_no_silent_voice_mix`: QUARANTINE stale (pre-P0): voice pipeline đã đổi
- `tests/test_v1_bs_roformer_audio.py::TestBSRoFormerAudio::test_separator_gpu_lock_concurrency`: QUARANTINE stale (pre-P0): GPU lock đã đổi
- `tests/test_v1_checkpoint.py::CheckpointTests::test_asr_reuse_and_audio_change`: QUARANTINE stale (pre-P0): checkpoint đã đổi
- `tests/test_v1_gemini_stability_codex.py::TestGeminiStabilityCodex::test_11_fpt_key_isolation`: QUARANTINE stale (pre-P0): flaky: phụ thuộc môi trường key
- `tests/test_v1_model_registry.py::ModelRegistryTests::test_resolve_effective_model_by_mode`: QUARANTINE stale (pre-P0): ASR đã chuyển sang whisper_sensevoice_hybrid (592276d)
- `tests/test_v1_model_registry.py::ModelRegistryTests::test_smart_skip_ocr_filter_logic`: QUARANTINE stale (pre-P0): ASR đã chuyển sang whisper_sensevoice_hybrid (592276d)
- `tests/test_v1_ocr_proxy.py::ProxyTests::test_cancel_does_not_start_original_after_proxy`: QUARANTINE stale (pre-P0): OCR proxy đã đổi
- `tests/test_v1_ocr_proxy.py::ProxyTests::test_direct_vfr_input_passes_probed_timeline_duration`: QUARANTINE stale (pre-P0): OCR proxy đã đổi
- `tests/test_v1_ocr_proxy.py::ProxyTests::test_large_source_preserves_original_dimensions`: QUARANTINE stale (pre-P0): OCR proxy đã đổi
- `tests/test_v1_phase1_to_4_upgrades.py::TestToolV1Upgrades::test_03_early_subtitle_band_lock_reduces_frames_in_full_mode`: QUARANTINE stale (pre-P0): flaky: đếm frame GPU đổi theo tải máy
- `tests/test_v1_resource_control.py::ResourceTests::test_stage_context_and_failure`: QUARANTINE stale (pre-P0): format log stage đã đổi
- `tests/test_v1_resource_control.py::ResourceTests::test_worker_environment_correlation`: QUARANTINE stale (pre-P0): format log stage đã đổi
- `tests/test_v1_speech_translation_repair.py::DispatcherTests::test_auth_stops_chain`: QUARANTINE stale (pre-P0): dispatcher/speech repair đã đổi
- `tests/test_v1_speech_translation_repair.py::DispatcherTests::test_bad_payload_stops_chain`: QUARANTINE stale (pre-P0): dispatcher/speech repair đã đổi
- `tests/test_v1_speech_translation_repair.py::DispatcherTests::test_cancel_after_response`: QUARANTINE stale (pre-P0): dispatcher/speech repair đã đổi
- `tests/test_v1_speech_translation_repair.py::DispatcherTests::test_cancel_before_network`: QUARANTINE stale (pre-P0): dispatcher/speech repair đã đổi
- `tests/test_v1_speech_translation_repair.py::DispatcherTests::test_empty_200_tries_next`: QUARANTINE stale (pre-P0): dispatcher/speech repair đã đổi
- `tests/test_v1_speech_translation_repair.py::DispatcherTests::test_expired_deadline`: QUARANTINE stale (pre-P0): dispatcher/speech repair đã đổi
- `tests/test_v1_speech_translation_repair.py::DispatcherTests::test_failed_response_retains_cause`: QUARANTINE stale (pre-P0): dispatcher/speech repair đã đổi
- `tests/test_v1_speech_translation_repair.py::DispatcherTests::test_partial_200_tries_next_model`: QUARANTINE stale (pre-P0): dispatcher/speech repair đã đổi
- `tests/test_v1_speech_translation_repair.py::SpeechTests::test_guard_last_cue_and_speed`: QUARANTINE stale (pre-P0): dispatcher/speech repair đã đổi
- `tests/test_v1_speech_translation_repair.py::SpeechTests::test_guard_measures_real_file_not_metadata`: QUARANTINE stale (pre-P0): dispatcher/speech repair đã đổi
- `tests/test_v1_speech_translation_repair.py::BatchTests::test_invalid_condensation_never_cached`: QUARANTINE stale (pre-P0): dispatcher/speech repair đã đổi
- `tests/test_v1_speech_translation_repair.py::BatchTests::test_unresolved_overflow_is_rejected_without_cache`: QUARANTINE stale (pre-P0): dispatcher/speech repair đã đổi
- `tests/test_v1_speech_translation_repair.py::MixerIntegrationTests::test_direct_refuses_silent_speech_clipping`: QUARANTINE stale (pre-P0): dispatcher/speech repair đã đổi
- `tests/test_v1_speech_translation_repair.py::MixerIntegrationTests::test_hierarchy_refuses_global_overlap_before_slices`: QUARANTINE stale (pre-P0): dispatcher/speech repair đã đổi
- `tests/test_v1_stability.py::StabilityTests::test_gemini_skips_recent_failed_model_and_keeps_full_array`: QUARANTINE stale (pre-P0): stability kỳ vọng cũ
- `tests/test_v1_stability.py::VoiceStabilityTests::test_successful_cue_reused_but_changed_text_invalidates`: QUARANTINE stale (pre-P0): stability kỳ vọng cũ
- `tests/test_v1_translation_final.py::TranslationFinalTests::test_old_video_cannot_record_model`: QUARANTINE stale (pre-P0): translation final kỳ vọng cũ
- `tests/test_v1_tts_translation_guard.py::TranslationGuardTests::test_failed_translation_never_reaches_tts`: QUARANTINE stale (pre-P0): guard TTS đã đổi
- `tests/test_v1_tts_translation_guard.py::TranslationGuardTests::test_repairs_only_chinese_before_tts`: QUARANTINE stale (pre-P0): guard TTS đã đổi
- `tests/test_v1_video_router.py::VideoRouterTests::test_immutable_snapshot_in_frozen_config`: QUARANTINE stale (pre-P0): flaky: immutable snapshot
- `tests/test_v1_voice_ocr_optimization.py::OptimizationTests::test_cache_failure_does_not_fail_audio`: QUARANTINE stale (pre-P0): cache voice/ocr đã đổi
- `tests/test_v1_voice_ocr_optimization.py::OptimizationTests::test_invalid_duration_is_never_cached`: QUARANTINE stale (pre-P0): cache voice/ocr đã đổi
- `tests/test_v1_voice_ocr_optimization.py::OptimizationTests::test_shared_cache_and_corruption`: QUARANTINE stale (pre-P0): cache voice/ocr đã đổi
- `tests/test_v1_wireup_integration.py::V1WireupIntegrationTests::test_orchestrator_planning_and_gpu_guard`: QUARANTINE stale (pre-P0): wireup đã đổi
- `tests/test_v2_refactor_contracts.py::BackgroundContracts::test_runtime_times_and_enhancement_failure_fallback`: QUARANTINE stale (pre-P0): v2 contracts kỳ vọng cũ
