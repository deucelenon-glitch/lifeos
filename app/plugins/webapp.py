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
            """Server-composed informative dashboard for the home screen."""
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                # Habits
                habit_count = conn.execute("SELECT COUNT(*) c FROM habits").fetchone()["c"]
                best_habit = conn.execute("""
                    SELECT h.name, COUNT(DISTINCT hl.completed_date) streak
                    FROM habits h LEFT JOIN habit_logs hl ON hl.habit_id = h.id
                    GROUP BY h.id ORDER BY streak DESC LIMIT 1
                """).fetchone()

                # Burn & Income
                burn = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE strftime('%Y-%m', expense_date) = strftime('%Y-%m', 'now')"
                ).fetchone()[0] or 0
                income = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM income_entries WHERE strftime('%Y-%m', income_date) = strftime('%Y-%m', 'now')"
                ).fetchone()[0] or 0
                expense_count = conn.execute("SELECT COUNT(*) c FROM expenses").fetchone()["c"]

                # Money config + budget = Planner expense plans (single source of truth)
                mcfg = conn.execute("SELECT * FROM money_config ORDER BY id DESC LIMIT 1").fetchone()
                try:
                    from app.plugins.planner import PlannerPlugin
                    budget = PlannerPlugin().monthly_plan_cost()
                except Exception:
                    budget = 0.0
                if not budget or budget <= 0:
                    budget = conn.execute("SELECT COALESCE(SUM(amount),0) FROM monthly_budget_items").fetchone()[0] or 0
                rent_row = None
                try:
                    rent_row = conn.execute(
                        "SELECT target_name, cost_per, target_quantity FROM plans WHERE target_type = 'expense' AND lower(target_name) = 'rent' LIMIT 1"
                    ).fetchone()
                except Exception:
                    rent_row = None
                if not rent_row:  # fallback: first legacy item with a due day
                    rent_row = conn.execute("SELECT amount, due_day FROM monthly_budget_items WHERE due_day IS NOT NULL ORDER BY id ASC LIMIT 1").fetchone()
                if rent_row and "cost_per" in rent_row.keys():
                    rent = float(rent_row["cost_per"] or 0) or float(rent_row["target_quantity"] or 0)
                    rent_due_day = 30
                else:
                    rent = float(rent_row["amount"] or 0) if rent_row else 0
                    rent_due_day = int(rent_row["due_day"] or 30) if rent_row else 30

                # Plans
                plan_count = conn.execute("SELECT COUNT(*) c FROM plans").fetchone()["c"]

                # P2P
                p2p = conn.execute("SELECT * FROM p2p_config ORDER BY id DESC LIMIT 1").fetchone()
                last_order = conn.execute("SELECT * FROM p2p_orders ORDER BY id DESC LIMIT 1").fetchone()
                p2p_on = bool(p2p and p2p["enabled"])
                last_profit = last_order['profit_usd'] if last_order else 0.0
                p2p_sub = f"collector active (${last_profit:.2f} last profit)" if p2p_on else "collector OFF"

                # Telegram
                tg = conn.execute("SELECT COUNT(*) c FROM telegram_config").fetchone()["c"]

                # Category breakdown
                cat_rows = conn.execute("""
                    SELECT category, COALESCE(SUM(amount),0) total, COUNT(*) n
                    FROM expenses WHERE strftime('%Y-%m', expense_date) = strftime('%Y-%m', 'now')
                    GROUP BY category ORDER BY total DESC
                """).fetchall()

                # Capital accounts
                acc_rows = conn.execute("SELECT balance, currency, label FROM capital_accounts").fetchall()
                cap_eur = sum((r["balance"] or 0) for r in acc_rows if r["currency"] == "EUR")
                cap_usd = sum((r["balance"] or 0) for r in acc_rows if r["currency"] == "USD")
                try:
                    from app.plugins.capital import _fx_eur_to_usd
                    eur2usd = _fx_eur_to_usd()
                except Exception:
                    eur2usd = 1.138
                cap_total_eur = cap_eur + (cap_usd / eur2usd if eur2usd else cap_usd)

            # Calculations
            today_d = datetime.now().day
            days_to_rent = max(0, rent_due_day - today_d)
            net_pnl = income - burn
            budget_pct = int((burn / budget) * 100) if budget > 0 else 0

            cat_chips = "".join(
                f"<div class='bg-dark-900 border border-dark-800 rounded-xl px-3 py-2 flex justify-between items-center'>"
                f"<span class='text-xs text-slate-300 font-mono uppercase'>{r['category']}</span>"
                f"<span class='text-xs font-mono text-white'>€{r['total']:.2f} <span class='text-slate-500'>({int((r['total']/budget)*100) if budget else 0}%)</span></span></div>"
                for r in cat_rows
            ) or "<div class='text-xs text-slate-500 py-2'>No expenses logged this month yet.</div>"

            best_str = f"{best_habit['name']} · {best_habit['streak']}d streak" if best_habit and best_habit['streak'] else "No active streaks"

            accounts_html = "".join(
                f"<div class='bg-dark-900 border border-dark-800 rounded-xl p-2.5 flex justify-between items-center'>"
                f"<span class='text-xs text-slate-300 font-medium'>{a['label']}</span>"
                f"<span class='text-xs font-mono text-emerald-400'>{('€' if a['currency']=='EUR' else '$')}{a['balance']:,.1f}</span></div>"
                for a in acc_rows
            ) or "<div class='text-xs text-slate-500 py-2'>No capital accounts configured.</div>"

            return f"""
            <div id='dash-cards' class='space-y-4'>
                <!-- Status Bar -->
                <div class='bg-dark-900 border border-dark-800 rounded-2xl px-4 py-3 flex flex-wrap items-center justify-between gap-3 text-xs'>
                    <div class='flex items-center gap-2'>
                        <span class='w-2 h-2 rounded-full {'bg-emerald-500 animate-pulse' if p2p_on else 'bg-amber-500'}'></span>
                        <span class='text-slate-300 font-medium'>P2P: {p2p_sub}</span>
                    </div>
                    <div class='flex items-center gap-3 font-mono text-slate-400'>
                        <span>🤖 Bot: {'Linked' if tg else 'Unlinked'}</span>
                        <span>•</span>
                        <span>🗓️ Rent in {days_to_rent}d (€{rent:.0f})</span>
                    </div>
                </div>

                <!-- Row 1: Core Financial & Activity Cards -->
                <div class='grid grid-cols-2 md:grid-cols-4 gap-3'>
                    <div class='bg-dark-800/50 border border-dark-700 rounded-2xl p-4 flex flex-col gap-1'>
                        <span class='text-[10px] uppercase font-mono text-slate-400'>💶 Total Net Worth</span>
                        <span class='text-2xl font-extrabold text-emerald-400'>€{cap_total_eur:,.0f}</span>
                        <span class='text-[11px] text-slate-500'>€{cap_eur:.0f} EUR + ${cap_usd:.0f} USD</span>
                    </div>
                    <div class='bg-dark-800/50 border border-dark-700 rounded-2xl p-4 flex flex-col gap-1'>
                        <span class='text-[10px] uppercase font-mono text-slate-400'>📈 Month P&L</span>
                        <span class='text-2xl font-extrabold { "text-emerald-400" if net_pnl >= 0 else "text-rose-400" }'>€{net_pnl:+.2f}</span>
                        <span class='text-[11px] text-slate-500'>In: €{income:.2f} | Out: €{burn:.2f}</span>
                    </div>
                    <div class='bg-dark-800/50 border border-dark-700 rounded-2xl p-4 flex flex-col gap-1'>
                        <span class='text-[10px] uppercase font-mono text-slate-400'>🎯 Budget Burn</span>
                        <span class='text-2xl font-extrabold text-white'>€{burn:.2f} <span class='text-xs font-normal text-slate-400'>/ €{budget}</span></span>
                        <div class='w-full bg-dark-900 rounded-full h-1.5 mt-1 overflow-hidden'>
                            <div class='bg-emerald-500 h-full rounded-full' style='width: {min(100, budget_pct)}%'></div>
                        </div>
                    </div>
                    <div class='bg-dark-800/50 border border-dark-700 rounded-2xl p-4 flex flex-col gap-1'>
                        <span class='text-[10px] uppercase font-mono text-slate-400'>🔥 Best Streak</span>
                        <span class='text-xl font-bold text-white truncate'>{best_str}</span>
                        <span class='text-[11px] text-slate-500'>{habit_count} habits tracked</span>
                    </div>
                </div>

                <!-- Row 2: Spend Breakdown & Capital Accounts Preview -->
                <div class='grid grid-cols-1 md:grid-cols-2 gap-3'>
                    <div class='bg-dark-800/50 border border-dark-700 rounded-2xl p-4 space-y-2'>
                        <div class='flex justify-between items-center'>
                            <span class='text-[10px] uppercase font-mono text-slate-400'>🧩 Spend by Category (This Month)</span>
                            <a href='#' @click.prevent="refreshTab('expenses')" class='text-[11px] text-emerald-400 hover:underline'>View all →</a>
                        </div>
                        <div class='space-y-1.5 mt-1'>
                            {cat_chips}
                        </div>
                    </div>
                    <div class='bg-dark-800/50 border border-dark-700 rounded-2xl p-4 space-y-2'>
                        <div class='flex justify-between items-center'>
                            <span class='text-[10px] uppercase font-mono text-slate-400'>🏦 Capital Accounts</span>
                            <a href='#' @click.prevent="refreshTab('capital')" class='text-[11px] text-emerald-400 hover:underline'>Manage →</a>
                        </div>
                        <div class='grid grid-cols-2 gap-2 mt-1'>
                            {accounts_html}
                        </div>
                    </div>
                </div>
            </div>
            """

        @router.get("/", response_class=HTMLResponse)
        def serve_dashboard(request: Request):
            entries = []
            app = request.app
            plugins = getattr(app.state, "plugins", {})
            for name in ["habits", "expenses", "planner", "p2p", "money", "capital", "telegram"]:
                p = plugins.get(name)
                if p is None:
                    continue
                m = p.menu()
                for entry in (m if isinstance(m, list) else [m]):
                    entry["id"] = entry.get("id", name)
                    entries.append(entry)
            for name, p in plugins.items():
                if name in {"habits", "expenses", "planner", "p2p", "money", "capital", "telegram", "webapp"}:
                    continue
                m = p.menu()
                for entry in (m if isinstance(m, list) else [m]):
                    entry["id"] = entry.get("id", name)
                    entries.append(entry)
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                habits = conn.execute("SELECT id, name FROM habits ORDER BY name").fetchall()
            return templates.TemplateResponse(
                request,
                "index.html",
                {"menu_entries": entries, "habits": habits},
            )

        return router

    def get_dashboard_widgets(self) -> list:
        return []
