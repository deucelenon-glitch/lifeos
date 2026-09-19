from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from app.config import settings
from app.plugins.base import LifeOSPlugin

class WebAppPlugin(LifeOSPlugin):
    @property
    def name(self) -> str:
        return "webapp"

    def init_tables(self, conn):
        pass

    def register_routes(self) -> APIRouter:
        router = APIRouter()
        templates_dir = settings.BASE_DIR / "app" / "templates"
        templates = Jinja2Templates(directory=str(templates_dir))

        @router.get("/", response_class=HTMLResponse)
        def serve_dashboard(request: Request):
            return templates.TemplateResponse(request, "index.html")

        return router

    def get_dashboard_widgets(self) -> list:
        return []
