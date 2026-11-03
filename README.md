# LLM Gateway

基于 Python、FastAPI 和 uv 的 LLM Gateway 项目骨架，按功能逐步完善。

当前包含健康检查、环境变量配置、控制台日志、统一错误响应、自动 API 文档和基础测试。
健康检查只确认 HTTP 服务正在运行，不检查外部模型服务。当前尚未接入模型调用。

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

`ENVIRONMENT` 目前仅作为环境标识，不会自动切换部署配置。监听地址和端口通过 Uvicorn 参数设置；
Uvicorn 自身日志级别可用 `--log-level` 设置。
真实密钥应保存在本地 `.env` 或部署环境变量中，不要写入 `.env.example`。

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

接入一个模型供应商，实现最小的 `POST /v1/chat/completions` 非流式请求，
打通客户端 → Gateway → 模型服务的调用链路，再逐步加入流式响应、鉴权和多模型路由。
