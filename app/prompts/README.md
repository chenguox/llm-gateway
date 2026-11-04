# 系统提示词管理

系统提示词集中维护在 `app/prompts/templates/`，每个模板版本使用一个 UTF-8 JSON 文件。
应用创建时加载并校验全部模板，聊天请求按 JSON 中的 `name` 和 `version` 精确匹配。
修改模板后需要重启服务；不会自动选择最新版本，也不会在版本不存在时回退到其他版本。

## 查看和选择

`GET /v1/prompts` 返回可选模板的名称、版本、说明及参数定义，不返回模板正文。
此接口无需配置模型服务密钥，可在 `/docs` 中直接调用。

聊天请求增加 `system_prompt`：

```json
{
  "model": "general-chat",
  "system_prompt": {
    "name": "customer_support",
    "version": "1.0.0",
    "parameters": {"product": "示例产品", "language": "中文"}
  },
  "messages": [{"role": "user", "content": "如何使用这个产品？"}]
}
```

网关在调用模型前执行：

1. 按模板名和版本查找模板。
2. 检查必填参数及未定义参数，为未提供的可选参数使用默认值。
3. 渲染正文，移除原有全部 `system` 消息，并将渲染结果作为第一条 `system` 消息。
4. 保持 `user` / `assistant` 消息及其顺序，将构建后的请求交给模型适配器。

`system_prompt` 是网关参数，不会发送给上游模型服务。
为兼容原有调用，未提供该字段或提供 null 时，原有 `messages` 保持原样。

## 新增模板和版本

例如新增 `app/prompts/templates/my_assistant/1.0.0.json`：

```json
{
  "name": "my_assistant",
  "version": "1.0.0",
  "description": "自定义领域助手",
  "content": "你是一名${role}。请使用${language}回答用户的问题。",
  "parameters": {
    "role": {"description": "助手角色", "required": true},
    "language": {"description": "回答语言", "required": false, "default": "中文"}
  }
}
```

- `name` 和 `version` 不能为空，同一名称和版本组合不得重复。
- `content` 使用标准库 `string.Template` 的 `${参数名}` 占位符，字面量美元符号写为 `$$`。
- 正文中的占位符与 `parameters` 中声明的参数必须一一对应；同一占位符可重复使用。
- `required` 默认 true，要求调用方提供该参数；false 时，省略该参数则使用 `default`。
- 参数值可以是字符串、数字、布尔值、数组、对象或 null。字符串原样插入，其他类型按 JSON 编码插入。
- 参数值只替换一次，不会被继续解释为模板，也不执行表达式。
- 已发布版本建议保持不变；修改提示词时新增版本文件，让调用方明确选择升级。

模板重复、JSON 无效、占位符无效或参数声明不一致时，应用会在加载时报告错误。
模板目录相对于代码文件定位，不依赖启动命令所在目录。

## 请求错误

| HTTP 状态码 | 错误码 | 原因 |
| --- | --- | --- |
| 404 | `PROMPT_TEMPLATE_NOT_FOUND` | 模板名或版本不存在 |
| 422 | `PROMPT_PARAMETER_ERROR` | 缺少必填参数或提供了未定义参数 |
| 422 | `VALIDATION_ERROR` | 选择对象缺少名称、版本或字段类型错误 |

匹配或参数检查失败时不会调用上游模型，也不会静默使用其他模板。
