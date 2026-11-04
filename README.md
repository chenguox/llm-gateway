# LLM Gateway

基于 Python、FastAPI 和 uv 的 LLM Gateway 项目骨架，按功能逐步完善。

当前包含健康检查、环境变量配置、控制台日志、统一错误响应、自动 API 文档和基础测试。
健康检查只确认 HTTP 服务正在运行，不检查外部模型服务。
内部已提供模型适配器协议和 DeepSeek 普通请求实现，暂未暴露对外的模型调用路由。

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

## 内部模型适配器

`app/adapters/base.py` 中的 `ModelAdapter` 使用 Python `Protocol` 定义接口：

- `await complete(request)`：返回统一的 `ChatCompletionResponse`。
- `stream(request)`：返回 `AsyncIterator[ChatCompletionChunk]`，供 `async for` 消费。
- `await healthcheck()`：返回上游连通性及认证检查结果 `bool`。

请求、完整响应和流式块的类型位于 `app/schemas/chat.py`。
目前请求支持 `model`、`messages`、`temperature`、`top_p` 和 `max_tokens`。
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
from app.schemas.chat import ChatCompletionRequest, ChatMessage


async def ask_model() -> str | None:
    async with DeepSeekChatAdapter.from_settings(get_settings()) as deepseek:
        adapter: ModelAdapter = deepseek
        response = await adapter.complete(
            ChatCompletionRequest(
                model="deepseek-chat",
                messages=[ChatMessage(role="user", content="你好，请介绍一下自己。")],
            )
        )
        return response.choices[0].message.content
```

适配器可在同一个异步上下文中复用，以复用 HTTP 连接。退出上下文时关闭自行创建的客户端；
通过 `client=` 注入的客户端由调用方关闭。缺少密钥不会影响 `/health` 或应用启动，
仅在尝试创建 DeepSeek 适配器时报告配置错误。

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

需要格式化时运行 `uv run ruff format .`。
`uv.lock` 应随代码提交，新环境使用 `uv sync --locked` 安装一致的依赖版本。

## 目录结构

```text
llm-gateway/
├── app/
│   ├── main.py             # 应用工厂和入口
│   ├── api/
│   │   ├── router.py       # 路由汇总
│   │   └── health.py       # 健康检查
│   ├── adapters/
│   │   ├── base.py         # ModelAdapter 协议
│   │   └── deepseek.py     # DeepSeek Chat Completions 实现
│   ├── schemas/
│   │   └── chat.py         # 统一请求、响应和流式块类型
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

将内部适配器接入对外的 `POST /v1/chat/completions` 路由，
打通客户端 → Gateway → 模型服务的调用链路，再逐步加入流式响应、鉴权和多模型路由。
