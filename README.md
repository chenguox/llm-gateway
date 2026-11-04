# LLM Gateway

基于 Python、FastAPI 和 uv 的 LLM Gateway 项目骨架，按功能逐步完善。

当前包含健康检查、环境变量配置、控制台日志、统一错误响应、系统提示词管理、自动 API 文档和聊天接口。
健康检查只确认 HTTP 服务正在运行，不检查外部模型服务。
通过统一的 `LLMRequest` / `LLMResponse` 协议和 `POST /v1/chat/completions` 接口调用 DeepSeek。
目前支持文本聊天和非流式响应。

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
| `DEEPSEEK_API_KEY` | 空 | DeepSeek 密钥，仅在创建 DeepSeek 适配器时必填 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek API 根地址，也支持以 `/v1` 结尾的地址 |
| `DEEPSEEK_TIMEOUT_SECONDS` | `60` | 单次 HTTP 网络操作的超时秒数，必须大于 0 |

`ENVIRONMENT` 目前仅作为环境标识，不会自动切换部署配置。监听地址和端口通过 Uvicorn 参数设置；
Uvicorn 自身日志级别可用 `--log-level` 设置。
真实密钥应保存在本地 `.env` 或部署环境变量中，不要写入 `.env.example`。

## 统一聊天接口协议

接口为 `POST /v1/chat/completions`，请求和响应使用 `application/json`。
协议定义位于 `app/schemas/chat.py`，也可从 `app.schemas` 导入 `LLMRequest` 和 `LLMResponse`。
Swagger 文档中的请求和响应类型分别为 `LLMRequest` 和 `LLMResponse`。

### LLM Request

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `model` | string | 是 | 模型名称，去除首尾空白后不能为空，例如 `deepseek-chat` |
| `messages` | array | 是 | 至少一条消息，按对话顺序排列 |
| `messages[].role` | string | 是 | `system`、`user` 或 `assistant` |
| `messages[].content` | string | 是 | 消息文本 |
| `system_prompt` | object / null | 否 | 选择系统提示词模板，仅由网关处理，不转发给上游 |
| `system_prompt.name` | string | 选择模板时必填 | 模板名称，例如 `assistant` |
| `system_prompt.version` | string | 选择模板时必填 | 精确版本，例如 `1.0.0` |
| `system_prompt.parameters` | object | 否 | 模板参数，默认 `{}`，支持 JSON 值 |
| `temperature` | number / null | 否 | 范围 0～2；省略或为 null 时不传给上游 |
| `top_p` | number / null | 否 | 范围 0～1；省略或为 null 时不传给上游 |
| `max_tokens` | integer / null | 否 | 正整数；省略或为 null 时不传给上游 |
| `stream` | boolean | 否 | 默认 `false`，本次仅支持非流式响应，`true` 返回 HTTP 422 |

请求顶层不接受未定义字段，避免静默忽略不支持的功能。
模型名称传给 DeepSeek，由上游判断模型是否可用；目前所有聊天请求均使用 DeepSeek 适配器。

请求示例：

