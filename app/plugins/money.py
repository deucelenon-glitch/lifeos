from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from app.plugins.base import LifeOSPlugin
from datetime import datetime
import sqlite3


def _r2(x: float) -> float:
    return round(float(x) + 1e-9, 2)


class MoneyPlugin(LifeOSPlugin):
    """Money & Cashflow brain.

    - Fixed monthly cost (house rent) + due day
    - Daily variable income (manual + auto-fed from P2P ledger profits)
    - Monthly budget ceiling; alerts when OVER, praises when UNDER
    - Worker-driven periodic_check() -> tiered notifications
    """

    @property
    def name(self) -> str:
        return "money"

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------
    def init_tables(self, conn: sqlite3.Connection):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS money_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rent_amount REAL DEFAULT 0,
                rent_due_day INTEGER DEFAULT 1,
                currency TEXT DEFAULT '€',
                monthly_budget REAL DEFAULT 0,
                income_goal REAL DEFAULT 0,
                alert_state TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS income_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount REAL NOT NULL,
                source TEXT DEFAULT 'manual',
                note TEXT,
                income_date TEXT NOT NULL DEFAULT (date('now')),
                ref_id INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS money_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type TEXT NOT NULL,
                period TEXT NOT NULL,
                message TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (alert_type, period)
            )
        """)
        row = conn.execute("SELECT COUNT(*) FROM money_config").fetchone()
        if not row or row[0] == 0:
            conn.execute("INSERT INTO money_config (rent_amount, rent_due_day, monthly_budget) VALUES (0, 1, 0)")
        # Migration: add income_goal column if missing (existing DBs)
        cols = [c[1] for c in conn.execute("PRAGMA table_info(money_config)").fetchall()]
        if "income_goal" not in cols:
            conn.execute("ALTER TABLE money_config ADD COLUMN income_goal REAL DEFAULT 0")
        conn.commit()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _cfg(self, conn) -> sqlite3.Row:
        return conn.execute("SELECT * FROM money_config ORDER BY id DESC LIMIT 1").fetchone()

    def _month(self) -> str:
        return datetime.now().strftime("%Y-%m")

    def _month_income(self, conn) -> float:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM income_entries WHERE strftime('%Y-%m', income_date) = ?",
            (self._month(),),
        ).fetchone()
        return _r2(row[0])

    def _month_burn(self, conn) -> float:
        """Burn linked live from the Expenses tracker."""
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE strftime('%Y-%m', expense_date) = strftime('%Y-%m','now')"
        ).fetchone()
        return _r2(row[0])

    def _today_income(self, conn) -> float:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM income_entries WHERE income_date = date('now')"
        ).fetchone()
        return _r2(row[0])

    def _budget_state(self, burn: float, budget: float) -> str:
        """over when burn > budget; warn at >=80%; else under/unset."""
        if budget <= 0:
            return "unset"
        if burn > budget:
            return "over"
        if burn >= budget * 0.8:
            return "warn"
        return "under"

    def _telegram_cfg(self, conn):
        row = conn.execute("SELECT bot_token, chat_id FROM telegram_config ORDER BY id DESC LIMIT 1").fetchone()
        return (row["bot_token"], row["chat_id"]) if row else (None, None)

    def _alert_sent(self, conn, alert_type: str, period: str) -> bool:
        row = conn.execute(
            "SELECT COUNT(*) c FROM money_alerts WHERE alert_type = ? AND period = ?",
            (alert_type, period),
        ).fetchone()
        return bool(row["c"])

    def _mark_alert(self, conn, alert_type: str, period: str, message: str):
        conn.execute(
            "INSERT OR IGNORE INTO money_alerts (alert_type, period, message) VALUES (?, ?, ?)",
            (alert_type, period, message),
        )

    def _notify(self, title: str, message: str, conn=None):
        from app.utils.notifications import notify
        token, chat_id = (None, None)
        if conn is not None:
            token, chat_id = self._telegram_cfg(conn)
        notify(title, message, telegram_token=token, telegram_chat_id=chat_id)

    # ------------------------------------------------------------------
    # public API used by other plugins (P2P link)
    # ------------------------------------------------------------------
    def record_income(self, amount: float, source: str = "manual", note: str = "", ref_id: int = None, income_date: str = None):
        """Log income. Used by P2P ledger to auto-feed daily profit."""
        if not amount or amount == 0:
            return None
        from app.database import Database
        db = Database()
        with db.get_connection() as conn:
            if income_date:
                cur = conn.execute(
                    "INSERT INTO income_entries (amount, source, note, ref_id, income_date) VALUES (?, ?, ?, ?, ?)",
                    (_r2(amount), source, note or source, ref_id, income_date),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO income_entries (amount, source, note, ref_id) VALUES (?, ?, ?, ?)",
                    (_r2(amount), source, note or source, ref_id),
                )
            row_id = cur.lastrowid
        return row_id

    def remove_income_ref(self, ref_id: int, source: str = "p2p"):
        """Delete income entries linked to a deleted P2P trade."""
        from app.database import Database
        db = Database()
        with db.get_connection() as conn:
            conn.execute("DELETE FROM income_entries WHERE source = ? AND ref_id = ?", (source, ref_id))

    # ------------------------------------------------------------------
    # alert engine — called by worker
    # ------------------------------------------------------------------
    def check_budget(self, notify: bool = True) -> dict:
        """Evaluate budget/rent. notify=True pushes state-flip notifications."""
        from app.database import Database
        db = Database()
        with db.get_connection() as conn:
            cfg = self._cfg(conn)
            burn = self._month_burn(conn)
            income = self._month_income(conn)
            budget = cfg["monthly_budget"] or 0
            rent = cfg["rent_amount"] or 0
            due_day = cfg["rent_due_day"] or 1
            cur = cfg["currency"] or "€"
            state = self._budget_state(burn, budget)
            today_d = datetime.now().day
            result = {
                "state": state,
                "burn": burn,
                "income": income,
                "budget": budget,
                "rent": rent,
                "due_day": due_day,
                "days_to_rent": max(due_day - today_d, 0),
                "rent_due": bool(rent > 0 and today_d >= due_day),
                "currency": cur,
            }

            if notify:
                period = self._month()
                msgs = []

                # rent due — once per month
                if result["rent_due"] and not self._alert_sent(conn, "rent_due", period):
                    msg = f"🏠 Rent {cur}{rent:.2f} is due (day {due_day}) — pay it."
                    self._mark_alert(conn, "rent_due", period, msg)
                    msgs.append(msg)

                # budget state flip — over / under (praise) / warn
                prev = cfg["alert_state"]
                if state == "over" and prev != "over":
                    msg = f"🚨 OVER BUDGET — {cur}{burn:.2f} spent vs {cur}{budget:.2f} ({cur}{_r2(burn - budget):.2f} over). Ease up."
                    self._mark_alert(conn, "over_budget", period, msg)
                    msgs.append(msg)
                elif state == "warn" and prev not in ("warn", "over"):
                    msg = f"⚠️ {cur}{burn:.2f} spent — {cur}{_r2(budget - burn):.2f} left before {cur}{budget:.2f} budget."
                    self._mark_alert(conn, "warn_budget", period, msg)
                    msgs.append(msg)
                elif state == "under" and prev in ("over", "warn"):
                    msg = f"🎉 Back under budget — {cur}{_r2(budget - burn):.2f} spare. Nice discipline 🔥"
                    self._mark_alert(conn, "under_budget", period, msg)
                    msgs.append(msg)

                if state != prev:
                    conn.execute("UPDATE money_config SET alert_state = ? WHERE id = ?", (state, cfg["id"]))

                if msgs:
                    self._notify("💰 Cashflow", "\n".join(msgs), conn)

            return result

    # ------------------------------------------------------------------
    # routes
    # ------------------------------------------------------------------
    def register_routes(self) -> APIRouter:
        router = APIRouter()

        @router.get("/view", response_class=HTMLResponse)
        def money_view(request: Request):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                cfg = self._cfg(conn)
                income = self._month_income(conn)
                burn = self._month_burn(conn)
                today = self._today_income(conn)
                entries = conn.execute(
                    "SELECT * FROM income_entries ORDER BY id DESC LIMIT 15"
                ).fetchall()

            rent = cfg["rent_amount"] or 0
            budget = cfg["monthly_budget"] or 0
            income_goal = cfg["income_goal"] or 0
            due_day = cfg["rent_due_day"] or 1
            cur = cfg["currency"] or "€"
            today_d = datetime.now().day

            # Income goal progress
            if income_goal > 0:
                goal_pct = min(100.0, (income / income_goal) * 100)
                goal_left = _r2(max(0.0, income_goal - income))
                if income >= income_goal:
                    goal_txt = f"🎯 Goal hit — {cur}{_r2(income - income_goal):.2f} over"
                    goal_cls = "text-emerald-400"
                    goal_state = "HIT"
                else:
                    goal_txt = f"{cur}{goal_left:.2f} to go"
                    goal_cls = "text-amber-400"
                    goal_state = f"{goal_pct:.0f}%"
            else:
                goal_txt = "Set an income goal below"
                goal_cls = "text-slate-400"
                goal_state = "—"

            if budget > 0:
                remaining = _r2(budget - burn)
                if burn > budget:
                    status_txt = f"🔴 Over budget by {cur}{_r2(burn - budget):.2f}"
                    status_cls = "text-red-400"
                    state_label = "OVER"
                elif burn >= budget * 0.8:
                    status_txt = f"🟠 Approaching budget — {cur}{remaining:.2f} left"
                    status_cls = "text-amber-400"
                    state_label = "WARN"
                else:
                    status_txt = f"🟢 Under budget — {cur}{remaining:.2f} to spare"
                    status_cls = "text-emerald-400"
                    state_label = "UNDER"
            else:
                status_txt = "Set a monthly budget below to unlock alerts 🎯"
                status_cls = "text-slate-400"
                state_label = "—"

            if rent > 0:
                if today_d >= due_day:
                    rent_txt = f"⚠️ Rent due {cur}{rent:.2f} (day {due_day})"
                    rent_cls = "text-amber-400"
                else:
                    rent_txt = f"Rent {cur}{rent:.2f} due day {due_day} — {due_day - today_d}d left"
                    rent_cls = "text-slate-400"
            else:
                rent_txt = "No rent set"
                rent_cls = "text-slate-400"

            rows = ""
            for e in entries:
                tag = "🛰️" if e["source"] == "p2p" else "💶"
                delete = f"<button hx-delete='/api/money/income/{e['id']}' hx-target='#money-area' hx-swap='outerHTML' class='text-slate-600 hover:text-red-400 text-[10px] ml-1'>✕</button>" if e["source"] != "p2p" else ""
                rows += f"""
                <div class='flex justify-between items-center text-xs py-1.5'>
                    <span class='text-slate-400 font-mono'>{e['income_date']} {tag} {e['note'] or e['source']}{delete}</span>
                    <span class='text-emerald-400 font-mono'>+{cur}{e['amount']:.2f}</span>
                </div>"""

            html = f"""
            <div id='money-area' hx-get='/api/money/view' hx-trigger='load'>
            <div class='space-y-4'>
                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4'>
                    <div class='flex justify-between items-center'>
                        <div>
                            <h3 class='font-bold text-white text-sm'>💰 Cashflow</h3>
                            <p class='text-xs text-slate-400 mt-0.5'>{self._month()} • income vs burn</p>
                        </div>
                        <span class='text-xs px-2.5 py-1 rounded-full bg-dark-800 {status_cls} font-mono'>{status_txt}</span>
                    </div>
                </div>

                <div class='grid grid-cols-2 md:grid-cols-4 gap-3'>
                    <div class='bg-dark-900 border border-dark-800 rounded-2xl p-3'>
                        <div class='text-[10px] uppercase font-mono text-slate-400'>Income (month)</div>
                        <div class='text-xl font-bold text-emerald-400 font-mono'>{cur}{income:.2f}</div>
                        <div class='text-[10px] text-slate-500'>today +{cur}{today:.2f}</div>
                    </div>
                    <div class='bg-dark-900 border border-dark-800 rounded-2xl p-3'>
                        <div class='text-[10px] uppercase font-mono text-slate-400'>Income Goal</div>
                        <div class='text-xl font-bold {goal_cls} font-mono'>{cur}{income_goal:.2f}</div>
                        <div class='text-[10px] {goal_cls}'>{goal_txt}</div>
                    </div>
                    <div class='bg-dark-900 border border-dark-800 rounded-2xl p-3'>
                        <div class='text-[10px] uppercase font-mono text-slate-400'>Burn (month)</div>
                        <div class='text-xl font-bold text-white font-mono'>{cur}{burn:.2f}</div>
                        <div class='text-[10px] text-slate-500'>from Expenses tab</div>
                    </div>
                    <div class='bg-dark-900 border border-dark-800 rounded-2xl p-3'>
                        <div class='text-[10px] uppercase font-mono text-slate-400'>Budget</div>
                        <div class='text-xl font-bold text-white font-mono'>{cur}{budget:.2f}</div>
                        <div class='text-[10px] text-slate-500'>{("over by " + cur + f"{_r2(burn - budget):.2f}") if budget and burn > budget else "set in config"}</div>
                    </div>
                    <div class='bg-dark-900 border border-dark-800 rounded-2xl p-3'>
                        <div class='text-[10px] uppercase font-mono text-slate-400'>Rent</div>
                        <div class='text-xl font-bold text-white font-mono'>{cur}{rent:.2f}</div>
                        <div class='text-[10px] {rent_cls}'>{rent_txt}</div>
                    </div>
                </div>

                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4'>
                    <h4 class='font-semibold text-white text-xs uppercase mb-3'>💶 Log Income (daily)</h4>
                    <form hx-post='/api/money/income' hx-target='#money-area' hx-swap='outerHTML'
                          @submit="toast = 'Income logged ✓'"
                          class='flex gap-2 items-end flex-wrap'>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Amount {cur}</label>
                            <input type='number' step='0.01' name='amount' placeholder='25.00' required
                                   class='w-28 bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm font-mono'>
                        </div>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Note</label>
                            <input type='text' name='note' placeholder='freelance / cash / p2p'
                                   class='w-44 bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm'>
                        </div>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Source</label>
                            <input type='text' name='source' list='cat-income' placeholder='Manual'
                                   class='w-32 bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm'>
                            <datalist id='cat-income'></datalist>
                        </div>
                        <button type='submit' class='bg-emerald-600 hover:bg-emerald-500 text-white font-medium px-4 py-2 rounded-xl text-sm'>+ Log</button>
                    </form>
                </div>

                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4'>
                    <h4 class='font-semibold text-white text-xs uppercase mb-2'>Recent Income</h4>
                    {rows if rows else "<p class='text-xs text-slate-500 py-2'>No income logged yet — P2P profits auto-appear here with a 🛰️ tag.</p>"}
                </div>

                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4'>
                    <h4 class='font-semibold text-white text-xs uppercase mb-3'>⚙️ Config — rent & budget</h4>
                    <form hx-post='/api/money/config' hx-target='#money-area' hx-swap='outerHTML'
                          @submit="toast = 'Config saved ✓ — budget re-evaluated'"
                          class='grid grid-cols-2 md:grid-cols-5 gap-2 items-end'>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Rent {cur}/mo</label>
                            <input type='number' step='0.01' name='rent_amount' value='{rent:.2f}'
                                   class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm font-mono'>
                        </div>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Due day</label>
                            <input type='number' min='1' max='31' name='rent_due_day' value='{due_day}'
                                   class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm font-mono'>
                        </div>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Monthly budget {cur}</label>
                            <input type='number' step='0.01' name='monthly_budget' value='{budget:.2f}'
                                   class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm font-mono'>
                        </div>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Income goal {cur}/mo</label>
                            <input type='number' step='0.01' name='income_goal' value='{income_goal:.2f}'
                                   class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm font-mono'>
                        </div>
                        <div class='bg-dark-950 rounded-lg px-2 py-2 text-center'>
                            <div class='text-[10px] uppercase font-mono text-slate-400'>State</div>
                            <span class='{status_cls} font-mono text-sm font-bold'>{state_label}</span>
                        </div>
                        <button type='submit' class='w-full bg-dark-800 hover:bg-dark-700 text-emerald-400 font-medium py-2 rounded-xl text-sm'>Save</button>
                    </form>
                    <p class='text-[10px] text-slate-500 mt-1.5 font-mono'>alerts: 🔴 over-budget · 🟠 80% warn · 🟢 praise when back under — pushed via Termux/Telegram by the worker</p>
                </div>
            </div>
            </div>
            """
            return html

        @router.post("/income", response_class=HTMLResponse)
        def log_income(
            request: Request,
            amount: float = Form(...),
            note: str = Form(""),
            source: str = Form("manual"),
            income_date: str = Form(""),
        ):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                from app.categories import ensure_category
                source = ensure_category(conn, "income", source)
            income_date = (income_date or "").strip()
            if income_date:
                self.record_income(amount, source=source, note=note, income_date=income_date)
            else:
                self.record_income(amount, source=source, note=note)
            return money_view(request)

        @router.delete("/income/{entry_id}", response_class=HTMLResponse)
        def delete_income(request: Request, entry_id: int):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                conn.execute("DELETE FROM income_entries WHERE id = ? AND source != 'p2p'", (entry_id,))
            return money_view(request)

        @router.post("/config", response_class=HTMLResponse)
        def save_config(
            request: Request,
            rent_amount: float = Form(0),
            rent_due_day: int = Form(1),
            monthly_budget: float = Form(0),
            income_goal: float = Form(0),
        ):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                cfg = self._cfg(conn)
                if cfg:
                    conn.execute(
                        "UPDATE money_config SET rent_amount = ?, rent_due_day = ?, monthly_budget = ?, income_goal = ? WHERE id = ?",
                        (rent_amount, rent_due_day, monthly_budget, income_goal, cfg["id"]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO money_config (rent_amount, rent_due_day, monthly_budget, income_goal) VALUES (?, ?, ?, ?)",
                        (rent_amount, rent_due_day, monthly_budget, income_goal),
                    )
            self.check_budget(notify=True)
            return money_view(request)

        @router.get("/data")
        def money_data():
            return self.check_budget(notify=False)

        return router

    def periodic_check(self):
        """Worker hook: evaluate budget once per cycle, push alerts on flips."""
        try:
            self.check_budget(notify=True)
        except Exception as e:
            print(f"[money] periodic_check error: {e}")
        return False

    def bot_commands(self) -> dict:
        def cmd_money(chat_id, parts):
            r = self.check_budget(notify=False)
            cur = r["currency"]
            lines = [
                f"*💰 Cashflow — {self._month()}*",
                f"State: {r['state'].upper()}",
                f"Income: {cur}{r['income']:.2f} · Burn: {cur}{r['burn']:.2f}",
            ]
            if r["budget"]:
                lines.append(f"Budget: {cur}{r['budget']:.2f} ({cur}{max(r['budget'] - r['burn'], 0):.2f} left)")
            if r["rent"]:
                lines.append(f"Rent: {cur}{r['rent']:.2f} due day {r['due_day']}"
                             + (" ⚠️ DUE" if r["rent_due"] else f" ({r['days_to_rent']}d left)"))
            return "\n".join(lines)

        def cmd_income(chat_id, parts):
            # /income <amount> [note]
            if len(parts) < 2:
                return "Usage: /income <amount> [note]  →  e.g. /income 25 freelance"
            try:
                amt = float(parts[1])
            except ValueError:
                return "Amount must be a number: /income 25"
            note = " ".join(parts[2:]) or "manual"
            self.record_income(amt, source="manual", note=note)
            r = self.check_budget(notify=False)
            return f"✅ Logged {r['currency']}{amt:.2f} income — month total {r['currency']}{r['income']:.2f}"

        return {"money": cmd_money, "income": cmd_income}

    def get_dashboard_widgets(self) -> list:
        return []
