# 功能验证脚本

统一入口覆盖当前已实现功能的自动化测试，并执行 Ruff 代码检查和格式检查。
测试使用模拟上游和临时 SQLite 数据库，不需要启动网关、不需要供应商密钥，
不会调用真实模型，也不会写入日常使用的 `data/calls.sqlite3`。

## 一键验证全部功能

在项目根目录运行，环境要求与主项目一致：Python 3.12+、uv。

```powershell
.\scripts\verify.ps1
```

跨平台入口，或 PowerShell 环境无法直接执行 `.ps1` 时使用：

```powershell
uv run --locked python scripts/verify.py
```

PowerShell 入口会定位项目根目录，支持从其他工作目录调用。
`uv run --locked` 按项目锁文件准备环境，首次运行可能需要下载依赖。

默认执行 `tests/` 下全部测试，而不是只执行固定文件列表；新增测试也会被纳入全量验证。
然后执行 `ruff check .` 和 `ruff format --check .`，只检查，不自动修改代码。
某一步失败仍继续执行后续检查，最终统一返回失败。

## 按功能验证

列出分组与对应测试文件：

```powershell
.\scripts\verify.ps1 --list
```

只验证流式输出、Token 口径和调用治理，同时执行代码检查：

```powershell
.\scripts\verify.ps1 --group streaming usage governance
```

只执行指定分组的测试，跳过代码检查：

```powershell
.\scripts\verify.ps1 --group streaming usage governance --tests-only
```

所有参数同样适用于 Python 入口：

```powershell
uv run --locked python scripts/verify.py --group chat structured
uv run --locked python scripts/verify.py --report-dir reports/verification/manual
```

`--report-dir` 的相对路径以项目根目录为基准。重复指定同一路径会覆盖该目录的同名报告。
分组验证的汇总标记为 `scope=selected`，全量验证标记为 `scope=all`。

## 功能点与用例映射

下表列出各功能点的代表性用例；执行分组时会运行对应文件中的全部用例及参数组合。
用例完整名称和本次实际执行情况见 `junit.xml` 与 `tests.log`。

