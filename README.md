# LLM Gateway

基于 Python、FastAPI 和 uv 的 LLM Gateway 项目骨架，按功能逐步完善。

当前包含健康检查、环境变量配置、控制台日志、统一错误响应、系统提示词管理、自动 API 文档和聊天接口。
健康检查只确认 HTTP 服务正在运行，不检查外部模型服务。
通过统一的 `LLMRequest` / `LLMResponse` 协议和 `POST /v1/chat/completions` 接口，
按逻辑模型名匹配内部配置、独立密钥和模型客户端。
目前支持文本聊天、SSE 流式响应及经过 Pydantic 校验的非流式结构化输出，上游可使用 Chat Completions 或 Anthropic Messages 协议。
内置逻辑模型 `flash-chat` 使用 Messages 协议调用 DeepSeek V4 Flash。

## 环境要求

- Python 3.12 或更高版本（项目默认使用 3.12）
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

以下命令均在项目根目录执行。

## 安装与启动

```powershell
uv sync --locked
Copy-Item .env.example .env
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

`.env` 已存在时无需再次复制。`--reload` 用于本地开发，部署时移除此参数。
环境变量有默认值，不创建 `.env` 也能启动服务。

启动后访问：

- 健康检查：<http://127.0.0.1:8000/health>
- Swagger 文档：<http://127.0.0.1:8000/docs>
- OpenAPI 描述：<http://127.0.0.1:8000/openapi.json>

在另一个 PowerShell 窗口验证：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

`GET /health` 返回 HTTP 200：

```json
{"status": "ok"}
```

## 配置

配置位于 `app/core/config.py`，优先级为：显式传入配置 > 系统环境变量 > 根目录 `.env` > 默认值。
配置在启动时读取并校验，修改后需要重启服务。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_NAME` | `LLM Gateway` | 应用名称，显示在 API 文档中 |
| `ENVIRONMENT` | `development` | 环境标识，可选 `development`、`test`、`production` |
| `LOG_LEVEL` | `INFO` | 应用日志级别，可选 `DEBUG`、`INFO`、`WARNING`、`ERROR`、`CRITICAL` |
| `MODELS_CONFIG_PATH` | 项目下的 `config/models.json` | 模型配置文件路径，默认路径不依赖工作目录 |
| `CALL_RECORDS_PATH` | 项目下的 `data/calls.sqlite3` | SQLite 调用主记录与上游尝试明细；自定义相对路径按工作目录解析 |
| `CALL_RECORDS_RETENTION_DAYS` | 30 | 已结束调用的保留天数，启动时及每小时清理，未结束记录保留待核对 |
| `PRICES_CONFIG_PATH` | 项目下的 `config/prices.json` | 模型价格版本配置；示例为空，不假设真实价格 |
| `AUDIT_HMAC_KEY` | 空 | 可选的独立 HMAC 密钥，至少 32 字节；空值时不保存内容指纹 |
| `AUDIT_HMAC_KEY_ID` | v1 | 指纹密钥版本，仅保存此标识，不保存密钥 |
| `GENERAL_CHAT_API_KEY` | 空 | 示例逻辑模型 `general-chat` 的独立密钥 |
| `CODING_ASSISTANT_API_KEY` | 空 | 示例逻辑模型 `coding-assistant` 的独立密钥 |
| `FLASH_CHAT_API_KEY` | 空 | 示例逻辑模型 `flash-chat` 的独立密钥，使用 DeepSeek Messages 接口 |

`ENVIRONMENT` 目前仅作为环境标识，不会自动切换部署配置。监听地址和端口通过 Uvicorn 参数设置；
Uvicorn 自身日志级别可用 `--log-level` 设置。
真实密钥应保存在本地 `.env` 或部署环境变量中，不要写入 `.env.example`。

## 统一聊天接口协议

接口为 `POST /v1/chat/completions`，请求使用 `application/json`。
`stream=false` 返回 JSON，`stream=true` 返回 `text/event-stream`。
协议定义位于 `app/schemas/chat.py`，也可从 `app.schemas` 导入 `LLMRequest` 和 `LLMResponse`。
Swagger 文档中的请求类型为 `LLMRequest`，响应同时声明 JSON 和 SSE 两种媒体类型。

