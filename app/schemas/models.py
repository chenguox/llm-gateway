"""定义内部模型配置、能力校验规则和对外公开的逻辑模型响应结构。"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints, model_validator

# 模型名称统一去除首尾空白，并拒绝空字符串或仅包含空白的名称。
ModelName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
# 结构化输出模式：JSON 对象，或通过 JSON Schema 约束输出结构。
OutputMode = Literal["json_object", "json_schema"]


class ConfigurationModel(BaseModel):
    """内部配置的公共基类，统一限制未知字段、字段重新赋值和错误文本中的输入值。"""

    # 禁止额外字段以发现配置拼写错误；frozen 限制字段赋值；错误文本不展示输入值。
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class StructuredOutputCapabilities(ConfigurationModel):
    """声明模型是否支持结构化输出，以及允许使用的模式。"""

    # 严格接收布尔值，避免将字符串或数字隐式转换为能力开关。
    supported: bool = Field(default=False, strict=True)
    # 为每个实例创建独立列表，默认不声明任何结构化输出模式。
    modes: list[OutputMode] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_modes(self) -> Self:
        """在字段校验完成后，检查支持状态与模式列表是否一致。"""
        # 支持时至少指定一种模式；不支持时模式列表必须为空。
        if self.supported != bool(self.modes):
            raise ValueError("Structured output support must match the configured modes")
        # 同一种模式只能声明一次，避免重复配置。
        if len(self.modes) != len(set(self.modes)):
            raise ValueError("Structured output modes must not repeat")
        return self


class ModelCapabilities(ConfigurationModel):
    """维护模型能力及 token 限制；能力声明不会自动启用网关尚未实现的功能。"""

    # 基础能力开关：聊天、流式输出、工具调用、视觉输入和推理能力。
    chat: bool = Field(default=True, strict=True)
    streaming: bool = Field(default=False, strict=True)
    tools: bool = Field(default=False, strict=True)
    vision: bool = Field(default=False, strict=True)
    reasoning: bool = Field(default=False, strict=True)
    # 通过工厂为每个模型创建独立的结构化输出能力配置。
    structured_output: StructuredOutputCapabilities = Field(
        default_factory=StructuredOutputCapabilities
    )
    # 上下文窗口大小仅用于展示能力信息，当前不据此计算输入 token 数。
    context_window: int | None = Field(default=None, gt=0, strict=True)
    # 输出上限由模型注册表检查；请求未指定输出长度时，也作为默认值使用。
    max_output_tokens: int | None = Field(default=None, gt=0, strict=True)

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        """同时配置两个限制时，确保输出上限不超过整个上下文窗口。"""
        if (
            self.context_window is not None
            and self.max_output_tokens is not None
            and self.max_output_tokens > self.context_window
        ):
            raise ValueError("Maximum output tokens must not exceed the context window")
        return self


class ModelConfiguration(ConfigurationModel):
    """单个模型的内部配置，关联逻辑名称、真实模型、客户端类型和独立密钥。"""

    # 调用方使用的逻辑模型名，注册表使用该名称进行精确查找。
    logical_name: ModelName
    # 决定由客户端工厂创建哪一种适配器，而非真实模型的名称。
    provider: Literal["deepseek", "openai_compatible", "anthropic"]
    # 仅在网关内部使用，发起上游请求时替换逻辑模型名。
    model: ModelName
    # 上游服务根地址或 API 前缀，由适配器追加具体接口路径。
    base_url: HttpUrl
    # 这里只保存环境变量名；密钥值由配置读取逻辑从环境变量或 .env 中获取。
    api_key_env: Annotated[
        str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    ]
    # 单位为秒，必须为有限正数，不接受无穷大或 NaN。
    timeout_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    capabilities: ModelCapabilities


class ModelsConfiguration(ConfigurationModel):
    """模型配置文件的顶层结构，负责校验多个模型之间的约束。"""

    models: list[ModelConfiguration]

    @model_validator(mode="after")
    def validate_unique_names_and_credentials(self) -> Self:
        """保证逻辑名唯一，并要求每个模型使用独立的密钥环境变量名。"""
        names = [model.logical_name for model in self.models]
        # 密钥变量名按不区分大小写的方式判重，避免跨部署系统时发生命名冲突。
        variables = [model.api_key_env.casefold() for model in self.models]
        if len(names) != len(set(names)):
            raise ValueError("Logical model names must be unique")
        if len(variables) != len(set(variables)):
            raise ValueError("Each model must use a different API key environment variable")
        return self


class LogicalModelInfo(BaseModel):
    """对外公开的模型信息，不包含真实模型名、供应商地址或密钥变量名。"""

    # 使用逻辑模型名作为公开标识，与聊天请求中的 model 字段对应。
    id: str
    # 固定对象类型，便于调用方识别模型条目。
    object: Literal["model"] = "model"
    capabilities: ModelCapabilities


class LogicalModelList(BaseModel):
    """GET /v1/models 的响应结构，统一封装逻辑模型列表。"""

    object: Literal["list"] = "list"
    data: list[LogicalModelInfo]
