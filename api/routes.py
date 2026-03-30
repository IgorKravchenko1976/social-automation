"""API route aggregator.

Sub-modules:
- api.routes_debug  — debug & diagnostic endpoints (public)
- api.routes_blog   — blog, media, chat (public)
- api.routes_admin  — admin CRUD, stats, emergency delete (auth required)
- api.schemas       — Pydantic request/response models
"""
from fastapi import APIRouter

from api.routes_debug import debug_router
from api.routes_blog import blog_router
from api.routes_admin import admin_router

# Combined public router (no extra prefix — sub-routers already have /api)
public_router = APIRouter()
public_router.include_router(debug_router)
public_router.include_router(blog_router)

# Admin router (re-export directly)
router = admin_router