### LLM Request

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `model` | string | 是 | 逻辑模型名称，例如 `general-chat`，可通过 `GET /v1/models` 查询 |
| `messages` | array | 是 | 至少一条消息，按对话顺序排列 |
| `messages[].role` | string | 是 | `system`、`user` 或 `assistant` |
| `messages[].content` | string | 是 | 消息文本 |
| `system_prompt` | object / null | 否 | 选择系统提示词模板，仅由网关处理，不转发给上游 |
| `system_prompt.name` | string | 选择模板时必填 | 模板名称，例如 `assistant` |
| `system_prompt.version` | string | 选择模板时必填 | 精确版本，例如 `1.0.0` |
| `system_prompt.parameters` | object | 否 | 模板参数，默认 `{}`，支持 JSON 值 |
| `temperature` | number / null | 否 | 范围 0～2；省略或为 null 时不传给上游 |
| `top_p` | number / null | 否 | 范围 0～1；省略或为 null 时不传给上游 |
| `max_tokens` | integer / null | 否 | 正整数；省略或为 null 时使用模型配置的输出上限；未配置上限时 Messages 默认 1024，Chat Completions 不传 |
| `stream` | boolean | 否 | 默认 `false`；`true` 返回统一 SSE 事件，要求模型启用 `streaming`，不能与 `response_format` 同用 |
| `response_format` | object / null | 否 | `json_object` 或 `json_schema`；只有通过网关校验的 JSON 对象才成功返回 |
| `max_retries` | integer | 否 | 校验失败后的修复重试次数，默认 1，范围 0～3；不传给上游，普通文本请求不使用 |

请求顶层不接受未定义字段，避免静默忽略不支持的功能。
网关按逻辑模型名查找内部配置，使用对应密钥和客户端调用真实模型；不接受未配置的真实模型名。

请求示例：

```json
{
  "model": "general-chat",
  "system_prompt": {
    "name": "assistant",
    "version": "1.0.0",
    "parameters": {"role": "友好的助手", "language": "中文"}
  },
  "messages": [
    {"role": "user", "content": "你好，请介绍一下自己。"}
  ],
  "temperature": 0.7,
  "max_tokens": 128,
  "stream": false
}
```

### SSE 流式响应

仍使用同一个聊天接口，设置 `stream=true` 即可。在 PowerShell 中调用：

```powershell
$body = '{"model":"general-chat","messages":[{"role":"user","content":"Hello"}],"stream":true}'
$body | curl.exe -N http://127.0.0.1:8000/v1/chat/completions -H "Content-Type: application/json" --data-binary "@-"
```

网关将供应商数据转换成统一事件，不透传原始 Chunk、Messages 事件或 `[DONE]`。
响应设置 `Cache-Control: no-cache` 和 `X-Accel-Buffering: no`，逐事件写入连接。

| SSE `event` | 含义 | JSON 数据中的附加字段 |
| --- | --- | --- |
| `started` | 流已建立，只发送一次 | 无 |
| `delta` | 文本、推理内容或角色增量 | `delta.index`、`delta.content`、`delta.reasoning_content`、`delta.role` |
| `usage` | 用量快照，供应商提供时发送，可多次更新 | `usage.input_tokens`、`usage.output_tokens`、`usage.total_tokens` |
| `completed` | 供应商明确结束响应 | `finish_reasons` 数组及最终 `usage`（如有） |
| `failed` | 流执行失败 | `error.code`、`error.message` 及已知用量（如有） |
| `cancelled` | 请求被取消 | `reason` 及已知用量（如有） |

每条事件的 JSON 都包含 `type`、`stream_id`、`sequence` 和逻辑模型名 `model`。
`sequence` 从 1 递增，SSE `id` 为 `<stream_id>:<sequence>`；不提供断点续传或历史重放。
未提供的字段直接省略。增量内容按序拼接；用量是累计快照，应以后一个替换前一个，不应累加。
`delta.index` 表示候选答案序号，Messages 协议统一为 0，不暴露上游内容块序号。

