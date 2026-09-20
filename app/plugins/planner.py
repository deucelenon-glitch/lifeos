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
                categories = [r[0] for r in conn.execute("SELECT DISTINCT category FROM expenses WHERE category IS NOT NULL AND category != '' ORDER BY category").fetchall()]
                # Money config for rent visibility
                try:
                    money_cfg = conn.execute("SELECT * FROM money_config ORDER BY id DESC LIMIT 1").fetchone()
                except Exception:
                    money_cfg = None
                # P2P rate for trade quick-log
                try:
                    from app.plugins.p2p import P2PPlugin
                    p2p_rate = P2PPlugin()._rate(conn)
                except Exception:
                    p2p_rate = 1.15

            # ---- Quick-log row (command center input) ----
            cat_opts = "".join(f"<option value='{c}'>{c}</option>" for c in (categories or ["Food"]))
            habit_opts = "".join(f"<option value='{h['id']}'>{h['name']}</option>" for h in habits)

            quick = f"""
            <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4'>
                <div class='flex items-center justify-between mb-3'>
                    <h3 class='font-semibold text-white text-sm'>⚡ Quick Log — everything lands in its tab</h3>
                    <span class='text-[10px] uppercase font-mono text-slate-500'>planner hub</span>
                </div>
                <div class='grid grid-cols-1 md:grid-cols-4 gap-3'>
                    <!-- Expense -->
                    <form hx-post='/api/expenses/' hx-target='#expenses-list' hx-swap='outerHTML'
                          @submit="toast = 'Expense logged ✓'"
                          class='bg-dark-950 border border-dark-800 rounded-xl p-3 space-y-2'>
                        <div class='text-[10px] uppercase font-mono text-slate-400'>💸 Expense</div>
                        <div class='flex gap-1'>
                            <input type='number' step='0.01' name='amount' placeholder='0.00' required
                                   class='w-20 bg-dark-900 border border-dark-800 rounded-lg px-2 py-1.5 text-white text-xs font-mono'>
                            <select name='category' class='flex-1 bg-dark-900 border border-dark-800 rounded-lg px-1 py-1.5 text-white text-xs'>{cat_opts}</select>
                        </div>
                        <div class='flex gap-1'>
                            <input type='date' name='expense_date' class='flex-1 bg-dark-900 border border-dark-800 rounded-lg px-1 py-1.5 text-white text-xs'>
                            <input type='text' name='note' placeholder='note' class='flex-1 bg-dark-900 border border-dark-800 rounded-lg px-2 py-1.5 text-white text-xs'>
                        </div>
                        <button class='w-full bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium py-1.5 rounded-lg'>Log</button>
                    </form>
                    <!-- Income -->
                    <form hx-post='/api/money/income' hx-target='#money-area' hx-swap='outerHTML'
                          @submit="toast = 'Income logged ✓'"
                          class='bg-dark-950 border border-dark-800 rounded-xl p-3 space-y-2'>
                        <div class='text-[10px] uppercase font-mono text-slate-400'>💶 Income</div>
                        <div class='flex gap-1'>
                            <input type='number' step='0.01' name='amount' placeholder='0.00' required
                                   class='w-20 bg-dark-900 border border-dark-800 rounded-lg px-2 py-1.5 text-white text-xs font-mono'>
                            <select name='source' class='flex-1 bg-dark-900 border border-dark-800 rounded-lg px-1 py-1.5 text-white text-xs'>
                                <option value='Manual'>Manual</option><option value='Freelance'>Freelance</option>
                                <option value='Cash'>Cash</option><option value='Other'>Other</option>
                            </select>
                        </div>
                        <div class='flex gap-1'>
                            <input type='date' name='income_date' class='flex-1 bg-dark-900 border border-dark-800 rounded-lg px-1 py-1.5 text-white text-xs'>
                            <input type='text' name='note' placeholder='note' class='flex-1 bg-dark-900 border border-dark-800 rounded-lg px-2 py-1.5 text-white text-xs'>
                        </div>
                        <button class='w-full bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium py-1.5 rounded-lg'>Log</button>
                    </form>
                    <!-- Habit check -->
                    <div class='bg-dark-950 border border-dark-800 rounded-xl p-3 space-y-2'>
                        <div class='text-[10px] uppercase font-mono text-slate-400'>🔥 Habit check</div>
                        <form hx-post='/api/habits/check' hx-target='#habits-list' hx-swap='outerHTML'
                              @submit="toast = 'Habit checked ✓'" class='space-y-2'>
                            <select name='habit_id' class='w-full bg-dark-900 border border-dark-800 rounded-lg px-1 py-1.5 text-white text-xs'>
                                {habit_opts if habit_opts else "<option value=''>No habits yet — add one below</option>"}
                            </select>
                            <div class='flex gap-1'>
                                <input type='date' name='habit_date' class='flex-1 bg-dark-900 border border-dark-800 rounded-lg px-1 py-1.5 text-white text-xs'>
                                <button class='bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium px-3 py-1.5 rounded-lg'>✓</button>
                            </div>
                        </form>
                        <form hx-post='/api/habits/' hx-target='#habits-list' hx-swap='outerHTML'
                              @submit="toast = 'Habit added ✓'" class='flex gap-1'>
                            <input type='text' name='name' placeholder='+ new habit (e.g. Gym)' required
                                   class='flex-1 bg-dark-900 border border-dark-800 rounded-lg px-2 py-1.5 text-white text-xs'>
                            <input type='hidden' name='target_streak' value='7'>
                            <button class='bg-dark-700 hover:bg-dark-600 text-white text-xs font-medium px-3 py-1.5 rounded-lg'>+</button>
                        </form>
                    </div>
                    <!-- P2P trade -->
                    <form hx-post='/api/p2p/orders' hx-target='#p2p-area' hx-swap='outerHTML'
                          @submit="toast = 'Trade logged ✓ — profit fed to Money'"
                          class='bg-dark-950 border border-dark-800 rounded-xl p-3 space-y-2'>
                        <div class='text-[10px] uppercase font-mono text-slate-400'>🛰️ P2P trade</div>
                        <div class='flex gap-1'>
                            <input type='number' step='0.01' name='receive_eur' placeholder='€50' required
                                   class='w-20 bg-dark-900 border border-dark-800 rounded-lg px-2 py-1.5 text-white text-xs font-mono'>
                            <input type='number' step='0.01' name='sent_usdt' placeholder='USDT' required
                                   class='flex-1 bg-dark-900 border border-dark-800 rounded-lg px-2 py-1.5 text-white text-xs font-mono'>
                        </div>
                        <div class='flex gap-1'>
                            <input type='date' name='trade_date' class='flex-1 bg-dark-900 border border-dark-800 rounded-lg px-1 py-1.5 text-white text-xs'>
                            <input type='text' name='note' value='dex' class='w-16 bg-dark-900 border border-dark-800 rounded-lg px-2 py-1.5 text-white text-xs'>
                        </div>
                        <button class='w-full bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium py-1.5 rounded-lg'>Log trade</button>
                    </form>
                </div>
            </div>

            <div class='flex items-center justify-between mt-2'>
                <h3 class='font-semibold text-white text-sm'>📋 Active plans</h3>
                <div class='flex gap-3 text-[10px] uppercase font-mono text-slate-500'>
                    <span>💰 <a href='#' @click.prevent="refreshTab('expenses')" class='hover:text-emerald-400'>Expenses</a></span>
                    <span>💶 <a href='#' @click.prevent="refreshTab('money')" class='hover:text-emerald-400'>Cashflow</a></span>
                    <span>🔥 <a href='#' @click.prevent="refreshTab('habits')" class='hover:text-emerald-400'>Habits</a></span>
                    <span>🛰️ <a href='#' @click.prevent="refreshTab('p2p')" class='hover:text-emerald-400'>P2P</a></span>
                </div>
            </div>
            """

            if not plans:
                empty_html = """
                <div class='text-slate-500 py-8 text-center text-sm'>
                    No plans yet — set your first goal below.<br>
                    Plans turn trackers into targets: "Gym 3x/week", "Spend ≤100€/week", "5 P2P orders/week".
                </div>
                """
            else:
                empty_html = ""

            html = quick + "<div class='space-y-3 mt-3'>"
            for p in plans:
                freq = p['frequency']
                target = p['target_quantity']
                progress = self._progress_for(global_db, p)
                pct = min(int((progress / target) * 100), 100) if target else 0

                if p['target_type'] == 'expense':
                    color = "text-emerald-400" if progress <= target else "text-red-400"
                    prog_display = f"€{progress:.2f} / €{target:.2f}"
                else:
                    color = "text-emerald-400" if pct >= 100 else ("text-amber-400" if pct >= 50 else "text-slate-400")
                    prog_display = f"{progress}/{int(target)}"

                # Inline action per plan type
                if p['target_type'] == 'habit' and p['target_id']:
                    action = f"""
                    <form hx-post='/api/habits/{p['target_id']}/check' hx-target='#habits-list' hx-swap='outerHTML'
                          @submit="toast = 'Habit done ✓'" class='flex gap-1 items-center mt-2'>
                        <input type='date' name='habit_date' class='bg-dark-950 border border-dark-800 rounded-lg px-2 py-1 text-white text-xs font-mono'>
                        <button class='bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium px-3 py-1 rounded-lg'>✓ check</button>
                        <span class='text-[10px] text-slate-500 ml-auto'><a href='#' @click.prevent="refreshTab('habits')" class='hover:text-emerald-400'>open habits →</a></span>
                    </form>"""
                elif p['target_type'] == 'expense':
                    action = f"""
                    <form hx-post='/api/expenses/' hx-target='#expenses-list' hx-swap='outerHTML'
                          @submit="toast = 'Expense logged ✓'" class='flex gap-1 items-center mt-2'>
                        <input type='number' step='0.01' name='amount' placeholder='€0.00' required
                               class='w-20 bg-dark-950 border border-dark-800 rounded-lg px-2 py-1 text-white text-xs font-mono'>
                        <input type='hidden' name='category' value="{p['target_name'] or 'Other'}">
                        <input type='hidden' name='note' value="{p['goal_label']}">
                        <input type='date' name='expense_date' class='bg-dark-950 border border-dark-800 rounded-lg px-2 py-1 text-white text-xs font-mono'>
                        <button class='bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium px-3 py-1 rounded-lg'>log</button>
                        <span class='text-[10px] text-slate-500 ml-auto'><a href='#' @click.prevent="refreshTab('expenses')" class='hover:text-emerald-400'>open expenses →</a></span>
                    </form>"""
                elif p['target_type'] == 'p2p':
                    action = f"""
                    <form hx-post='/api/p2p/orders' hx-target='#p2p-area' hx-swap='outerHTML'
                          @submit="toast = 'Trade logged ✓'" class='flex gap-1 items-center mt-2'>
                        <input type='number' step='0.01' name='receive_eur' placeholder='€50' required
                               class='w-20 bg-dark-950 border border-dark-800 rounded-lg px-2 py-1 text-white text-xs font-mono'>
                        <input type='number' step='0.01' name='sent_usdt' placeholder='USDT' required
                               class='w-24 bg-dark-950 border border-dark-800 rounded-lg px-2 py-1 text-white text-xs font-mono'>
                        <input type='date' name='trade_date' class='bg-dark-950 border border-dark-800 rounded-lg px-2 py-1 text-white text-xs font-mono'>
                        <button class='bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium px-3 py-1 rounded-lg'>log</button>
                        <span class='text-[10px] text-slate-500 ml-auto'><a href='#' @click.prevent="refreshTab('p2p')" class='hover:text-emerald-400'>open P2P →</a></span>
                    </form>"""
                else:
                    action = ""

                html += f"""
                <div class='bg-dark-900 border border-dark-700 rounded-xl p-4'>
                    <div class='flex justify-between items-start'>
                        <div>
                            <h4 class='font-semibold text-white text-sm'>{p['goal_label']}</h4>
                            <p class='text-xs text-slate-400 mt-0.5'><span class='uppercase font-mono'>{p['target_type']}</span> • {p['frequency']}{f" • <span class='text-slate-300'>{p['target_name']}</span>" if p['target_name'] else ""}</p>
                        </div>
                        <button hx-delete='/api/planner/{p['id']}' hx-target='#planner-list' class='text-slate-500 hover:text-red-400 p-1'>✕</button>
                    </div>
                    <div class='flex items-center mt-3'>
                        <div class='flex-1 h-1.5 bg-dark-700 rounded-full overflow-hidden'>
                            <div class='bg-emerald-500 h-full rounded-full' style='width: {pct}%'></div>
                        </div>
                        <span class='ml-3 text-xs font-mono {color}'>{prog_display}</span>
                    </div>
                    {action}
                </div>
                """
            html += "</div>"
            return html

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