# 用量、调用记录与成本

`POST /v1/chat/completions` 使用同一套治理链路处理普通响应、结构化修复和流式响应。
记录默认保存在 `data/calls.sqlite3`；SQLite、WAL 和共享内存文件均已加入 Git 忽略规则。
记录是内部管理数据，不通过未鉴权的 HTTP 接口公开。

## 用量口径

- `input_tokens`：供应商报告的输入总量，包含缓存读取和缓存创建等输入分类。
- `output_tokens`：供应商报告的输出总量，包含已计入其中的推理 Token，不重复相加。
- `total_tokens`：由前两项相加；任一未知时为 null，不采用供应商独立总量作为计费依据。
- Chat Completions 原始 `prompt_tokens`、`completion_tokens` 在适配器内转换，不作为对外别名。
- Messages 输入量是普通输入、缓存创建、缓存读取之和。
- 流式 `usage` 是累计快照，后一个替换前一个；计数倒退作为无效上游数据处理。
- 内容正常结束不保证用量完整。只有早期输出快照而缺少最终用量时，最终 `output_tokens` 为 null。
- 结构化输出顶层 `usage` 包含所有尝试，`structured_output` 仅保留 `mode`、`attempts`。

记录的 `usage.status` 为 `complete`、`partial` 或 `missing`。
`known_input_tokens`、`known_output_tokens` 保存已知下限，不能作为最终总量。
失败和取消时仍保存最近的有效快照。计数缺失时不以 0 替代，也不根据字符数估算。

## 记录结构与关联

`calls` 保存请求主记录，`attempts` 保存上游尝试；每条记录具有固定白名单结构：

| 类别 | 字段与说明 |
| --- | --- |
| 关联 | `id`、`call_id`、`request_id`、`attempt_number`、`stream_id`、安全格式的 `upstream_request_id` |
| 路由 | `route.logical_model`、`provider`、`provider_model`；不保存服务地址或密钥变量 |
| Prompt | `source`、已解析的模板名与版本、消息数、角色数量、字符数、可选 HMAC 指纹与密钥版本 |
| 用量 | 统一计数、完整性、已知下限；尝试明细含缓存读取、创建和推理输出分类 |
| 延迟 | UTC 开始和结束时间、单调时钟耗时、首次非空文本或推理内容延迟 |
| 弹性 | 实际尝试数、修复重试数、重试原因、超时数、回退标识；本期无自动回退 |
| 结果 | `in_progress` 或唯一终态 `completed` / `failed` / `cancelled`、HTTP 状态、结束原因、内容校验结果、是否进入上游调用阶段 |
| 错误 | 统一错误码、处理阶段、安全的异常类型；不保存原始异常消息 |
| 成本 | 状态、币种、Decimal 金额、已知金额下限、分币种金额、本次价格快照 |

网关自己生成 `request_id`，通过响应头 `X-Request-ID` 返回，不把客户端任意提交的 ID 写入记录。
输入校验失败、未知模型、Prompt 错误、能力限制或密钥缺失也生成主记录，但不会发起上游尝试。
上游返回完整答案但结构化校验失败时，该尝试仍消耗 Token：尝试结果为 `completed`，
`validation_status` 为 `failed`；最终请求的结果取决于修复是否成功。

延迟是网关观测值，不等同于供应商内部推理时间；流式耗时会受到客户端消费速度影响。
非流式响应无法测量真实首 Token 延迟，因此 `first_content_ms` 为 null。
请求耗时包含网关处理与流式连接清理，不包含最后一次主记录落盘自身的耗时。

## 写入与故障策略

1. 先写入 `in_progress` 主记录，再处理请求。
2. 每次上游尝试先写入明细，再发起调用。
3. 尝试结束时保存用量、结果、延迟和成本；流式取消也执行受取消保护的清理与记录。
4. 请求结束时按尝试明细汇总一次，不因重复清理或重复结束标记累计两次。

发起尝试前同步保存主记录的路由与 Prompt 元数据，便于查询正在执行的调用。
如果在创建尝试明细后、进入上游调用前取消，该明细保留取消结果，`upstream_dispatched=false`，
不计入实际尝试数，成本为 0。`in_progress` 记录的字段尚未结算，不能据此认定没有产生费用。

SQLite 使用 WAL 与唯一主键；条件更新允许同一终态补充校验元数据，不允许切换到另一个终态。
数据库操作放在工作线程中，不在异步事件循环中直接执行磁盘操作。
开始写入失败时返回 HTTP 503 `CALL_RECORD_UNAVAILABLE`，避免发起无法追踪的上游调用。
结束写入失败时保留原业务响应，并输出 `CALL_RECORD_WRITE_FAILED` 安全日志；未成功更新的记录仍处于待核对状态。
异常退出遗留的 `in_progress` 不自动伪造成失败、成功或零成本，`reconciliation_required` 表示需要核对。
只有启动记录、结束记录均成功时，才能将记录视为已持久化；进程崩溃或磁盘故障不保证最终用量可恢复。

