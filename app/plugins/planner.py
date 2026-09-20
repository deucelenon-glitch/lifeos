from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from app.plugins.base import LifeOSPlugin
import sqlite3

class PlannerPlugin(LifeOSPlugin):
    @property
    def name(self) -> str:
        return "planner"

    def init_tables(self, conn: sqlite3.Connection):
        # Plan items: for each trackable target, a weekly/periodic goal
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_type TEXT NOT NULL,      -- 'habit' | 'expense' | 'p2p'
                target_id INTEGER,              -- FK to the tracked row (habit id etc)
                target_name TEXT,               -- category name for expense-type goals
                goal_label TEXT NOT NULL,       -- e.g. "Gym x3/week"
                frequency TEXT DEFAULT 'weekly',-- weekly | daily | monthly
                target_quantity REAL,           -- goal number (3 sessions, 100€, 5 orders)
                period_start DATE,
                period_end DATE,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(plans)").fetchall()]
        if "target_name" not in cols:
            conn.execute("ALTER TABLE plans ADD COLUMN target_name TEXT")

        from app.categories import init_categories_table
        init_categories_table(conn)

    def register_routes(self) -> APIRouter:
        router = APIRouter()

        @router.get("/view", response_class=HTMLResponse)
        def planner_view(request: Request):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                plans = conn.execute("SELECT * FROM plans ORDER BY id DESC").fetchall()
                habits = conn.execute("SELECT id, name FROM habits ORDER BY name").fetchall()
                # Get distinct categories from expenses
                categories = [r[0] for r in conn.execute("SELECT DISTINCT category FROM expenses WHERE category IS NOT NULL AND category != '' ORDER BY category").fetchall()]

            if not plans:
                empty_html = """
                <div class='text-slate-500 py-10 text-center text-sm'>
                    No plans yet — set your first goal below.<br>
                    Plans turn trackers into targets: "Gym 3x/week", "Spend ≤100€/week", "5 P2P orders/week".
                </div>
                """
            else:
                empty_html = ""

            html = "<div class='space-y-3'>"
            for p in plans:
                freq = p['frequency']
                target = p['target_quantity']
                progress = self._progress_for(global_db, p)
                pct = min(int((progress / target) * 100), 100) if target else 0
                
                # Color coding based on target type and direction
                if p['target_type'] == 'expense':
                    # For expenses, lower is better (budget ceiling)
                    color = "text-emerald-400" if progress <= target else "text-red-400"
                    prog_display = f"€{progress:.2f} / €{target:.2f}"
                else:
                    color = "text-emerald-400" if pct >= 100 else ("text-amber-400" if pct >= 50 else "text-slate-400")
                    prog_display = f"{progress}/{int(target)}"

                html += f"""
                <div class='bg-dark-900 border border-dark-700 rounded-xl p-4'>
                    <div class='flex justify-between items-start'>
                        <div>
                            <h4 class='font-semibold text-white text-sm'>{p['goal_label']}</h4>
                            <p class='text-xs text-slate-400 mt-0.5'><span class='uppercase font-mono'>{p['target_type']}</span> • {p['frequency']}</p>
                        </div>
                        <button hx-delete='/api/planner/{p['id']}' hx-target='#planner-list' class='text-slate-500 hover:text-red-400 p-1'>✕</button>
                    </div>
                    <div class='flex items-center mt-3'>
                        <div class='flex-1 h-1.5 bg-dark-700 rounded-full overflow-hidden'>
                            <div class='bg-emerald-500 h-full rounded-full' style='width: {pct}%'></div>
                        </div>
                        <span class='ml-3 text-xs font-mono {color}'>{prog_display}</span>
                    </div>
                </div>
                """
            html += "</div>"
            return empty_html + html

        @router.get("/list", response_class=HTMLResponse)
        def planner_list(request: Request):
            return planner_view(request)

        @router.post("/", response_class=HTMLResponse)
        def create_plan(
            request: Request,
            goal_label: str = Form(...),
            target_type: str = Form("habit"),
            target_id: int = Form(0),
            target_name: str = Form(""),
            frequency: str = Form("weekly"),
            target_quantity: float = Form(1),
        ):
            from app.database import db as global_db
            if target_type == "expense" and target_name:
                with global_db.get_connection() as conn:
                    from app.categories import ensure_category
                    target_name = ensure_category(conn, "expense", target_name)
            with global_db.get_connection() as conn:
                conn.execute(
                    "INSERT INTO plans (target_type, target_id, target_name, goal_label, frequency, target_quantity) VALUES (?, ?, ?, ?, ?, ?)",
                    (target_type, target_id, target_name, goal_label, frequency, target_quantity),
                )
            return planner_view(request)

        @router.delete("/{plan_id}", response_class=HTMLResponse)
        def delete_plan(request: Request, plan_id: int):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
            return planner_view(request)

        # JSON API for programmatic queries / CLI / future widgets
        @router.get("/api")
        def plans_api():
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                rows = conn.execute("SELECT * FROM plans ORDER BY id DESC").fetchall()
                out = []
                for r in rows:
                    d = dict(r)
                    d["progress"] = self._progress_for(global_db, r)
                    out.append(d)
            return out

        return router

    def _progress_for(self, db, plan) -> float:
        """Count actual completions or sums within the current period for the plan target."""
        with db.get_connection() as conn:
            target_type = plan["target_type"]
            target_id = plan["target_id"]
            frequency = plan["frequency"]

            # Determine date filter based on frequency
            if frequency == "daily":
                date_clause = "date = date('now')"
                date_filter_sqlite = "date(completed_date) = date('now')"
                expense_filter_sqlite = "date(expense_date) = date('now')"
                p2p_filter_sqlite = "date(created_at) = date('now')"
            elif frequency == "monthly":
                date_filter_sqlite = "strftime('%Y-%m', completed_date) = strftime('%Y-%m', 'now')"
                expense_filter_sqlite = "strftime('%Y-%m', expense_date) = strftime('%Y-%m', 'now')"
                p2p_filter_sqlite = "strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')"
            else:  # weekly (default)
                # Current week: SQLite datetime('now', 'weekday 0', '-6 days') or similar, or simply last 7 days
                date_filter_sqlite = "completed_date >= date('now', '-7 days')"
                expense_filter_sqlite = "expense_date >= date('now', '-7 days')"
                p2p_filter_sqlite = "created_at >= date('now', '-7 days')"

            if target_type == "habit" and target_id:
                row = conn.execute(
                    f"SELECT COUNT(DISTINCT completed_date) FROM habit_logs WHERE habit_id = ? AND {date_filter_sqlite}",
                    (target_id,),
                ).fetchone()
                return float(row[0] or 0)

            elif target_type == "expense":
                # Budget goal: sum expenses in period, optionally filtered to a category.
                cat = (plan.get("target_name") if hasattr(plan, "get") else plan["target_name"])
                if cat and plan["target_name"]:
                    row = conn.execute(
                        f"SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE category = ? AND {expense_filter_sqlite}",
                        (plan["target_name"],),
                    ).fetchone()
                else:
                    # No category: total spend in period.
                    row = conn.execute(
                        f"SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE {expense_filter_sqlite}"
                    ).fetchone()
                return float(row[0] or 0.0)

            elif target_type == "p2p":
                row = conn.execute(
                    f"SELECT COUNT(*) FROM p2p_orders WHERE {p2p_filter_sqlite}"
                ).fetchone()
                return float(row[0] or 0)
        return 0.0

    def menu(self) -> dict:
        return {"id": self.name, "icon": "🗓️", "label": "Planner & Goals", "badge": "", "view": "/api/planner/view"}

    def bot_commands(self) -> dict:
        def cmd_plan(chat_id, parts):
            from app.database import Database
            db = Database()
            with db.get_connection() as conn:
                rows = conn.execute("SELECT * FROM plans ORDER BY id DESC").fetchall()
            if not rows:
                return "No plans. Add one in the webapp: Planner tab."
            out = ["*Plans:*"]
            for r in rows:
                prog = self._progress_for(db, r)
                out.append(f"📋 {r['goal_label']} — {prog}/{int(r['target_quantity'])} ({r['frequency']})")
            return "\n".join(out)

        def cmd_compare(chat_id, parts):
            return self._compare_text()

        return {"plan": cmd_plan, "plans": cmd_plan, "compare": cmd_compare}

    def _compare_text(self) -> str:
        """Tracked vs planned — text summary for bot/CLI."""
        from app.database import Database
        db = Database()
        with db.get_connection() as conn:
            plans = conn.execute("SELECT * FROM plans ORDER BY id DESC").fetchall()
        if not plans:
            return "No plans to compare. Add goals in Planner → view shows plan vs actual."
        out = ["*Plan vs Actual:*"]
        for p in plans:
            prog = self._progress_for(db, p)
            target = p["target_quantity"]
            status = "✅" if prog >= target else ("🟡" if prog >= target * 0.5 else "🔴")
            out.append(f"{status} {p['goal_label']}: {prog}/{int(target)} ({p['frequency']})")
        return "\n".join(out)

    def get_dashboard_widgets(self) -> list:
        return []