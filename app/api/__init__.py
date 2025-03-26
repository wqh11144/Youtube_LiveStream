from fastapi import APIRouter
from app.api import tasks, video, config

api_router = APIRouter()

api_router.include_router(tasks.router)
api_router.include_router(video.router)
api_router.include_router(config.router)