```text
id: stream-demo:1
event: started
data: {"type":"started","stream_id":"stream-demo","sequence":1,"model":"general-chat"}

id: stream-demo:2
event: delta
data: {"type":"delta","stream_id":"stream-demo","sequence":2,"model":"general-chat","delta":{"index":0,"content":"Hello!"}}

id: stream-demo:3
event: completed
data: {"type":"completed","stream_id":"stream-demo","sequence":3,"model":"general-chat","finish_reasons":[{"index":0,"finish_reason":"stop"}]}

```

网关为每条已建立的流只确定一个终态：`completed`、`failed` 或 `cancelled`。
终态后不再输出事件；供应商重复的结束标记不会产生第二个终态。
Chat Completions 必须收到 `[DONE]` 且候选答案均已结束；Messages 必须收到 `message_stop`。
只有内容结束而缺少整个响应的结束标记时，不会误报完成，仍可接收后续用量。
`completed` 表示协议正常结束；是否因输出长度限制而截断，需要检查 `finish_reasons` 中的 `length`。

参数、能力校验以及上游连接或 HTTP 状态错误在 SSE 建立前返回原有 HTTP JSON 错误。
SSE 建立后，超时、网络错误、无效数据及供应商错误事件转换为唯一的 `failed`，HTTP 状态仍为 200。
客户端不能仅凭 HTTP 200 判断生成成功，也不能将未收到终态的连接关闭视为完成。
流式调用不自动重试，避免重复输出；`max_retries` 不作用于流式请求。
`stream=true` 与 `response_format` 同用返回 HTTP 422，错误码为 `STREAMING_STRUCTURED_OUTPUT_NOT_SUPPORTED`。

客户端断开或服务端取消请求时，停止上游读取并释放连接，内部确定并记录唯一终态。
连接已断开或请求任务已取消时无法保证发送 `cancelled`；内部终态通过 `app.streaming.service` 的 INFO 日志记录。
终态日志使用网关流 ID 和逻辑模型名，不包含原始供应商错误内容。
完整调用记录会写入 SQLite，可通过 `X-Request-ID` 查询，见 [用量与调用治理](app/governance/README.md)。

### LLM Response

成功时返回 HTTP 200，响应字段如下：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 上游生成的响应标识 |
| `object` | string | 固定为 `chat.completion` |
| `created` | integer | 上游生成响应时的 Unix 时间戳，单位为秒 |
| `model` | string | 调用方使用的逻辑模型名称，网关不会在此字段返回真实模型名 |
| `choices` | array | 至少一个候选结果 |
| `choices[].index` | integer | 非负候选索引 |
| `choices[].message.role` | string | 固定为 `assistant` |
| `choices[].message.content` | string / null | 生成的文本，可省略 |
| `choices[].message.reasoning_content` | string / null | 可选的推理内容 |
| `choices[].message.parsed` | object / null | 结构化请求通过 Pydantic 校验后的 JSON 对象 |
| `structured_output` | object / null | 实际输出模式及总调用次数；累计用量统一放在顶层 `usage` |
| `choices[].finish_reason` | string / null | 可选的结束原因，例如 `stop`、`length` |
| `usage` | object / null | 本次请求全部上游尝试的累计用量，包含结构化输出修复调用 |
| `usage.input_tokens` | integer / null | 非负输入 token 数，缺少数据时为 null |
| `usage.output_tokens` | integer / null | 非负输出 token 数，缺少数据时为 null |
| `usage.total_tokens` | integer / null | 前两项之和；任一未知时为 null |

响应会保留上游在响应顶层和助手消息中返回的扩展字段。
用量只输出统一字段，供应商扩展用量不会直接透传；缓存输入和推理输出的分类明细仅保存在内部记录。
没有供应商用量时 `usage` 可省略或为 null；未知计数不填 0，不根据字符数估算 Token。
流式快照不能相加；缺少最终用量时保留已知下限，并将最终未知计数置为 null。
旧 `prompt_tokens`、`completion_tokens` 字段已替换为 `input_tokens`、`output_tokens`，
旧 `structured_output.total_usage` 已合并到顶层 `usage`，客户端需要同步更新。

