"""通过异步 HTTP 客户端调用兼容 Chat Completions 协议的模型服务。"""

from collections.abc import AsyncIterator
from types import TracebackType
from typing import Self

import httpx2 as httpx

from app.schemas.chat import ChatCompletionChunk, LLMRequest, LLMResponse
from app.schemas.structured_output import JsonSchemaFormat
from app.structured_output.formats import native_schema


class ChatCompletionsAdapter:
    """封装请求发送、统一响应校验、连通性检查和客户端资源释放。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """初始化服务地址、认证头和超时配置，并支持注入已有 HTTP 客户端。"""
        # 在创建客户端前检查必要配置，避免持有不可用的连接资源。
        if not api_key.strip():
            raise ValueError("Model API key must not be empty")
        if timeout <= 0:
            raise ValueError("Model timeout must be positive")
        # 统一移除末尾斜杠，后续在此地址后追加聊天或模型列表路径。
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key.strip()}"}
        self._timeout = timeout
        # 注入的客户端由调用方管理；适配器只负责关闭自己创建的客户端。
        self._owns_client = client is None
        self._client = (
            client
            if client is not None
            else httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0))
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """将统一请求发送给上游，并将上游 JSON 校验为统一响应。"""
        # 忽略值为 None 的可选参数；system_prompt 已由请求模型声明排除序列化。
        payload = request.model_dump(mode="json", exclude_none=True)
        # 当前仅实现非流式调用，确保上游返回完整 JSON 而不是事件流。
        payload["stream"] = False
        if isinstance(request.response_format, JsonSchemaFormat):
            specification = request.response_format.json_schema
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": specification.name,
                    "strict": True,
                    "schema": native_schema(specification.schema_, require_all=True),
                },
            }
        elif request.response_format is not None:
            payload["response_format"] = {"type": request.response_format.type}
        response = await self._client.post(
            f"{self._base_url}/chat/completions",
            headers=self._headers,
            json=payload,
            timeout=self._timeout,
        )
        # 将 HTTP 错误和响应校验异常向上传递，由 API 层统一转换为网关错误。
        response.raise_for_status()
        return LLMResponse.model_validate(response.json())

    def stream(self, request: LLMRequest) -> AsyncIterator[ChatCompletionChunk]:
        """预留流式接口，当前调用时明确报告尚未实现。"""
        raise NotImplementedError("Chat completion streaming is not implemented yet")

    async def healthcheck(self) -> bool:
        """访问需要认证的模型列表接口，检查连通性而不发起文本生成。"""
        try:
            response = await self._client.get(
                f"{self._base_url}/models",
                headers=self._headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            # 认证、网络或 HTTP 状态异常均表示此次检查未通过。
            return False
        return True

    async def aclose(self) -> None:
        """关闭内部创建的客户端，保留外部注入客户端的生命周期。"""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        """允许通过 async with 使用同一个适配器实例。"""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """退出上下文时释放适配器持有的资源，包括调用过程中发生异常的情况。"""
        await self.aclose()
