# 模型配置管理

调用方使用逻辑模型名。网关根据 `models.json` 查找真实模型、地址、独立密钥变量及能力，创建并选择对应客户端。
对外请求和响应的 `model` 都是逻辑模型名，只有发往上游的请求使用真实模型名。

## 维护位置

默认文件为项目下的 `config/models.json`，也可以通过 `MODELS_CONFIG_PATH` 指定其他文件。
默认路径相对于代码定位；自定义相对路径则相对于工作目录。建议从项目根目录启动服务。
配置在应用创建时加载，客户端在应用启动时创建，配置或密钥变化后需要重启服务。

示例配置包含 `general-chat`、`coding-assistant` 和 `flash-chat`。
前两个逻辑模型使用 Chat Completions 协议；`flash-chat` 使用 Messages 协议调用 DeepSeek V4 Flash。
三个入口使用不同的密钥变量；可以独立修改实际模型和服务地址。
示例能力采用保守配置，仅声明当前示例使用的文本非流式能力，不代表供应商的全部能力。

## 模型字段

文件顶层包含 `models` 数组，每个对象包含：

| 字段 | 说明 |
| --- | --- |
| `logical_name` | 对外逻辑模型名，不能为空且不能重复，按名称精确匹配 |
| `provider` | `deepseek`、`openai_compatible` 或 `anthropic`，决定客户端适配器类型和上游协议 |
| `model` | 上游真实模型名，不能为空 |
| `base_url` | HTTP/HTTPS 服务根地址或 API 前缀，例如以 `/v1` 结尾的地址 |
| `api_key_env` | 独立密钥环境变量名，不能保存密钥值 |
| `timeout_seconds` | 正数，默认 60，表示每次 HTTP 网络操作的超时秒数 |
| `capabilities` | 模型能力配置，字段见下表 |

密钥变量名必须符合 `[A-Za-z_][A-Za-z0-9_]*`。
不同模型必须配置不同的变量名；校验时不区分大小写，避免不同部署系统上的冲突。
同一供应商、同一真实模型的不同逻辑入口，也需要分别配置密钥变量名。

| provider | 上游协议 | 请求路径 | 认证 |
| --- | --- | --- | --- |
| `deepseek` | Chat Completions | `base_url` + `/chat/completions` | `Authorization: Bearer <key>` |
| `openai_compatible` | Chat Completions | `base_url` + `/chat/completions` | `Authorization: Bearer <key>` |
| `anthropic` | Anthropic Messages | `base_url` + `/v1/messages`；已含 `/v1` 时仅追加 `/messages` | `x-api-key: <key>` |

`provider` 在当前配置中用于选择协议适配器，可以指向兼容该协议的其他供应商。
不要在 `base_url` 中写完整聊天接口路径。Messages 适配器固定发送 `anthropic-version: 2023-06-01`。
Anthropic 官方服务可配置 `https://api.anthropic.com`，真实模型名填写该账号可调用的 Claude 模型。

## DeepSeek V4 Flash 配置

内置 `flash-chat` 的关键配置如下，完整字段见 `models.json`：

```json
{
  "logical_name": "flash-chat",
  "provider": "anthropic",
  "model": "deepseek-v4-flash",
  "base_url": "https://api.deepseek.com/anthropic",
  "api_key_env": "FLASH_CHAT_API_KEY",
  "timeout_seconds": 60,
  "capabilities": {
    "chat": true,
    "reasoning": true,
    "structured_output": {"supported": false, "modes": []},
    "max_output_tokens": 8192
  }
}
```

实际请求地址为 `https://api.deepseek.com/anthropic/v1/messages`。
调用方继续使用 `/v1/chat/completions` 和逻辑名 `flash-chat`，协议由配置决定。
系统模板渲染完成后，适配器移除 `messages` 中的 system 消息，将它们以空行连接为顶层 `system`；
用户和助手历史消息顺序保持不变。Messages 请求必须至少包含一条用户或助手消息。

响应的文本和可见推理分别转换为统一消息的 `content` 和 `reasoning_content`。
`end_turn` / `stop_sequence` 映射为 `stop`，输出或上下文限制映射为 `length`，
`refusal` 映射为 `content_filter`，其他结束原因原样保留。
输入 token 用量合计普通输入、缓存创建和缓存读取；创建时间由网关生成。
目前只接收文本、可见推理及加密推理块；加密推理不输出，其他未知块会被视为无效上游响应。

截至 **2026-11-04**，DeepSeek 官方文档说明旧标识 `deepseek-v4-flash` 仍可调用，
但原模型已退役，请求由 DeepSeek-V4.1-Flash-1017 承接；该标识不能锁定原始 V4 模型。
这里按需求保留 `deepseek-v4-flash`；若需要供应商当前通用别名，可将内部 `model` 改为 `deepseek-flash`。
`max_output_tokens=8192` 是本项目设置的输出上限和默认值，不是供应商能力上限。
该入口未启用结构化输出、流式、工具调用或视觉输入。