| 分组 | 功能点 | 测试文件与代表性用例 |
| --- | --- | --- |
| `health` | 健康检查、OpenAPI 暴露 | `tests/test_health.py`：`test_health`、`test_openapi_exposes_health` |
| `config` | 环境变量覆盖 dotenv、非法日志配置 | `tests/test_config.py`：`test_environment_variables_override_dotenv`、`test_invalid_log_level_fails_at_startup` |
| `config` | 独立模型密钥、空环境密钥、多 dotenv 文件、禁用 dotenv | `tests/test_config.py`：`test_model_keys_use_individual_variables_and_environment_precedes_dotenv`、`test_empty_environment_key_does_not_fall_back_to_dotenv`、`test_model_keys_honor_multiple_dotenv_files` |
| `models` | 配置字段校验、模型名与密钥变量唯一性、配置缺失或损坏 | `tests/test_models.py`：`test_invalid_model_configuration_is_rejected`、`test_models_cannot_share_logical_names_or_key_variables`、`test_invalid_config_file_prevents_application_creation` |
| `models` | 模型列表、逻辑路由、未知模型、独立客户端和密钥隔离 | `tests/test_models.py`：`test_model_catalog_exposes_only_logical_names_and_capabilities`、`test_logical_models_select_independent_clients_and_hide_provider_names`、`test_one_missing_key_does_not_affect_other_models_or_use_legacy_key` |
| `models` | chat/结构化能力开关、输出 Token 上限、客户端初始化失败清理 | `tests/test_models.py`：`test_chat_capability_is_checked_before_calling_provider`、`test_excessive_output_tokens_are_rejected`、`test_partial_client_initialization_failure_closes_created_clients` |
| `prompts` | 模板名称和精确版本、默认参数、JSON 参数、参数不二次渲染 | `tests/test_prompts.py`：`test_template_lookup_requires_exact_name_and_version`、`test_render_uses_supplied_parameters_and_defaults`、`test_parameter_values_are_not_interpreted_as_templates` |
| `prompts` | 参数错误脱敏、无效模板、损坏 JSON、重复版本 | `tests/test_prompts.py`：`test_parameter_errors_hide_values`、`test_invalid_templates_fail_during_loading`、`test_duplicate_template_versions_are_rejected` |
| `prompts` | 模板目录接口、system 消息替换、不修改原始请求 | `tests/test_prompts.py`：`test_prompt_catalog_lists_versions_and_parameter_requirements`、`test_build_request_replaces_system_messages_without_mutating_input` |
| `chat` | 统一请求响应、默认非流式、可选字段、参数与必填项校验 | `tests/test_chat.py`：`test_chat_forwards_request_and_returns_unified_response`、`test_chat_defaults_to_non_streaming_and_omits_unset_options`、`test_invalid_chat_request_is_rejected_without_calling_provider` |
| `chat` | Prompt 选择与消息构建、错误阻止上游调用 | `tests/test_chat.py`：`test_selected_prompt_replaces_system_messages_before_calling_provider`、`test_selected_prompt_uses_exact_version_and_default_parameters`、`test_prompt_errors_prevent_upstream_calls` |
| `chat` | HTTP/网络错误映射、无效上游响应、连接复用与关闭、依赖替换 | `tests/test_chat.py`：`test_upstream_http_errors_are_mapped_and_sanitized`、`test_invalid_upstream_response_returns_bad_gateway`、`test_http_client_is_reused_and_closed_with_application` |
| `adapters` | DeepSeek 请求地址、认证、参数透传、响应解析、健康检查 | `tests/test_deepseek_adapter.py`：`test_complete_sends_chat_request_and_parses_response`、`test_healthcheck_checks_authenticated_models_access` |
| `adapters` | Anthropic system/messages 转换、结束原因、用量、逻辑路由 | `tests/test_anthropic_adapter.py`：`test_messages_request_and_response_conversion`、`test_defaults_plain_text_and_stop_reasons`、`test_flash_model_routing_template_and_catalog` |
| `adapters` | Messages HTTP/网络异常、响应损坏、纯 system 请求、Token 限制 | `tests/test_anthropic_adapter.py`：`test_messages_http_errors_use_gateway_mapping`、`test_malformed_messages_response_is_rejected`、`test_flash_token_limit_and_system_only_rejection` |
| `errors` | 统一错误结构、HTTP 错误头保留、校验输入和内部异常脱敏 | `tests/test_errors.py`：`test_unknown_route_has_consistent_error`、`test_http_error_preserves_headers`、`test_validation_error_omits_input`、`test_unexpected_error_hides_internal_message` |
| `structured` | JSON 对象、原生 JSON Schema、降级与本地严格校验 | `tests/test_structured_output.py`：`test_json_object_prompt_and_validated_response`、`test_native_json_schema_request`、`test_schema_fallback_succeeds_and_reports_actual_mode` |
| `structured` | 不支持的 Schema、复杂度上限、所有 choices 校验、损坏和截断 JSON | `tests/test_structured_output.py`：`test_invalid_or_unimplemented_schema_is_explicitly_rejected`、`test_schema_complexity_limits`、`test_every_choice_is_validated`、`test_truncated_json_is_repaired_even_if_parseable` |
| `structured` | 修复预算、保留任务与采样参数、拒答不重试、传输失败停止修复 | `tests/test_structured_output.py`：`test_retry_budget_and_bounded_repair_context`、`test_schema_repair_preserves_task_and_sampling_parameters`、`test_refusal_is_not_retried`、`test_transport_failure_during_repair_stops_further_attempts` |
| `structured` | Messages 原生结构化协议、修复请求、错误详情脱敏 | `tests/test_structured_output.py`：`test_anthropic_native_structured_request`、`test_messages_repair_uses_the_same_system_and_native_schema`、`test_content_error_details_do_not_echo_invalid_values` |
| `streaming` | Chat Completions SSE 归一化、事件 ID/序列、逻辑模型名、用量和完成原因 | `tests/test_streaming.py`：`test_stream_normalizes_deltas_usage_and_late_terminal` |
| `streaming` | 多行 SSE、心跳、多个 choices、Messages 推理内容及缓存用量 | `tests/test_streaming.py`：`test_sse_multiline_data_and_keepalive`、`test_multiple_choices_finish_before_completion`、`test_messages_normalize_reasoning_cached_usage_and_finish` |
| `streaming` | 单一终态、上游缺失结束标记、异常帧、流中途网络错误 | `tests/test_streaming.py`：`test_stream_failures_have_one_sanitized_terminal`、`test_midstream_transport_errors`、`test_messages_failures_have_one_terminal` |
| `streaming` | 下游断连、等待上游时取消、写入失败、关闭上游、终态竞争 | `tests/test_streaming.py`：`test_cancellation_while_reading_closes_upstream_and_has_one_terminal`、`test_disconnect_while_waiting_cancels_upstream`、`test_completed_wins_over_late_cancellation`、`test_failure_wins_over_late_cancellation` |
| `streaming` | SSE 前 HTTP 错误、错误 Content-Type、能力禁用、流式结构化拒绝、统一 complete 入口 | `tests/test_streaming.py`：`test_wrong_stream_content_type_is_rejected_before_sse`、`test_streaming_structured_output_is_rejected_without_calling_provider`、`test_messages_complete_uses_same_entry_point` |
| `usage` | 输入输出统一命名、零值、缺失字段、忽略供应商独立总量、非法计数类型 | `tests/test_usage.py`：`test_zero_counters_are_known_and_provider_total_is_ignored`、`test_missing_counters_are_not_invented`、`test_compatible_usage_rejects_invalid_counters` |
| `usage` | 缓存/推理分类不能超过总量、Messages 缓存仅计一次、跨尝试汇总 | `tests/test_usage.py`：`test_usage_classification_cannot_exceed_total`、`test_messages_input_includes_both_cache_categories_once`、`test_aggregation_requires_each_attempt_counter_to_be_known` |
| `governance` | 每请求主记录、关联、路由、Prompt、用量、延迟、弹性、结果、错误和成本 | `tests/test_governance.py`：`test_success_records_all_groups_without_content`、`test_rejected_requests_are_recorded_without_upstream_attempts` |
| `governance` | 每尝试记录、修复 Token/费用汇总、后续失败保留已发生费用 | `tests/test_governance.py`：`test_repair_attempts_are_charged_once_and_usage_is_aggregated`、`test_later_network_failure_preserves_previous_attempt_cost` |
| `governance` | Prompt HMAC、密钥校验、敏感文本不落库、不进入日志 | `tests/test_governance.py`：`test_prompt_hmac_is_keyed_and_tracks_actual_repair_prompt`、`test_hmac_key_validation_does_not_expose_secret`、`test_success_records_all_groups_without_content` |
| `governance` | 首次写入失败阻止上游、结束写入失败保留待核对记录、并发隔离 | `tests/test_governance.py`：`test_record_start_failure_prevents_provider_call`、`test_finish_storage_failure_is_visible_and_leaves_pending_record`、`test_concurrent_calls_keep_independent_ids_and_costs` |
| `governance` | Decimal 定价、缓存分类、版本生效时间、货币分组、未知价格/用量不记为零 | `tests/test_governance.py`：`test_cache_and_reasoning_are_not_double_charged`、`test_cache_pricing_needs_breakdown_and_rates`、`test_price_version_effective_date_and_currency_are_preserved`、`test_unknown_usage_and_unknown_prices_never_report_zero_cost` |
| `governance` | 流式快照替换、计数倒退、缺失最终用量、断连落库、派发前取消零成本 | `tests/test_governance.py`：`test_stream_snapshots_are_not_added_and_final_result_is_recorded`、`test_regressing_usage_fails_without_overwriting_known_snapshot`、`test_disconnect_persists_cancelled_usage_and_releases_provider`、`test_cancellation_before_dispatch_has_no_incurred_cost` |
| `governance` | 保留期清理、终态条件更新 | `tests/test_governance.py`：`test_retention_and_terminal_updates` |
| `governance` | request_id/call_id 查询、模型/时间/条数过滤、空结果、时间格式、数据库缺失与损坏 | `tests/test_governance.py`：`test_local_query_command_reads_call_and_attempts`、`test_local_query_filters_and_limits`、`test_local_query_rejects_invalid_or_naive_timestamps`、`test_local_query_storage_error_is_sanitized` |
| `verification` | 报告分组、参数化用例计数、失败/异常/跳过/空分组阻止通过 | `tests/test_verification.py`：`test_report_groups_parameterized_cases_and_includes_new_modules`、`test_unsuccessful_or_skipped_case_prevents_acceptance`、`test_empty_report_and_missing_feature_prevent_acceptance` |
| `verification` | 参数校验、各步骤退出码传递、旧报告不能导致误判通过 | `tests/test_verification.py`：`test_invalid_group_fails_instead_of_running_an_unintended_suite`、`test_runner_propagates_step_failures_and_saves_summary`、`test_missing_new_report_cannot_reuse_previous_success` |