响应示例：

```json
{
  "id": "chatcmpl-example",
  "object": "chat.completion",
  "created": 1700000000,
  "model": "general-chat",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "你好！我是一个人工智能助手。"},
      "finish_reason": "stop"
    }
  ],
  "usage": {"input_tokens": 20, "output_tokens": 12, "total_tokens": 32}
}
```

在 `.env` 中配置 `GENERAL_CHAT_API_KEY` 并启动服务后，可在 PowerShell 中调用：

```powershell
$payload = @{
    model = "general-chat"
    system_prompt = @{
        name = "assistant"
        version = "1.0.0"
        parameters = @{ role = "友好的助手"; language = "中文" }
    }
    messages = @(@{ role = "user"; content = "你好，请介绍一下自己。" })
    max_tokens = 128
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post `
    -Uri http://127.0.0.1:8000/v1/chat/completions `
    -ContentType "application/json; charset=utf-8" `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($payload))
```

### 聊天接口错误

失败时使用统一的 `error` 对象；错误详情不会透传上游原始响应或密钥。

| HTTP 状态码 | `error.code` | 场景 |
| --- | --- | --- |
| 422 | `VALIDATION_ERROR` | 请求参数不合法 |
| 404 | `PROMPT_TEMPLATE_NOT_FOUND` | 系统提示词模板名或版本不存在 |
| 422 | `PROMPT_PARAMETER_ERROR` | 模板参数缺失或提供了未定义参数 |
| 404 | `MODEL_NOT_FOUND` | 逻辑模型不存在，不会回退到其他模型 |
| 503 | `MODEL_NOT_CONFIGURED` | 当前模型指定的密钥变量缺失或为空白 |
| 503 | `CALL_RECORD_UNAVAILABLE` | 调用开始或上游尝试开始前无法写入记录，拒绝发起上游请求 |
| 422 | `MODEL_CAPABILITY_NOT_SUPPORTED` | 配置中的模型不支持聊天、流式或指定的结构化输出模式 |
| 422 | `STREAMING_STRUCTURED_OUTPUT_NOT_SUPPORTED` | 流式请求同时指定了 `response_format` |
| 422 | `MODEL_TOKEN_LIMIT_EXCEEDED` | 请求的输出 token 数超过该模型配置的上限 |
| 422 | `INVALID_MESSAGES` | Messages 协议请求只有 system 消息，缺少用户或助手消息 |
| 429 | `UPSTREAM_RATE_LIMITED` | 上游返回限流状态 |
| 504 | `UPSTREAM_TIMEOUT` | 上游连接、读取等操作超时 |
| 502 | `UPSTREAM_HTTP_ERROR` | 上游返回其他非成功状态，包括上游认证失败 |
| 502 | `UPSTREAM_CONNECTION_ERROR` | 上游网络或传输协议错误 |
| 502 | `UPSTREAM_INVALID_RESPONSE` | 上游返回无效 JSON 或不符合响应协议的数据 |
| 422 | `STRUCTURED_OUTPUT_SCHEMA_INVALID` | Schema 无效、超出支持范围或复杂度限制 |
| 502 | `STRUCTURED_OUTPUT_VALIDATION_FAILED` | 结构化内容校验失败，并已用尽修复次数 |
| 502 | `STRUCTURED_OUTPUT_REFUSED` | 模型明确拒答，不进行修复重试 |

例如，上游超时返回：

```json
{
  "error": {
    "code": "UPSTREAM_TIMEOUT",
    "message": "Model provider request timed out"
  }
}
```

## 用量与调用治理

所有聊天请求均生成网关关联 ID，并在响应头 `X-Request-ID` 中返回。
一次请求对应一条 `calls` 主记录；每次上游调用对应一条 `attempts` 明细，结构化修复不会漏记。
记录覆盖关联、路由、Prompt 元数据、用量、延迟、弹性、结果、错误和成本，默认写入 `data/calls.sqlite3`。
流式请求直到终态和连接清理后才完成主记录，HTTP 200 不等于最终生成成功。