```json
{
  "model": "deepseek-chat",
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

### LLM Response

成功时返回 HTTP 200，响应字段如下：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 上游生成的响应标识 |
| `object` | string | 固定为 `chat.completion` |
| `created` | integer | 上游生成响应时的 Unix 时间戳，单位为秒 |
| `model` | string | 上游返回的模型名称 |
| `choices` | array | 至少一个候选结果 |
| `choices[].index` | integer | 非负候选索引 |
| `choices[].message.role` | string | 固定为 `assistant` |
| `choices[].message.content` | string / null | 生成的文本，可省略 |
| `choices[].message.reasoning_content` | string / null | 可选的推理内容 |
| `choices[].finish_reason` | string / null | 可选的结束原因，例如 `stop`、`length` |
| `usage` | object / null | 可选的 token 用量 |
| `usage.prompt_tokens` | integer | 非负输入 token 数 |
| `usage.completion_tokens` | integer | 非负输出 token 数 |
| `usage.total_tokens` | integer | 非负总 token 数 |

响应会保留上游在响应顶层、助手消息和用量对象中返回的扩展字段。
上游未返回的可选字段不会额外补充到接口响应中。

响应示例：

```json
{
  "id": "chatcmpl-example",
  "object": "chat.completion",
  "created": 1700000000,
  "model": "deepseek-chat",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "你好！我是一个人工智能助手。"},
      "finish_reason": "stop"
    }
  ],
  "usage": {"prompt_tokens": 20, "completion_tokens": 12, "total_tokens": 32}
}
```

在 `.env` 中配置 `DEEPSEEK_API_KEY` 并启动服务后，可在 PowerShell 中调用：

```powershell
$payload = @{
    model = "deepseek-chat"
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
| 422 | `VALIDATION_ERROR` | 请求参数不合法或请求流式响应 |
| 404 | `PROMPT_TEMPLATE_NOT_FOUND` | 系统提示词模板名或版本不存在 |
| 422 | `PROMPT_PARAMETER_ERROR` | 模板参数缺失或提供了未定义参数 |
| 503 | `PROVIDER_NOT_CONFIGURED` | 未配置 DeepSeek 密钥或密钥为空白 |
| 429 | `UPSTREAM_RATE_LIMITED` | 上游返回限流状态 |
| 504 | `UPSTREAM_TIMEOUT` | 上游连接、读取等操作超时 |
| 502 | `UPSTREAM_HTTP_ERROR` | 上游返回其他非成功状态，包括上游认证失败 |
| 502 | `UPSTREAM_CONNECTION_ERROR` | 上游网络或传输协议错误 |
| 502 | `UPSTREAM_INVALID_RESPONSE` | 上游返回无效 JSON 或不符合响应协议的数据 |

例如，上游超时返回：

```json
{
  "error": {
    "code": "UPSTREAM_TIMEOUT",
    "message": "Model provider request timed out"
  }
}
```

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

- `await complete(request)`：接收 `LLMRequest`，返回统一的 `LLMResponse`。
- `stream(request)`：返回 `AsyncIterator[ChatCompletionChunk]`，供 `async for` 消费。
- `await healthcheck()`：返回上游连通性及认证检查结果 `bool`。

请求、完整响应和流式块的类型位于 `app/schemas/chat.py`。
原有的 `ChatCompletionRequest` / `ChatCompletionResponse` 名称作为别名保留，兼容已有调用代码。
响应保留候选内容、结束原因、token 用量，以及 DeepSeek 可选的 `reasoning_content`。

`DeepSeekChatAdapter.complete()` 使用 Bearer 认证调用上游 `POST /chat/completions`，
固定发送 `stream=false`，并校验返回 JSON。非成功 HTTP 状态抛出 `httpx2.HTTPStatusError`，
网络错误、超时和响应校验错误交由调用方处理；暂不自动重试。
`healthcheck()` 调用 `GET /models`，网络错误或非成功状态返回 `False`，不会生成聊天内容。
`stream()` 当前明确抛出 `NotImplementedError`，后续再实现 SSE 解析。

配置 `.env` 中的 `DEEPSEEK_API_KEY` 后，可在异步业务代码中使用：

```python
from app.adapters import DeepSeekChatAdapter, ModelAdapter
from app.core.config import get_settings
from app.schemas.chat import ChatMessage, LLMRequest


async def ask_model() -> str | None:
    async with DeepSeekChatAdapter.from_settings(get_settings()) as deepseek:
        adapter: ModelAdapter = deepseek
        response = await adapter.complete(
            LLMRequest(
                model="deepseek-chat",
                messages=[ChatMessage(role="user", content="你好，请介绍一下自己。")],
            )
        )
        return response.choices[0].message.content
```

适配器可在同一个异步上下文中复用，以复用 HTTP 连接。退出上下文时关闭自行创建的客户端；
通过 `client=` 注入的客户端由调用方关闭。
对外聊天接口通过依赖注入获取适配器：应用启动时创建适配器，同一进程内复用其 HTTP 连接，
应用关闭时释放客户端。测试或后续扩展可覆盖 `app/api/dependencies.py` 中的 `get_model_adapter`。
缺少密钥不会影响 `/health` 或应用启动，调用聊天接口时返回 HTTP 503。

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

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

DeepSeek 单元测试使用 `httpx2.MockTransport` 模拟上游，验证请求地址、认证头、请求体和响应解析，
不访问真实服务、不需要密钥，也不会产生模型调用费用。单独运行：

```powershell
uv run pytest tests/test_deepseek_adapter.py
```

聊天接口测试还覆盖统一协议、参数校验、上游错误映射、连接生命周期和依赖替换：

```powershell
uv run pytest tests/test_chat.py
```

系统提示词测试覆盖模板加载、精确版本、默认参数、JSON 参数、请求消息构建和模板列表：

```powershell
uv run pytest tests/test_prompts.py tests/test_chat.py
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
│   │   └── health.py       # 健康检查
│   ├── adapters/
│   │   ├── base.py         # ModelAdapter 协议
│   │   └── deepseek.py     # DeepSeek Chat Completions 实现
│   ├── schemas/
│   │   ├── chat.py         # 统一请求、响应和流式块类型
│   │   └── prompts.py      # 模板选择和参数协议
│   ├── prompts/
│   │   ├── registry.py     # 模板加载、匹配和渲染
│   │   ├── README.md       # 提示词维护说明
│   │   └── templates/      # 按名称和版本维护的 JSON 模板
│   └── core/
│       ├── config.py       # 配置加载和校验
│       ├── logging.py      # 日志配置
│       └── errors.py       # 异常处理
├── tests/                 # 接口与配置测试
├── .env.example           # 环境变量示例
├── .gitignore
├── .python-version
├── pyproject.toml         # 依赖和开发工具配置
├── uv.lock                # 锁定依赖版本
└── README.md
```

## 下一步

逐步加入流式响应、工具调用、多模态、网关鉴权和多供应商模型路由。