协议及模型标识参考：[DeepSeek Anthropic 兼容接口](https://api-docs.deepseek.com/guides/anthropic_api/)、
[DeepSeek 模型说明](https://api-docs.deepseek.com/quick_start/pricing/)、
[Anthropic Messages](https://platform.claude.com/docs/en/api/messages)。

## 模型能力

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `chat` | true | 是否支持聊天，false 时聊天请求返回 422 |
| `streaming` | false | 是否支持流式输出 |
| `tools` | false | 是否支持工具调用 |
| `vision` | false | 是否支持视觉输入 |
| `reasoning` | false | 是否支持推理能力 |
| `structured_output.supported` | false | 是否支持结构化输出 |
| `structured_output.modes` | `[]` | 支持的结构化输出模式 |
| `context_window` | null | 可选的正整数，上下文窗口大小，仅作为能力信息展示 |
| `max_output_tokens` | null | 可选的正整数，允许的最大输出 token 数 |

例如，模型确认支持两种结构化输出模式时，可配置：

```json
{
  "structured_output": {
    "supported": true,
    "modes": ["json_object", "json_schema"]
  }
}
```

- `json_object`：JSON 对象输出模式。
- `json_schema`：按 JSON Schema 约束输出的模式。
- 不支持结构化输出时，必须配置 `supported=false`、`modes=[]`。
- 支持时至少指定一种模式；不允许未定义模式或重复模式。

本轮只记录和展示结构化输出能力，聊天请求尚不接受 `response_format` 或 JSON Schema。
同样，模型能力声明不会自动启用网关当前尚未实现的流式、工具调用或多模态接口。

如果配置了 `max_output_tokens`，请求超过上限会直接返回错误，不会静默截断。
请求未指定 `max_tokens` 或值为 null 时，使用配置上限作为发往上游的 `max_tokens`。
如果配置也没有输出上限，Messages 适配器使用 1024，因为上游协议要求提供 `max_tokens`；
Chat Completions 适配器则不发送此字段。采样参数直接透传，取值还需符合所选上游模型的限制。
配置同时指定上下文窗口和最大输出时，最大输出不能超过上下文窗口。
当前不对输入进行 tokenizer 计数，`context_window` 不用于推断请求是否超长。

## 独立密钥

配置文件只写变量名：

```json
{
  "logical_name": "general-chat",
  "api_key_env": "GENERAL_CHAT_API_KEY"
}
```

密钥值放在系统环境变量或本地 `.env`：

```dotenv
GENERAL_CHAT_API_KEY=第一个模型的密钥
CODING_ASSISTANT_API_KEY=第二个模型的密钥
FLASH_CHAT_API_KEY=Flash模型的独立密钥
```

读取规则为系统环境变量优先于 `.env`，只读取模型指定的变量。
变量缺失、为空或仅包含空白时，该模型不可调用，不会回退到其他模型的变量或旧全局密钥。
系统环境变量已存在但为空时，不会继续使用 `.env` 中同名变量的值。
密钥已配置的模型分别创建客户端，进程内复用，应用关闭时统一释放。

缺少某个模型密钥不会阻止应用启动，也不影响其他模型、`GET /health`、`GET /v1/models` 或 `GET /v1/prompts`。
模型列表展示的是配置能力，不表示已完成真实上游连通性或密钥有效性检查。

## 调用与迁移

1. 在配置文件中维护真实模型信息及能力，为每个模型分配独立的 `api_key_env`。
2. 在环境变量或 `.env` 中配置对应密钥值，原有密钥值可以迁移到新的变量中。
3. 将客户端请求的 `model` 改为逻辑模型名，例如从真实模型名改为 `general-chat`。
4. 重启服务，通过 `GET /v1/models` 查看模型，调用 `POST /v1/chat/completions` 验证。

`GET /v1/models` 只返回逻辑模型名和能力，不返回真实模型名、供应商、地址、密钥变量名或密钥值。
系统提示词的选择和参数保持原有协议，可以与任意已配置逻辑模型组合。
原有 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_TIMEOUT_SECONDS` 不再决定网关路由。
它们仅保留用于兼容原来直接调用 `DeepSeekChatAdapter.from_settings()` 的内部代码。

## 错误处理

| HTTP 状态码 | 错误码 | 场景 |
| --- | --- | --- |
| 404 | `MODEL_NOT_FOUND` | 未配置该逻辑模型，不会回退选择其他模型 |
| 503 | `MODEL_NOT_CONFIGURED` | 该模型的密钥缺失或为空 |
| 422 | `MODEL_CAPABILITY_NOT_SUPPORTED` | 模型配置不支持聊天 |
| 422 | `MODEL_TOKEN_LIMIT_EXCEEDED` | 请求输出 token 数超过配置上限 |
| 422 | `INVALID_MESSAGES` | Messages 请求缺少用户或助手消息 |

无效 JSON、逻辑名重复、密钥变量重复、不支持的适配器、无效地址及矛盾的能力配置在应用创建时报告错误。
上游限流、认证失败、超时、网络错误等沿用聊天接口已有的统一错误处理。
