# 结构化输出

`POST /v1/chat/completions` 接受 `response_format`。网关选择生成模式，校验模型返回的原始
`message.content`，只有通过 Pydantic 校验的结果才返回成功。
不传此参数或传 null 时保持普通文本调用，`max_retries` 不会启用文本校验或自动重试。

## 调用示例

只要求合法 JSON 对象时，传入 `"response_format": {"type": "json_object"}`。
该模式不约束具体业务字段；需要字段、类型和范围约束时，使用以下请求：

```json
{
  "model": "general-chat",
  "system_prompt": {
    "name": "assistant",
    "version": "1.0.0",
    "parameters": {"language": "中文"}
  },
  "messages": [{"role": "user", "content": "提取信息：张三，28岁。"}],
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "person",
      "strict": true,
      "schema": {
        "type": "object",
        "properties": {
          "name": {"type": "string", "minLength": 1},
          "age": {"type": "integer", "minimum": 0, "maximum": 150}
        },
        "required": ["name", "age"],
        "additionalProperties": false
      }
    }
  },
  "max_retries": 2
}
```

`json_schema.name` 为 1～64 个英文字母、数字、下划线或连字符。`strict` 默认且只能为 true。
`max_retries` 是严格整数，取值 0～3，默认 1；实际最多请求 `1 + max_retries` 次。
网关不会把重试参数转发给上游。

## 模式选择

| 请求 | 模型和协议的可用能力 | 执行方式 |
| --- | --- | --- |
| `json_schema` | 包含 `json_schema` | 原生 Schema 生成，原始 Schema 本地校验 |
| `json_schema` | 只有 `json_object` | JSON Object 生成，Schema 加入提示词，仍按原始 Schema 本地校验 |
| `json_object` | 包含 `json_object` | 原生 JSON Object 生成，Pydantic 校验顶层为对象 |
| 结构化格式 | 无对应模式 | 422 `MODEL_CAPABILITY_NOT_SUPPORTED`，不请求上游 |

默认 `general-chat`、`coding-assistant` 启用 `json_object`。`flash-chat` 使用 DeepSeek Messages 兼容服务，
目前未启用原生结构化输出，结构化请求会返回 422。协议兼容不代表支持该协议的全部功能。
DeepSeek 文档列出的 Messages `output_config` 仅支持 `effort`，参考
[DeepSeek 兼容说明](https://api-docs.deepseek.com/guides/anthropic_api/)。

Chat Completions 上游使用 `response_format`；原生 Claude Messages 使用
`output_config.format={"type":"json_schema","schema":...}`，参考
[Claude 结构化输出](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)。

系统模板先渲染，再补充 JSON 要求，保留原始任务。JSON Object 模式明确要求不带 Markdown 或解释文字，
附带 JSON 语法示例；有业务 Schema 时以业务 Schema 为准，也符合
[DeepSeek JSON 模式](https://api-docs.deepseek.com/guides/json_mode/) 的提示词要求。

## Schema 范围及严格校验

根节点必须是 `type: object`。通过动态 Pydantic 模型支持以下子集：

| 类型 | 关键字 |
| --- | --- |
| 所有类型 | `type`、`title`、`description`、由原始 JSON 标量组成的非空 `enum` |
| `object` | `properties`、`required`、布尔型 `additionalProperties` |
| `array` | 必填 `items`、`minItems`、`maxItems` |
| `string` | `minLength`、`maxLength`、`pattern` |
| `integer` / `number` | `minimum`、`maximum`、数值型 `exclusiveMinimum` / `exclusiveMaximum`、`multipleOf` |
| `boolean` / `null` | 对应 JSON 布尔值及 null |

嵌套字段支持 `["string", "null"]` 这类单一类型加 null 的联合。
可选字段省略时不会自动填成 null；`additionalProperties` 默认 true，显式 false 时拒绝额外字段。
枚举不支持对象或数组。`pattern` 使用 Pydantic 默认正则引擎，无法编译的表达式返回 422。
`$ref`、`$defs`、`anyOf`、`oneOf`、`format` 等未实现的关键字直接返回
`STRUCTURED_OUTPUT_SCHEMA_INVALID`，不会悄悄忽略。
编译深度上限 16、节点数上限 256；可空类型的非 null 分支也计入编译层级与节点数。

不会把字符串 `"28"` 转成数字，也不会把 true 当作整数。`integer` 要求 JSON 整数字面量，
不会将 `28.0` 转成 28。顶层数组、标量、多个连续 JSON、Markdown 包装、NaN、Infinity
或溢出成无穷大的数值都不能通过校验。不通过正则截取 JSON，也不自动改写业务值。

为兼容原生生成接口，上游 Schema 关闭额外字段；Chat Completions 的 strict 格式还要求输出所有已声明属性。
数值、长度、正则约束移入上游 Schema 的 description，系统要求保留完整原始 Schema。
这些是生成侧转换，本地校验始终采用调用方的原始规则，不放宽返回结果的校验。

## 有限修复重试

每次尝试只调用一次客户端，`service.py` 统一控制次数。校验失败后，保留初始任务、系统提示词、
模型、采样参数、输出预算和 Schema，只增加最近一次失败输出与字段级错误，要求最小修改并返回完整 JSON。
下一次失败时替换修复上下文，避免堆积全部失败历史；每个候选最多提供 10 条字段错误。

明确拒答直接返回 `STRUCTURED_OUTPUT_REFUSED`，不重试。输出截断即使恰好能解析为 JSON，
也会进入有限修复；不会自动增加输出预算。HTTP、限流、认证、网络、超时和响应信封损坏不触发内容修复。

网关创建的两个协议客户端均显式设置 HTTP `retries=0`，当前不使用带自动重试的厂商 SDK。
外部注入客户端由其创建方保证没有额外重试；厂商内部执行行为无法由客户端控制。
每次调用沿用模型配置的网络操作超时，没有覆盖全部尝试的总时限。

## 响应与错误

成功时保留最后一次原始 `choices[].message.content`，增加经过校验的 `choices[].message.parsed`：

```json
{
  "role": "assistant",
  "content": "{\"name\":\"张三\",\"age\":28}",
  "parsed": {"name": "张三", "age": 28}
}
```

所有 choice 都必须通过校验。顶层 `structured_output` 返回实际 `mode`、总调用次数 `attempts`、
所有尝试累计的 `total_usage`。原有顶层 `usage` 仍表示最后一次调用的用量。
任意一次缺少上游用量时，`total_usage` 为 null，避免把不完整合计当作总消耗。
普通文本请求不会主动增加 `parsed` 或 `structured_output` 字段。

修复次数耗尽时返回 502 `STRUCTURED_OUTPUT_VALIDATION_FAILED`。`error.details` 包含尝试次数、
字段位置、错误类型和累计用量，不包含失败输出正文或 Pydantic 的 input/context。

## 测试

```powershell
uv run --locked pytest tests/test_structured_output.py -q
```

测试使用 MockTransport，不访问真实模型、不需要真实密钥。
