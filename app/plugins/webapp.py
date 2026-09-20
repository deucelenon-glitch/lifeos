from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from app.config import settings
from app.plugins.base import LifeOSPlugin
from datetime import datetime

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

        @router.get("/api/dashboard", response_class=HTMLResponse)
        def dashboard(request: Request):
            """Server-composed stat cards for the home screen — no JS needed."""
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                # Habits
                habit_count = conn.execute("SELECT COUNT(*) c FROM habits").fetchone()["c"]
                streak_total = conn.execute(
                    "SELECT COALESCE(SUM(s), 0) FROM (SELECT COUNT(DISTINCT completed_date) s FROM habit_logs GROUP BY habit_id)"
                ).fetchone()[0] or 0
                best_habit = conn.execute("""
                    SELECT h.name, COUNT(DISTINCT hl.completed_date) streak
                    FROM habits h LEFT JOIN habit_logs hl ON hl.habit_id = h.id
                    GROUP BY h.id ORDER BY streak DESC LIMIT 1
                """).fetchone()

                # Expenses this month
                burn = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE strftime('%Y-%m', expense_date) = strftime('%Y-%m', 'now')"
                ).fetchone()[0] or 0
                expense_count = conn.execute("SELECT COUNT(*) c FROM expenses").fetchone()["c"]

                # Plans
                plan_count = conn.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"]

                # P2P
                p2p = conn.execute("SELECT * FROM p2p_config ORDER BY id DESC LIMIT 1").fetchone()
                last_order = conn.execute("SELECT * FROM p2p_orders ORDER BY id DESC LIMIT 1").fetchone()
                if p2p and last_order:
                    try:
                        created = datetime.fromisoformat(str(last_order["created_at"]).replace("Z", ""))
                        mins_ago = int((datetime.now() - created).total_seconds() // 60)
                        p2p_sub = f"{mins_ago}m ago"
                    except (ValueError, TypeError):
                        p2p_sub = "logged"
                elif p2p:
                    p2p_sub = "no orders yet"
                else:
                    p2p_sub = "—"
                p2p_on = bool(p2p and p2p["enabled"])

                # Telegram
                tg = conn.execute("SELECT COUNT(*) c FROM telegram_config").fetchone()["c"]

            card = lambda title, value, sub, accent="text-emerald-400", tag="" : f"""
            <div class='bg-dark-800/50 border border-dark-700 rounded-2xl p-4 flex flex-col gap-1'>
                <span class='text-[10px] uppercase font-mono text-slate-400'>{title} {tag}</span>
                <span class='text-2xl font-extrabold {accent}'>{value}</span>
                <span class='text-[11px] text-slate-500'>{sub}</span>
            </div>"""

            best = f"{best_habit['name']} · {best_habit['streak']}d" if best_habit and best_habit["streak"] else "track one below"
            return card("🔥 Habits", str(habit_count), best, "text-white") + card(
                "💰 Burn (month)", f"€{burn:.2f}", f"{expense_count} logged", "text-white"
            ) + card(
                "🗓️ Plans", str(plan_count), "goals active", "text-white"
            ) + card(
                "🛰️ P2P", "ON" if p2p_on else "OFF", p2p_sub, "text-emerald-400" if p2p_on else "text-slate-400"
            ) + card(
                "🤖 Bot", "Linked" if tg else "Unlinked", "telegram config", "text-emerald-400" if tg else "text-slate-400"
            )

        @router.get("/", response_class=HTMLResponse)
        def serve_dashboard(request: Request):
            # Dynamically collect menu entries from every loaded plugin.
            # New plugins auto-appear in the drawer, below existing ones.
            entries = []
            app = request.app
            plugins = getattr(app.state, "plugins", {})
            for name in ["habits", "expenses", "planner", "p2p", "money", "telegram"]:
                p = plugins.get(name)
                if p is None:
                    continue
                m = p.menu()
                for entry in (m if isinstance(m, list) else [m]):
                    entry["id"] = entry.get("id", name)
                    entries.append(entry)
            # Any extra plugins not in the preferred order get appended after
            for name, p in plugins.items():
                if name in {"habits", "expenses", "planner", "p2p", "money", "telegram", "webapp"}:
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