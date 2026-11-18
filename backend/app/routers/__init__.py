from fastapi import APIRouter

from app.routers import core, seal

api = APIRouter()
api.include_router(core.router)
api.include_router(seal.router)
