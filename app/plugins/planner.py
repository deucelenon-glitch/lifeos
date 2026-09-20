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

    def _auto_link_habits(self, conn, plans):
        """Heal habit plans that point at no/invalid habit.

        Old plans carry target_id = 0 (no FK), or the habit may have been
        deleted since. Match by name when possible; otherwise auto-create the
        habit from the goal label so progress starts counting.
        """
        habits = conn.execute("SELECT id, name FROM habits").fetchall()
        by_id = {h["id"]: h for h in habits}
        unlinked = []
        for p in plans:
            if p["target_type"] != "habit":
                continue
            # unlinked = no id yet, or the referenced habit no longer exists
            if not (p["target_id"] or 0) or p["target_id"] not in by_id:
                unlinked.append(p)
        if not unlinked:
            return

        def norm(s: str) -> str:
            return "".join(c for c in (s or "").lower() if c.isalnum())

        changed = 0
        for plan in unlinked:
            goal = norm(plan["goal_label"])
            best = None
            for h in habits:
                hname = norm(h["name"])
                if goal and (hname in goal or goal in hname):
                    best = h
                    break
            if best is None and goal:
                # No matching habit -> auto-create one from the goal label
                name = (plan["goal_label"] or "Habit").strip()
                cur = conn.execute(
                    "INSERT INTO habits (name, target_streak) VALUES (?, ?)",
                    (name, int(plan["target_quantity"] or 1)),
                )
                best = {"id": cur.lastrowid, "name": name}
                habits.append(best)
                by_id[best["id"]] = best
            if best:
                conn.execute(
                    "UPDATE plans SET target_id = ?, target_name = ? WHERE id = ?",
                    (best["id"], best["name"], plan["id"]),
                )
                changed += 1
        if changed:
            conn.commit()
            print(f"[planner] healed {changed} unlinked habit plan(s)")

    def register_routes(self) -> APIRouter:
        router = APIRouter()

        @router.get("/view", response_class=HTMLResponse)
        def planner_view(request: Request):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                plans = conn.execute("SELECT * FROM plans ORDER BY id DESC").fetchall()
                self._auto_link_habits(conn, plans)
                plans = conn.execute("SELECT * FROM plans ORDER BY id DESC").fetchall()

            plan_type_icon = {
                "habit": "🔥", "expense": "💰", "p2p": "🛰️",
            }
            plan_type_tab = {
                "habit": "habits", "expense": "expenses", "p2p": "p2p",
            }

            header = """
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

            tab_links = {
                "habit": ("🔥", "Habits", "refreshTab('habits')"),
                "expense": ("💰", "Expenses", "refreshTab('expenses')"),
                "p2p": ("🛰️", "P2P", "refreshTab('p2p')"),
            }

            cards = []
            for p in plans:
                target = p['target_quantity']
                progress = self._progress_for(global_db, p)
                pct = min(int((progress / target) * 100), 100) if target else 0
                unlinked = p['target_type'] == 'habit' and not (p['target_id'] or 0)

                if p['target_type'] == 'expense':
                    color = "text-emerald-400" if progress <= target else "text-red-400"
                    prog_display = f"€{progress:.2f} / €{target:.2f}"
                else:
                    color = "text-emerald-400" if pct >= 100 else ("text-amber-400" if pct >= 50 else "text-slate-400")
                    prog_display = f"{int(progress)}/{int(target)}"

                icon, tab_label, tab_call = tab_links.get(
                    p['target_type'], ("📌", "Tracker", "")
                )

                # Pure planning: no inline log/check forms.
                # Every plan links out to the tab where the actual logging happens.
                linked = ""
                if unlinked:
                    linked = f"""
                    <div class='flex items-center gap-1 mt-2'>
                        <span class='text-[10px] text-amber-400'>⚠️ not linked to a habit</span>
                        <span class='text-[10px] text-slate-500'>· delete & re-add with a habit picked</span>
                    </div>"""
                elif tab_call:
                    linked = f"""
                    <div class='flex items-center gap-1 mt-2'>
                        <span class='text-[10px] text-slate-500'><a href='#' @click.prevent="{tab_call}" class='hover:text-emerald-400'>open {tab_label.lower()} →</a></span>
                    </div>"""

                cards.append(f"""
                <div class='bg-dark-900 border border-dark-700 rounded-xl p-4'>
                    <div class='flex justify-between items-start'>
                        <div>
                            <h4 class='font-semibold text-white text-sm'>{icon} {p['goal_label']}</h4>
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
                    {linked}
                </div>
                """)

            if cards:
                html = header + "<div class='space-y-3 mt-3'>" + "".join(cards) + "</div>"
            else:
                html = header + empty_html
            return html

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
            if target_type == "habit":
                with global_db.get_connection() as conn:
                    if target_id:
                        row = conn.execute(
                            "SELECT id, name FROM habits WHERE id = ?", (target_id,)
                        ).fetchone()
                        if row:
                            target_name = row["name"]
                        else:
                            target_id = 0
                    if not target_id:
                        # No habit picked (list empty or left blank) -> auto-create one
                        # from the goal label so progress can actually be tracked.
                        name = (goal_label or "Habit").strip()
                        cur = conn.execute(
                            "INSERT INTO habits (name, target_streak) VALUES (?, ?)",
                            (name, int(target_quantity or 1)),
                        )
                        target_id = cur.lastrowid
                        target_name = name
                        conn.commit()
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
            target_id = plan["target_id"] or 0
            frequency = plan["frequency"]

            # Date windows: daily = today, weekly = this Mon-Sun, monthly = this month
            if frequency == "daily":
                habit_filter = "completed_date = date('now')"
                expense_filter = "expense_date = date('now')"
                p2p_filter = "trade_date = date('now')"
            elif frequency == "monthly":
                habit_filter = "strftime('%Y-%m', completed_date) = strftime('%Y-%m', 'now')"
                expense_filter = "strftime('%Y-%m', expense_date) = strftime('%Y-%m', 'now')"
                p2p_filter = "strftime('%Y-%m', trade_date) = strftime('%Y-%m', 'now')"
            else:  # weekly = Mon-Sun of current week
                habit_filter = "completed_date >= date('now', 'weekday 1', '-7 days') AND completed_date < date('now', 'weekday 1', '1 day')"
                expense_filter = "expense_date >= date('now', 'weekday 1', '-7 days') AND expense_date < date('now', 'weekday 1', '1 day')"
                p2p_filter = "trade_date >= date('now', 'weekday 1', '-7 days') AND trade_date < date('now', 'weekday 1', '1 day')"

            if target_type == "habit":
                if not target_id:
                    return 0.0  # unlinked plan — no real progress possible
                row = conn.execute(
                    f"SELECT COUNT(DISTINCT completed_date) FROM habit_logs WHERE habit_id = ? AND {habit_filter}",
                    (target_id,),
                ).fetchone()
                return float(row[0] or 0)

            elif target_type == "expense":
                cat = plan["target_name"] if plan["target_name"] else ""
                if cat:
                    row = conn.execute(
                        f"SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE category = ? AND {expense_filter}",
                        (cat,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        f"SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE {expense_filter}"
                    ).fetchone()
                return float(row[0] or 0.0)

            elif target_type == "p2p":
                row = conn.execute(
                    f"SELECT COUNT(*) FROM p2p_orders WHERE {p2p_filter}"
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