`CALL_RECORDS_RETENTION_DAYS` 默认 30。启动时与每小时删除过期的已结束主记录并级联删除尝试；
未结束记录保留供核对，不自动删除。SQLite 文件没有应用层加密，部署时应限制文件访问权限，并为备份设置相应保留策略。

## 敏感数据

默认不保存请求和响应正文、模板参数值、模型答案、推理正文、Cookie、认证头、API Key、完整 URL 或原始错误。
非法模板名或模型名不会作为路由或 Prompt 元数据写入数据库。
HTTP 客户端及 Uvicorn 访问日志默认降到 WARNING，应用异常日志只包含安全分类和网关关联标识。

`AUDIT_HMAC_KEY` 为空时只保存元数据，不生成普通内容哈希。
配置至少 32 字节的独立密钥后，针对实际消息序列计算 HMAC-SHA256；
结构化格式提示和修复消息也参与对应尝试的指纹，不保存参与计算的内容。
密钥从环境变量或 `.env` 读取，不写入记录；仅保存 `AUDIT_HMAC_KEY_ID`，轮换时应同时更新版本。

## 价格配置

`PRICES_CONFIG_PATH` 默认指向 `config/prices.json`，示例为空，因此尚未配置的成本状态为 `unpriced`，金额为 null。
价格配置在启动时读取，修改后重启应用。以下数字仅演示格式，不代表供应商实际价格：

```json
{
  "prices": [
    {
      "provider": "deepseek",
      "model": "deepseek-chat",
      "version": "example-v1",
      "effective_at": "2026-01-01T00:00:00Z",
      "currency": "USD",
      "input_pricing": "flat",
      "input_per_million": "1.00",
      "output_per_million": "2.00"
    }
  ]
}
```

价格按内部供应商类型和真实请求模型名匹配，选择调用时已经生效的最新版本。
同一供应商和模型的版本号、生效时间不能重复。时间必须含时区，金额必须是有限非负值。
`flat` 表示调用方明确配置所有输入统一单价；配置中不允许同时出现缓存分类单价。

分类计费时设置 `input_pricing=cache_split`，并添加 `cache_read_per_million`、`cache_creation_per_million`：

```text
普通输入 = input_tokens - 缓存读取输入 - 缓存创建输入
金额 = (普通输入 × 普通输入单价
      + 缓存读取输入 × 缓存读取单价
      + 缓存创建输入 × 缓存创建单价
      + output_tokens × 输出单价) / 1,000,000
```

缺少分类计数，或非零分类缺少单价时，不推断完整金额。
推理 Token 已包含在 `output_tokens` 中，不再额外计费。
所有计算使用 Decimal；数据库 JSON 中金额与单价保存为十进制字符串，不通过浮点数计算。
不同币种不能直接相加；汇总保留 `by_currency`，主金额为 null，不引入隐式汇率。

| 成本状态 | 含义 |
| --- | --- |
| `not_incurred` | 未发起上游调用，金额为 0 |
| `estimated` | 用量完整且所需价格已知，金额是按配置计算的估算值 |
| `partial` | 用量、价格分类或币种汇总不完整，完整金额为 null，另存已知下限 |
| `unpriced` | 没有匹配价格，金额为 null |

`known_amount` 是已知的金额下限，不是最终金额；供应商最终账单可能包含其他计费政策。
每次尝试保留所用价格快照，后续修改配置不会改变已经写入的历史记录。
有任一失败或缺失用量的尝试时，请求总成本不能仅使用最后一次成功响应的费用。

## 本地查询

```powershell
uv run python -m app.governance --request-id YOUR_REQUEST_ID
uv run python -m app.governance --model general-chat --limit 20
uv run python -m app.governance --since "2026-11-05T00:00:00+08:00" --limit 100
uv run python -m app.governance --call-id YOUR_CALL_ID
```

将 `YOUR_REQUEST_ID` 替换为响应头 `X-Request-ID`，`YOUR_CALL_ID` 替换为主记录的 `call_id`。
`--since` 要求带时区并按 UTC 比较；`--call-id` 查询该调用的尝试明细，其余选项用于查询主记录。
查询最多返回 1000 条，不创建不存在的数据库；不要向没有管理权限的用户转发内部路由及成本记录。