## 报告与通过标准

默认每次运行生成独立的 UTC 时间目录：

```text
reports/verification/<UTC时间>/
├── summary.json   # 总状态、全量/分组范围、各组计数、步骤退出码和耗时
├── junit.xml      # 每个测试用例的结果，可交给 CI 展示
├── tests.log      # pytest 完整输出与失败详情
├── lint.log       # Ruff 代码检查输出；--tests-only 时不生成
└── format.log     # Ruff 格式检查输出；--tests-only 时不生成
```

通过要求：所有选定分组至少执行一个用例，所有用例通过，没有失败、异常或跳过，
且所有已执行检查的退出码均为 0。报告不存在或无法解析也判定失败。
全量运行中新增加、尚未配置分组的测试仍然执行，计数显示在 `other`；维护时应补入分组和清单。

终端显示 `Verification PASSED` 且 `$LASTEXITCODE` 为 0 表示本次验证通过。
验证失败返回 1，参数错误返回 2。默认报告目录已加入 Git 忽略规则。

查看最近一次报告：

```powershell
$verificationReport = Get-ChildItem .\reports\verification -Directory |
    Where-Object Name -Match '^\d{8}T\d{6}\.\d{6}Z$' |
    Sort-Object Name -Descending | Select-Object -First 1
Get-Content (Join-Path $verificationReport.FullName 'summary.json') -Raw | ConvertFrom-Json
```

该验收验证网关实现对模拟协议和故障的处理。真实供应商连通性、供应商实时行为、
代理缓冲、真实客户端网络断开和负载性能仍需要在部署环境联调。
当前尚未实现的流式结构化输出、工具调用、多模态和网关鉴权不列入已实现功能验收。