默认不保存消息、Prompt 正文、模型答案、推理正文、模板参数值、认证信息或原始错误。
应用默认将 HTTP 客户端和 Uvicorn 访问日志降到 WARNING，避免详细请求目标进入控制台日志。
独立 HMAC 指纹、价格配置、成本完整性和本地查询命令见 [用量与调用治理说明](app/governance/README.md)。

```powershell
uv run python -m app.governance --request-id YOUR_REQUEST_ID
uv run python -m app.governance --model general-chat --limit 20
```

成本使用 Decimal 和本次调用采用的价格快照计算。默认价格配置为空，成本标记为 `unpriced`，金额为 null。
没有发起上游调用时成本为 0；未知用量或未知价格不能当作零成本。

## 模型配置管理

`config/models.json` 集中维护逻辑模型名、真实模型名、适配器类型、上游地址、超时、独立密钥环境变量名和模型能力。
调用方通过 `GET /v1/models` 查询逻辑模型和能力，不需要知道实际模型、服务地址或密钥变量名。

```powershell
Invoke-RestMethod http://127.0.0.1:8000/v1/models | ConvertTo-Json -Depth 10
```

每个模型必须指定不同的 `api_key_env`。密钥值存放在系统环境变量或本地 `.env`，不写入 JSON。
系统环境变量优先；变量存在但为空时，不会回退到 `.env` 或其他模型的密钥。
缺少一个模型的密钥只影响该模型的调用，不影响服务启动、查询接口、健康检查和其他模型。

模型能力包含聊天、流式、工具调用、视觉、推理、结构化输出及可选的上下文窗口和输出限制。
结构化输出通过 `structured_output.supported` 和 `structured_output.modes` 声明，模式支持 `json_object`、`json_schema`。
结构化请求通过 `response_format` 指定 JSON 对象或目标 Schema，网关根据能力选择模式并严格校验。
默认 `general-chat`、`coding-assistant` 已声明 `json_object`；请求 Schema 时可回退到该模式，仍按原始 Schema 校验。
原生 `json_schema` 优先使用；模型和协议均不支持时返回 422。
请求示例、Schema 支持范围及修复重试说明见 [结构化输出说明](app/structured_output/README.md)。
流式请求会检查模型的 `streaming` 能力；工具调用或多模态接口尚未实现。

配置加载时校验名称重复、密钥变量重复、适配器类型、地址和结构化输出模式等内容。
修改配置或密钥后需要重启服务。详细字段、客户端创建和迁移步骤见 [模型配置管理说明](config/README.md)。

### DeepSeek V4 Flash：Messages 协议

在本地 `.env` 或部署环境中设置 `FLASH_CHAT_API_KEY`，然后重启服务。
调用方仍请求 `POST /v1/chat/completions`，将逻辑模型设为 `flash-chat`：

```json
{
  "model": "flash-chat",
  "system_prompt": {
    "name": "assistant",
    "version": "1.0.0",
    "parameters": {"language": "中文"}
  },
  "messages": [{"role": "user", "content": "你好，请介绍一下自己。"}],
  "max_tokens": 1024
}
```

网关匹配该模型的独立配置和密钥，将系统提示词转为顶层 `system`，
向 `https://api.deepseek.com/anthropic/v1/messages` 发送 Messages 请求，再转换为统一 `LLMResponse`。
默认配置的输出上限为 8192，这是网关限制，不代表供应商最大输出能力。
截至 2026-11-04，DeepSeek 官方仍接受 `deepseek-v4-flash`，但对应请求由 V4.1 Flash 承接；
该配置保留用户指定的模型标识，不能用于锁定原始 V4 版本。详见 [模型配置管理说明](config/README.md)。

## 系统提示词管理

使用 `GET /v1/prompts` 查看可选模板、版本和参数要求，不需要模型密钥。
当前内置 `assistant` 的 `1.0.0` / `2.0.0` 版本，以及 `customer_support` 的 `1.0.0` 版本。

