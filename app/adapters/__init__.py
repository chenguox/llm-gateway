"""Model adapter contracts and provider implementations."""

from app.adapters.base import ModelAdapter
from app.adapters.deepseek import DeepSeekChatAdapter

__all__ = ["DeepSeekChatAdapter", "ModelAdapter"]
