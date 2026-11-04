"""提供逻辑模型查询接口，仅公开模型名称与能力，不暴露内部供应商配置。"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import get_model_registry
from app.models.registry import ModelRegistry
from app.schemas.models import LogicalModelList

# 统一添加版本前缀，并在自动生成的 API 文档中归入 models 分组。
router = APIRouter(prefix="/v1", tags=["models"])


@router.get("/models", response_model=LogicalModelList)
def list_models(models: Annotated[ModelRegistry, Depends(get_model_registry)]) -> LogicalModelList:
    """通过依赖注入获取应用中的模型注册表，返回调用方可选择的逻辑模型列表。"""
    # 注册表负责筛选公开字段；响应模型统一封装列表结构。
    return LogicalModelList(data=models.list_models())