模板集中维护在 `app/prompts/templates/<模板名>/<版本>.json`，不需要改动接口或适配器代码。
模板正文通过 `${参数名}` 替换参数，支持必填参数和默认值。
详细格式及维护步骤见 [系统提示词管理说明](app/prompts/README.md)。

收到 `system_prompt` 后，网关精确查找模板版本、校验参数并渲染正文，
替换原有全部 `system` 消息，将结果放在 `messages` 首位，随后调用模型。
用户与助手的历史消息保持顺序，模板选择信息不会发送给上游。
未传 `system_prompt` 或传 null 时，兼容原有行为，原消息不变。
模板或参数错误时直接返回错误，不调用模型。

模板在应用创建时加载，修改后需要重启服务。重复版本或无效模板会在加载时报告错误。

## 内部模型适配器

`app/adapters/base.py` 中的 `ModelAdapter` 使用 Python `Protocol` 定义接口：

- `await complete(request)`：统一调用入口；按 `request.stream` 返回 `LLMResponse` 或已建立的 `CompletionStream`。
- `await healthcheck()`：检查上游连通性及认证，不生成聊天内容。
- `await aclose()`：释放适配器持有的资源。

`app/adapters/factory.py` 根据模型配置创建适配器：

- `deepseek` / `openai_compatible`：创建 `DeepSeekChatAdapter` / `ChatCompletionsAdapter`，采用 Bearer 认证，在 `base_url` 后追加 `/chat/completions`。
- `anthropic`：创建 `AnthropicMessagesAdapter`，采用 `x-api-key` 和 `anthropic-version: 2023-06-01`，调用 `/v1/messages`；`base_url` 已含 `/v1` 时不会重复追加。

`CompletionStream` 支持异步迭代统一的 `StreamUpdate`，并通过 `aclose()` 释放上游连接。
适配器使用单一 `complete()` 接口，不再提供独立的 `stream()` 方法或预留 Chunk 类型。
`app/streaming/` 负责 SSE 记录解析、供应商归一化和唯一终态控制；公开事件定义位于 `app/schemas/streaming.py`。
Chat Completions 请求用量尾包，供应商未返回用量时不推算；Messages 合并缓存输入并使用累计输出用量。

Messages 响应的文本块合并为 `content`，可见推理块合并为 `reasoning_content`，加密推理块不向调用方输出。
结束原因和 token 用量转换为统一字段；输入用量包含缓存创建与读取 token。
Messages 不提供创建时间，因此 `created` 为网关处理响应时的 Unix 秒数。
上游返回当前未支持的内容块（例如工具调用）时，返回 `UPSTREAM_INVALID_RESPONSE`。
适配器的健康检查使用模型列表接口；兼容服务未实现该接口时返回 false，不影响正常聊天或服务 `/health`。

传给适配器的请求使用真实模型名；返回客户端前，由模型注册表改回逻辑模型名。
非成功状态、超时、网络错误及响应信封校验异常统一转换为网关错误，不触发结构化修复重试。
网关创建的 HTTP 客户端显式使用 `retries=0`；只有结构化内容校验失败时，服务层才按 `max_retries` 发起修复调用。

对外路由通过依赖注入获得 `ModelRegistry`，由它按逻辑模型名选择适配器。
应用启动时为密钥已配置的模型创建客户端，同一进程内复用，关闭时统一释放。
部分客户端创建失败时，也会释放此前创建的客户端。
测试可覆盖 `app/api/dependencies.py` 中的 `get_model_registry`。

原有的 `ChatCompletionRequest` / `ChatCompletionResponse` 名称作为类型别名保留。
`DeepSeekChatAdapter.from_settings()` 保留给原有内部调用使用；网关路由不会回退读取旧的全局 `DEEPSEEK_API_KEY`。

## 错误响应

HTTP 错误、参数校验失败和未处理异常统一使用 `error` 对象。例如不存在的路径返回 HTTP 404：

```json
{
  "error": {
    "code": "HTTP_ERROR",
    "message": "Not Found"
  }
}
```

