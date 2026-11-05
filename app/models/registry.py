"""维护逻辑模型配置与客户端，完成模型匹配、能力校验和上游调用。"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from app.adapters.base import ModelAdapter
from app.adapters.factory import create_adapter
from app.core.config import Settings
from app.core.errors import GatewayError
from app.governance.context import current_call
from app.governance.invocation import invoke
from app.schemas.chat import LLMRequest, LLMResponse
from app.schemas.models import LogicalModelInfo, ModelConfiguration, ModelsConfiguration
from app.streaming.provider import CompletionStream
from app.structured_output.service import complete_structured


class ModelRegistry:
    """以逻辑模型名关联内部配置和客户端，隔离调用方与真实模型信息。"""

    def __init__(self, configuration: ModelsConfiguration) -> None:
        # 配置已由 Pydantic 校验；建立索引，供后续请求按逻辑名直接查找。
        self._configurations = {model.logical_name: model for model in configuration.models}
        # 客户端在应用启动阶段创建；配置存在不代表对应客户端已就绪。
        self._adapters: dict[str, ModelAdapter] = {}

    @classmethod
    def from_file(cls, path: Path) -> "ModelRegistry":
        """读取配置文件，校验字段、名称唯一性及能力配置后创建注册表。"""
        try:
            configuration = ModelsConfiguration.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            # 将文件读取和配置校验错误统一标记为配置加载失败，并保留异常原因。
            raise ValueError(f"Invalid model configuration file: {path}") from exc
        return cls(configuration)

    @asynccontextmanager
    async def connect(self, settings: Settings) -> AsyncIterator[None]:
        """在应用生命周期内持有客户端，并在退出或初始化失败时统一释放。"""
        # 退出栈记录每个已创建客户端，避免后续客户端创建失败时泄漏连接。
        async with AsyncExitStack() as clients:
            try:
                for logical_name, configuration in self._configurations.items():
                    # 每个模型仅读取自己配置的密钥变量，不使用其他模型的密钥。
                    api_key = settings.resolve_model_key(configuration.api_key_env)
                    if api_key is None or not api_key.get_secret_value().strip():
                        # 缺少密钥时跳过该模型，允许其他模型和查询接口正常工作。
                        continue
                    adapter = create_adapter(configuration, api_key)
                    clients.push_async_callback(adapter.aclose)
                    self._adapters[logical_name] = adapter
                # 将控制权交给应用；应用运行期间复用上述客户端。
                yield
            finally:
                # 先移除可调用的客户端引用，随后由退出栈执行各客户端的关闭方法。
                self._adapters.clear()

    def list_models(self) -> list[LogicalModelInfo]:
        """按逻辑名排序，返回公开信息；列表不代表上游连通性已通过检查。"""
        # 显式构造公开模型，避免返回真实模型名、地址或密钥环境变量名。
        return [
            LogicalModelInfo(id=name, capabilities=self._configurations[name].capabilities)
            for name in sorted(self._configurations)
        ]

    def get_configuration(self, logical_name: str) -> ModelConfiguration:
        """精确查找逻辑模型；未配置时返回 404，不自动回退到其他模型。"""
        configuration = self._configurations.get(logical_name)
        if configuration is None:
            raise GatewayError(404, "MODEL_NOT_FOUND", "Logical model was not found")
        return configuration

    def get_adapter(self, logical_name: str) -> ModelAdapter:
        """获取已创建的客户端，区分模型不存在和客户端未就绪两种情况。"""
        # 先检查配置，保证未知逻辑名返回 404，而不是被误判为缺少密钥。
        self.get_configuration(logical_name)
        adapter = self._adapters.get(logical_name)
        if adapter is None:
            raise GatewayError(503, "MODEL_NOT_CONFIGURED", "Model credentials are not configured")
        return adapter

    async def complete(self, request: LLMRequest) -> LLMResponse | CompletionStream:
        """校验模型能力并统一调用客户端；完整响应恢复逻辑模型名。"""
        configuration = self.get_configuration(request.model)
        call = current_call.get()
        if call is not None:
            call.route(configuration)
        # 在发起上游请求前检查能力，避免向不支持聊天的模型发送请求。
        if not configuration.capabilities.chat:
            raise GatewayError(422, "MODEL_CAPABILITY_NOT_SUPPORTED", "Model does not support chat")
        if request.stream and not configuration.capabilities.streaming:
            raise GatewayError(
                422, "MODEL_CAPABILITY_NOT_SUPPORTED", "Model does not support streaming"
            )
        if request.stream and request.response_format is not None:
            raise GatewayError(
                422,
                "STREAMING_STRUCTURED_OUTPUT_NOT_SUPPORTED",
                "Streaming cannot be combined with response_format",
            )
        if configuration.provider == "anthropic" and all(
            message.role == "system" for message in request.messages
        ):
            raise GatewayError(
                422, "INVALID_MESSAGES", "Messages requests require a user or assistant message"
            )
        limit = configuration.capabilities.max_output_tokens
        # 显式请求超过配置上限时直接报错，不静默缩减调用方指定的值。
        if limit is not None and request.max_tokens is not None and request.max_tokens > limit:
            raise GatewayError(
                422, "MODEL_TOKEN_LIMIT_EXCEEDED", "Requested output exceeds model limit"
            )
        # 使用副本替换真实模型名，保留原请求中的逻辑名；未指定输出长度时使用配置上限。
        upstream_request = request.model_copy(
            update={
                "model": configuration.model,
                "max_tokens": request.max_tokens if request.max_tokens is not None else limit,
            }
        )
        adapter = self.get_adapter(request.model)
        if request.response_format is None:
            response = await invoke(adapter, upstream_request)
        else:
            response = await complete_structured(adapter, configuration, upstream_request)
        if isinstance(response, CompletionStream):
            return response
        # 对外的 model 字段始终使用逻辑名，调用方无需依赖供应商的真实模型名称。
        return response.model_copy(update={"model": request.model})
