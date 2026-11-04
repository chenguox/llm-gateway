"""将统一文本请求转换为 Anthropic Messages 协议，并归一化响应。"""

from time import time
from types import TracebackType
from typing import Self

import httpx2 as httpx

from app.core.errors import GatewayError
from app.schemas.anthropic import (
    AnthropicMessageResponse,
    AnthropicTextBlock,
    AnthropicThinkingBlock,
)
from app.schemas.chat import (
    AssistantMessage,
    ChatCompletionChoice,
    ChatCompletionUsage,
    LLMRequest,
    LLMResponse,
)
from app.schemas.structured_output import JsonSchemaFormat
from app.streaming.anthropic import finish_reason, normalize_messages
from app.streaming.provider import CompletionStream, open_completion_stream
from app.structured_output.formats import native_schema


class AnthropicMessagesAdapter:
    """支持 Claude 原生及兼容服务的文本 Messages 请求。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.anthropic.com",
        timeout: float = 60.0,
        default_max_tokens: int = 1024,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """配置 Messages 服务、协议版本、默认输出长度及客户端所有权。"""
        # 上游要求提供密钥和输出长度，在创建客户端前检查这些基础参数。
        if not api_key.strip():
            raise ValueError("Model API key must not be empty")
        if timeout <= 0:
            raise ValueError("Model timeout must be positive")
        if default_max_tokens <= 0:
            raise ValueError("Default maximum tokens must be positive")
        # 兼容服务根地址、带 /anthropic 的前缀及已有 /v1 的地址，避免重复追加版本路径。
        base_url = base_url.rstrip("/")
        self._api_url = base_url if base_url.endswith("/v1") else f"{base_url}/v1"
        # Messages 使用独立的密钥头和协议版本头，版本固定为本适配器实现的格式。
        self._headers = {
            "x-api-key": api_key.strip(),
            "anthropic-version": "2023-06-01",
        }
        self._timeout = timeout
        self._default_max_tokens = default_max_tokens
        # 内部创建的客户端由适配器关闭；注入客户端由其创建方管理。
        self._owns_client = client is None
        self._client = (
            client
            if client is not None
            else httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0))
        )

    async def complete(self, request: LLMRequest) -> LLMResponse | CompletionStream:
        """提取 system 并调用 Messages，按 stream 参数返回响应或事件流。"""
        # 此协议版本的 messages 仅承载用户和助手消息，system 在下方单独组装。
        messages = [
            message.model_dump() for message in request.messages if message.role != "system"
        ]
        if not messages:
            # 提前拒绝只有系统提示词的请求，避免发送无效的上游调用。
            raise GatewayError(
                422, "INVALID_MESSAGES", "Messages requests require a user or assistant message"
            )
        # model 已由模型注册表替换为真实模型名；上游必填 max_tokens，未指定时使用默认值。
        payload = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_tokens or self._default_max_tokens,
            "stream": request.stream,
        }
        system_messages = [
            message.content for message in request.messages if message.role == "system"
        ]
        if system_messages:
            # 模板渲染后的系统消息也从这里提取；多条系统消息按原顺序以空行拼接。
            payload["system"] = "\n\n".join(system_messages)
        for name in ("temperature", "top_p"):
            value = getattr(request, name)
            if value is not None:
                # 只跳过 None，保留 0 等有效采样值；模板选择信息不会进入上游请求体。
                payload[name] = value
        if isinstance(request.response_format, JsonSchemaFormat):
            payload["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": native_schema(
                        request.response_format.json_schema.schema_, require_all=False
                    ),
                }
            }
        elif request.response_format is not None:
            raise GatewayError(
                422, "MODEL_CAPABILITY_NOT_SUPPORTED", "Messages does not support JSON object mode"
            )
        if request.stream:
            return await open_completion_stream(
                self._client,
                f"{self._api_url}/messages",
                headers=self._headers,
                payload=payload,
                timeout=self._timeout,
                normalize=normalize_messages,
            )
        response = await self._client.post(
            f"{self._api_url}/messages",
            headers=self._headers,
            json=payload,
            timeout=self._timeout,
        )
        # HTTP、JSON 解析和结构校验异常交由 API 层映射，避免透传上游内部错误详情。
        response.raise_for_status()
        return self._normalize(AnthropicMessageResponse.model_validate(response.json()))

    def _normalize(self, response: AnthropicMessageResponse) -> LLMResponse:
        """合并文本和推理块，计算包含缓存的用量，并映射结束原因。"""
        # 按内容块出现的顺序拼接文本，统一返回一条助手消息。
        message = AssistantMessage(
            role="assistant",
            content="".join(
                block.text for block in response.content if isinstance(block, AnthropicTextBlock)
            ),
        )
        # 只提取可见推理内容；加密推理块不会作为文本或 reasoning_content 返回。
        thinking = [
            block.thinking
            for block in response.content
            if isinstance(block, AnthropicThinkingBlock)
        ]
        if thinking:
            message.reasoning_content = "".join(thinking)
        # 上游将普通输入、缓存创建和缓存读取分开计数，统一协议需要合计输入 token。
        prompt_tokens = (
            response.usage.input_tokens
            + response.usage.cache_creation_input_tokens
            + response.usage.cache_read_input_tokens
        )
        # 将常见结束原因映射到统一协议；其余原因（包括 None）原样保留。
        reason = finish_reason(response.stop_reason) if response.stop_reason is not None else None
        return LLMResponse(
            id=response.id,
            object="chat.completion",
            # Messages 响应没有创建时间，使用网关转换响应时的 Unix 秒数。
            created=int(time()),
            # 此处保留上游模型名，模型注册表会在返回调用方前改回逻辑模型名。
            model=response.model,
            choices=[ChatCompletionChoice(index=0, message=message, finish_reason=reason)],
            usage=ChatCompletionUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=response.usage.output_tokens,
                total_tokens=prompt_tokens + response.usage.output_tokens,
            ),
        )

    async def healthcheck(self) -> bool:
        """检查上游模型列表，不通过生成消息消耗 token。"""
        try:
            response = await self._client.get(
                f"{self._api_url}/models", headers=self._headers, timeout=self._timeout
            )
            response.raise_for_status()
        except httpx.HTTPError:
            # 兼容服务未提供模型列表接口时也会返回 False，不代表其聊天接口一定不可用。
            return False
        return True

    async def aclose(self) -> None:
        """仅释放内部创建的客户端，避免关闭调用方共享的连接。"""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        """进入异步上下文，复用当前已配置的适配器。"""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """无论请求成功还是发生异常，退出上下文时都执行资源清理。"""
        await self.aclose()