参数校验失败返回 HTTP 422，错误码为 `VALIDATION_ERROR`，并提供字段位置等 `details`。
未处理异常记录服务端日志，对客户端返回 HTTP 500 和 `INTERNAL_ERROR`，不返回内部异常信息。

## 测试与代码检查

统一验证全部已实现功能，并生成按功能分组的 JSON 汇总、JUnit 报告和执行日志：

```powershell
.\scripts\verify.ps1
```

也可使用跨平台入口，或只验证指定模块：

```powershell
uv run --locked python scripts/verify.py
.\scripts\verify.ps1 --list
.\scripts\verify.ps1 --group streaming usage governance
```

完整的功能点清单、分组说明、报告位置和通过标准见 [功能验证脚本](scripts/README.md)。
测试使用模拟上游及临时数据库，不会产生真实模型调用费用，也不会修改日常调用记录。

也可以直接运行底层工具：

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

DeepSeek 单元测试使用 `httpx2.MockTransport` 模拟上游，验证请求地址、认证头、请求体和响应解析，
不访问真实服务、不需要密钥，也不会产生模型调用费用。单独运行：

```powershell
uv run pytest tests/test_deepseek_adapter.py
uv run pytest tests/test_anthropic_adapter.py
uv run pytest tests/test_structured_output.py
```

聊天接口测试还覆盖统一协议、参数校验、上游错误映射、连接生命周期和依赖替换：

```powershell
uv run pytest tests/test_chat.py
```

系统提示词测试覆盖模板加载、精确版本、默认参数、JSON 参数、请求消息构建和模板列表：

```powershell
uv run pytest tests/test_prompts.py tests/test_chat.py
```

模型配置测试覆盖能力配置、独立密钥、逻辑路由、对外模型信息和客户端释放：

```powershell
uv run pytest tests/test_models.py tests/test_config.py tests/test_chat.py
```

需要格式化时运行 `uv run ruff format .`。
`uv.lock` 应随代码提交，新环境使用 `uv sync --locked` 安装一致的依赖版本。

## 目录结构

```text
llm-gateway/
├── app/
│   ├── main.py             # 应用工厂和入口
│   ├── api/
│   │   ├── router.py       # 路由汇总
│   │   ├── chat.py         # POST /v1/chat/completions
│   │   ├── dependencies.py # 模型适配器依赖
│   │   ├── prompts.py      # GET /v1/prompts
│   │   ├── models.py       # GET /v1/models
│   │   └── health.py       # 健康检查
│   ├── adapters/
│   │   ├── base.py         # ModelAdapter 协议
│   │   ├── deepseek.py     # DeepSeek 适配器
│   │   ├── compatible.py   # 通用 Chat Completions 协议
│   │   └── factory.py      # 按配置创建客户端
│   ├── schemas/
│   │   ├── chat.py         # 统一请求及非流式响应类型
│   │   ├── streaming.py    # 统一增量、用量和 SSE 事件类型
│   │   ├── prompts.py      # 模板选择和参数协议
│   │   └── models.py       # 模型配置和公开能力协议
│   ├── models/
│   │   └── registry.py     # 逻辑模型匹配与客户端管理
│   ├── prompts/
│   │   ├── registry.py     # 模板加载、匹配和渲染
│   │   ├── README.md       # 提示词维护说明
│   │   └── templates/      # 按名称和版本维护的 JSON 模板
│   └── core/
│       ├── config.py       # 配置加载和校验
│       ├── logging.py      # 日志配置
│       └── errors.py       # 异常处理
├── config/
│   ├── models.json        # 内部模型配置，不含密钥值
│   └── README.md          # 模型维护说明
├── scripts/               # 统一功能验证入口和功能点清单
├── tests/                 # 接口、配置、流式输出和调用治理测试
├── .env.example           # 环境变量示例
├── .gitignore
├── .python-version
├── pyproject.toml         # 依赖和开发工具配置
├── uv.lock                # 锁定依赖版本
└── README.md
```

## 下一步

逐步加入流式结构化输出、工具调用、多模态和网关鉴权。
