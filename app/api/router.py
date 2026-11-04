"""Collect public API routes in one place."""

from fastapi import APIRouter

from app.api.chat import router as chat_router
from app.api.health import router as health_router
from app.api.prompts import router as prompts_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(chat_router)
api_router.include_router(prompts_router)
