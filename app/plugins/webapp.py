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
            # Dynamically collect menu entries from every loaded plugin.
            # New plugins auto-appear in the drawer, below existing ones.
            entries = []
            app = request.app
            plugins = getattr(app.state, "plugins", {})
            for name in ["habits", "expenses", "planner", "p2p", "telegram"]:
                p = plugins.get(name)
                if p is None:
                    continue
                m = p.menu()
                for entry in (m if isinstance(m, list) else [m]):
                    entry["id"] = entry.get("id", name)
                    entries.append(entry)
            # Any extra plugins not in the preferred order get appended after
            for name, p in plugins.items():
                if name in {"habits", "expenses", "planner", "p2p", "telegram", "webapp"}:
                    continue
                m = p.menu()
                for entry in (m if isinstance(m, list) else [m]):
                    entry["id"] = entry.get("id", name)
                    entries.append(entry)
            return templates.TemplateResponse(
                request,
                "index.html",
                {"menu_entries": entries},
            )

        return router

    def get_dashboard_widgets(self) -> list:
        return []