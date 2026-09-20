from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from app.plugins.base import LifeOSPlugin
import sqlite3

class HabitsPlugin(LifeOSPlugin):
    @property
    def name(self) -> str:
        return "habits"

    def init_tables(self, conn: sqlite3.Connection):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS habits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                target_streak INTEGER DEFAULT 7,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Budget-link columns: how many times/week + cost per occurrence
        cols = [c[1] for c in conn.execute("PRAGMA table_info(habits)").fetchall()]
        if "weekly_target" not in cols:
            conn.execute("ALTER TABLE habits ADD COLUMN weekly_target INTEGER DEFAULT 0")
        if "cost_per" not in cols:
            conn.execute("ALTER TABLE habits ADD COLUMN cost_per REAL DEFAULT 0")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS habit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                habit_id INTEGER,
                completed_date TEXT NOT NULL DEFAULT (date('now')),
                FOREIGN KEY (habit_id) REFERENCES habits (id) ON DELETE CASCADE
            )
        """)
        # Dedupe: one check-in per habit per day
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_habit_log_unique ON habit_logs (habit_id, completed_date)")

    # ------------------------------------------------------------------
    # shared helpers
    # ------------------------------------------------------------------
    def monthly_plan_cost(self) -> float:
        """Sum of habit-driven monthly spend: Σ (weekly_target × cost_per × 52/12).
        Callable from other plugins (Money uses it for the budget check)."""
        from app.database import db as global_db
        try:
            with global_db.get_connection() as conn:
                rows = conn.execute("SELECT weekly_target, cost_per FROM habits").fetchall()
        except Exception:
            return 0.0
        total = 0.0
        for h in rows:
            if (h["weekly_target"] or 0) and (h["cost_per"] or 0):
                total += float(h["weekly_target"]) * float(h["cost_per"]) * 52 / 12
        return round(total, 2)

    def register_routes(self) -> APIRouter:
        router = APIRouter()

        @router.get("/")
        def list_habits_api(request: Request):
            db = request.app.state.db if hasattr(request.app.state, "db") else None
            # fallback to direct sqlite connection via app config or database module
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                habits = conn.execute("SELECT * FROM habits ORDER BY id DESC").fetchall()
            return {"plugin": self.name, "count": len(habits)}

        @router.get("/list", response_class=HTMLResponse)
        def habits_list_html(request: Request):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                habits = conn.execute("""
                    SELECT h.*, 
                    (SELECT COUNT(DISTINCT completed_date) FROM habit_logs WHERE habit_id = h.id) as streak
                    FROM habits h ORDER BY h.id DESC
                """).fetchall()
            
            if not habits:
                return "<div class='text-slate-500 py-8 text-center'>No habits tracked yet. Add one above!</div>"

            html = "<div class='space-y-3'>"
            for h in habits:
                streak = h["streak"] or 0
                target = h["target_streak"]
                pct = min(int((streak / target) * 100), 100) if target > 0 else 0
                plan_line = ""
                if (h["weekly_target"] or 0) and (h["cost_per"] or 0):
                    monthly = round(float(h["weekly_target"]) * float(h["cost_per"]) * 52 / 12, 2)
                    plan_line = (f"<p class='text-xs text-slate-500 mt-0.5'>Plan: "
                                 f"{h['weekly_target']}×/wk · €{h['cost_per']:.2f} each → <span class='text-emerald-400 font-mono font-bold'>€{monthly:.2f}/mo</span></p>")
                html += f"""
                <div class='bg-dark-900 border border-dark-700 rounded-xl p-4 flex items-center justify-between'>
                    <div>
                        <h4 class='font-semibold text-white'>{h['name']}</h4>
                        <p class='text-xs text-slate-400 mt-0.5'>Streak: <span class='text-emerald-400 font-mono font-bold'>{streak}</span> / {target} days</p>
                        {plan_line}
                        <div class='w-32 bg-dark-700 h-1.5 rounded-full mt-2 overflow-hidden'>
                            <div class='bg-emerald-500 h-full rounded-full' style='width: {pct}%'></div>
                        </div>
                    </div>
                    <div class='flex items-center space-x-2'>
                        <details class='relative'>
                            <summary class='text-slate-500 hover:text-emerald-400 text-xs cursor-pointer px-2 py-1.5 rounded-lg border border-dark-700 transition'>⚙️ plan</summary>
                            <form hx-post='/api/habits/{h['id']}/plan' hx-target='#habits-list'
                                  class='absolute right-0 top-full mt-1 z-40 bg-dark-900 border border-dark-700 rounded-xl p-3 space-y-1.5 w-56 shadow-lg'>
                                <div class='flex items-center gap-2'>
                                    <label class='text-[10px] uppercase font-mono text-slate-400 w-16'>×/week</label>
                                    <input type='number' name='weekly_target' value='{h['weekly_target'] or 0}' min='0' max='28'
                                           class='w-20 bg-dark-950 border border-dark-800 rounded-lg px-2 py-1.5 text-white text-sm font-mono'>
                                </div>
                                <div class='flex items-center gap-2'>
                                    <label class='text-[10px] uppercase font-mono text-slate-400 w-16'>€ each</label>
                                    <input type='number' step='0.01' name='cost_per' value='{h['cost_per'] or 0:.2f}' min='0'
                                           class='w-20 bg-dark-950 border border-dark-800 rounded-lg px-2 py-1.5 text-white text-sm font-mono'>
                                </div>
                                <button type='submit' class='w-full bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium py-1.5 rounded-lg'>Save plan</button>
                            </form>
                        </details>
                        <button hx-post='/api/habits/{h['id']}/check' hx-target='#habits-list' class='bg-emerald-600/20 hover:bg-emerald-600 text-emerald-400 hover:text-white px-3 py-1.5 rounded-lg text-xs font-medium transition border border-emerald-500/30'>Check Today</button>
                        <button hx-delete='/api/habits/{h['id']}' hx-target='#habits-list' class='text-slate-500 hover:text-red-400 p-1.5 transition'>✕</button>
                    </div>
                </div>
                """
            html += "</div>"
            return html

        @router.get("/widget", response_class=HTMLResponse)
        def habits_widget():
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                habits = conn.execute("""
                    SELECT h.*, 
                    (SELECT COUNT(DISTINCT completed_date) FROM habit_logs WHERE habit_id = h.id) as streak
                    FROM habits h ORDER BY h.id DESC LIMIT 5
                """).fetchall()
            if not habits:
                return "<div class='text-slate-500 py-6 text-center'>No habits yet — add one in the Habits tab.</div>"
            html = "<div class='space-y-2 text-sm'>"
            for h in habits:
                html += f"""<div class='flex justify-between items-center'>
                    <span class='text-slate-200'>{h['name']}</span>
                    <span class='text-emerald-400 font-mono'>🔥 {h['streak'] or 0}d</span></div>"""
            html += "</div>"
            return html

        @router.post("/", response_class=HTMLResponse)
        def create_habit(request: Request, name: str = Form(...), target_streak: int = Form(7),
                         weekly_target: int = Form(0), cost_per: float = Form(0)):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                conn.execute(
                    "INSERT INTO habits (name, target_streak, weekly_target, cost_per) VALUES (?, ?, ?, ?)",
                    (name, target_streak, weekly_target, cost_per),
                )
            return habits_list_html(request)

        @router.post("/{habit_id}/plan", response_class=HTMLResponse)
        def set_habit_plan(request: Request, habit_id: int, weekly_target: int = Form(0), cost_per: float = Form(0)):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                conn.execute(
                    "UPDATE habits SET weekly_target = ?, cost_per = ? WHERE id = ?",
                    (weekly_target, cost_per, habit_id),
                )
            return habits_list_html(request)

        @router.get("/plan-cost")
        def habit_plan_cost():
            """Sum of habit-driven monthly spend: Σ (weekly_target × cost_per × 52/12)."""
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                rows = conn.execute("SELECT weekly_target, cost_per FROM habits").fetchall()
            total = 0.0
            count = 0
            for h in rows:
                if (h["weekly_target"] or 0) and (h["cost_per"] or 0):
                    total += float(h["weekly_target"]) * float(h["cost_per"]) * 52 / 12
                    count += 1
            return {"monthly": round(total, 2), "costed_habits": count}

        @router.post("/check", response_class=HTMLResponse)
        def check_habit_form(request: Request, habit_id: int = Form(...), habit_date: str = Form("")):
            """Form-based check (used by Planner hub) — habit_id comes from the form."""
            from app.database import db as global_db
            habit_date = (habit_date or "").strip()
            with global_db.get_connection() as conn:
                if habit_date:
                    conn.execute(
                        "INSERT OR IGNORE INTO habit_logs (habit_id, completed_date) VALUES (?, ?)",
                        (habit_id, habit_date),
                    )
                else:
                    conn.execute(
                        "INSERT OR IGNORE INTO habit_logs (habit_id, completed_date) VALUES (?, date('now'))",
                        (habit_id,),
                    )
            return habits_list_html(request)

        @router.post("/{habit_id}/check", response_class=HTMLResponse)
        def check_habit(request: Request, habit_id: int, habit_date: str = Form("")):
            from app.database import db as global_db
            habit_date = (habit_date or "").strip()
            with global_db.get_connection() as conn:
                # Insert check (defaults to today, or explicit date from planner)
                if habit_date:
                    conn.execute(
                        "INSERT OR IGNORE INTO habit_logs (habit_id, completed_date) VALUES (?, ?)",
                        (habit_id, habit_date),
                    )
                else:
                    conn.execute(
                        "INSERT OR IGNORE INTO habit_logs (habit_id, completed_date) VALUES (?, date('now'))",
                        (habit_id,),
                    )
            return habits_list_html(request)

        @router.delete("/{habit_id}", response_class=HTMLResponse)
        def delete_habit(request: Request, habit_id: int):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                conn.execute("DELETE FROM habits WHERE id = ?", (habit_id,))
                conn.execute("DELETE FROM habit_logs WHERE habit_id = ?", (habit_id,))
                # Keep Planner in sync: remove any plan pointing at this habit
                # so the deleted habit doesn't linger as a dead card in the Planner.
                conn.execute(
                    "DELETE FROM plans WHERE target_type = 'habit' AND target_id = ?",
                    (habit_id,),
                )
            return habits_list_html(request)

        return router

    def get_dashboard_widgets(self) -> list:
        return []

    def menu(self) -> dict:
        return {"id": self.name, "icon": "🔥", "label": "Habits & Streaks", "badge": "", "view": "/api/habits/view"}
