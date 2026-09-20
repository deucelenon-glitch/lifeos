"""Shared categories plugin: exposes GET /api/categories?domain=expense
so every tab's picker stays in sync with the seeded + user-added options.
"""
from fastapi import APIRouter, Request
from app.plugins.base import LifeOSPlugin
from app.categories import list_categories, init_categories_table
import sqlite3


class CategoriesPlugin(LifeOSPlugin):
    @property
    def name(self) -> str:
        return "categories"

    def init_tables(self, conn: sqlite3.Connection):
        init_categories_table(conn)

    def register_routes(self) -> APIRouter:
        router = APIRouter()

        @router.get("/")
        @router.get("")
        def get_categories(request: Request, domain: str = "expense"):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                names = list_categories(conn, domain)
            return {"domain": domain, "categories": names}

        return router

    def get_dashboard_widgets(self) -> list:
        return []