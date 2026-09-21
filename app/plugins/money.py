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
        # Fixed monthly costs (rent, subs, insurance...) — the fixed part of the plan budget
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fixed_costs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # UNIFIED monthly budget items — rent, subs, food, anything. Σ = monthly budget goal.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS monthly_budget_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                amount REAL NOT NULL DEFAULT 0,
                due_day INTEGER DEFAULT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # One-time migration: fold legacy fixed_costs + money_config.rent into monthly_budget_items
        try:
            has_items = conn.execute("SELECT COUNT(*) c FROM monthly_budget_items").fetchone()["c"]
            if has_items == 0:
                for fc in conn.execute("SELECT name, amount FROM fixed_costs").fetchall():
                    conn.execute("INSERT INTO monthly_budget_items (name, amount) VALUES (?, ?)", (fc["name"], fc["amount"]))
                mcfg = conn.execute("SELECT rent_amount, rent_due_day FROM money_config ORDER BY id DESC LIMIT 1").fetchone()
                if mcfg and (mcfg["rent_amount"] or 0) > 0:
                    conn.execute(
                        "INSERT INTO monthly_budget_items (name, amount, due_day) VALUES ('Rent', ?, ?)",
                        (mcfg["rent_amount"], mcfg["rent_due_day"] or 1),
                    )
        except Exception as e:
            print(f"[money] budget-items migration skipped: {e}")
        row = conn.execute("SELECT COUNT(*) FROM money_config").fetchone()
        if not row or row[0] == 0:
            conn.execute("INSERT INTO money_config (rent_amount, rent_due_day, monthly_budget) VALUES (0, 1, 0)")
        # Migration: add income_goal column if missing (existing DBs)
        cols = [c[1] for c in conn.execute("PRAGMA table_info(money_config)").fetchall()]
        if "income_goal" not in cols:
            conn.execute("ALTER TABLE money_config ADD COLUMN income_goal REAL DEFAULT 0")
        if "daily_budget" not in cols:
            conn.execute("ALTER TABLE money_config ADD COLUMN daily_budget REAL DEFAULT 0")
        conn.commit()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _cfg(self, conn) -> sqlite3.Row:
        return conn.execute("SELECT * FROM money_config ORDER BY id DESC LIMIT 1").fetchone()

    # ---- unified monthly budget items ----
    def _budget_items(self, conn):
        return conn.execute("SELECT * FROM monthly_budget_items ORDER BY id ASC").fetchall()

    def _budget_total(self, conn) -> float:
        """Monthly budget = Planner expense plans total (single source of truth).
        Falls back to legacy monthly_budget_items only if no expense plans exist."""
        try:
            from app.plugins.planner import PlannerPlugin
            total = PlannerPlugin().monthly_plan_cost()
            if total and total > 0:
                return _r2(total)
        except Exception as e:
            print(f"[money] planner cost unavailable: {e}")
        row = conn.execute("SELECT COALESCE(SUM(amount),0) FROM monthly_budget_items").fetchone()
        return _r2(row[0])

    def _rent_item(self, conn):
        """Rent = expense plan named Rent (any casing), else legacy item named Rent, else first with a due day."""
        try:
            rent_rows = conn.execute(
                "SELECT target_name, cost_per, target_quantity FROM plans WHERE target_type = 'expense'"
            ).fetchall()
        except Exception:
            rent_rows = []
        for r in rent_rows:
            if (r["target_name"] or "").strip().lower() == "rent":
                amt = float(r["cost_per"] or 0) or float(r["target_quantity"] or 0)
                return {"name": "Rent", "amount": amt, "due_day": None}
        items = self._budget_items(conn)
        for it in items:
            if (it["name"] or "").strip().lower() == "rent":
                return it
        for it in items:
            if it["due_day"]:
                return it
        return None

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

    def _today_burn(self, conn) -> float:
        """Spent today, live from the Expenses tracker."""
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE expense_date = date('now')"
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
        """Log income. Used by P2P ledger to auto-feed daily profit.
        Also credits the capital account so income flows into Net Worth."""
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
            # Credit the capital account — which one? The termux expense source
            # (the account expenses deduct from) so income/expense are symmetric.
            cap_cfg = conn.execute(
                "SELECT source_account FROM termux_config ORDER BY id DESC LIMIT 1"
            ).fetchone()
            platform = (cap_cfg["source_account"] if cap_cfg else None) or "wise"
        if row_id:
            try:
                from app.plugins.expenses import ExpensesPlugin
                ExpensesPlugin()._credit_to_capital(amount, "EUR", platform)
            except Exception as e:
                print(f"[money] credit-to-capital failed: {e}")
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
            budget = self._budget_total(conn)          # Σ Planner expense plans — single source of truth
            rent_item = self._rent_item(conn)
            rent = float(rent_item["amount"] or 0) if rent_item else 0
            due_day = int(rent_item["due_day"] or 1) if rent_item else 1
            cur = cfg["currency"] or "€"
            state = self._budget_state(burn, budget)
            income_goal = cfg["income_goal"] or 0
            today_d = datetime.now().day
            # Plan check: does the planned monthly budget fit the income goal?
            plan_delta = _r2(budget - income_goal) if income_goal > 0 else 0.0
            plan_over = bool(income_goal > 0 and budget > income_goal)
            result = {
                "state": state,
                "burn": burn,
                "income": income,
                "budget": budget,
                "income_goal": income_goal,
                "plan_over": plan_over,
                "plan_delta": plan_delta,
                "rent": rent,
                "due_day": due_day,
                "days_to_rent": max(due_day - today_d, 0),
                "rent_due": bool(rent > 0 and today_d >= due_day),
                "currency": cur,
            }

            if notify:
                period = self._month()
                msgs = []

                # due-day items (rent, subs...) — once per month each
                for bit in self._budget_items(conn):
                    dday = bit["due_day"]
                    if not dday or (bit["amount"] or 0) <= 0:
                        continue
                    if today_d >= int(dday) and not self._alert_sent(conn, "due_day", f"{period}:{bit['id']}"):
                        msg = f"📌 {bit['name']} {cur}{float(bit['amount']):.2f} is due (day {dday}) — pay it."
                        self._mark_alert(conn, "due_day", f"{period}:{bit['id']}", msg)
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

                # plan check — monthly budget vs income goal (once/month)
                if result["plan_over"] and not self._alert_sent(conn, "budget_over_income", period):
                    msg = (f"⚠️ PLAN CHECK — budget {cur}{budget:.2f} exceeds income goal {cur}{income_goal:.2f} "
                           f"by {cur}{result['plan_delta']:.2f}. Trim the budget or raise income.")
                    self._mark_alert(conn, "budget_over_income", period, msg)
                    msgs.append(msg)

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
                today_spent = self._today_burn(conn)
                entries = conn.execute(
                    "SELECT * FROM income_entries ORDER BY id DESC LIMIT 15"
                ).fetchall()
                # Unified monthly budget items (rent, food, subs...) — Σ = monthly budget goal
                budget_items = self._budget_items(conn)
                budget = self._budget_total(conn)
                rent_item = self._rent_item(conn)
                rent = float(rent_item["amount"] or 0) if rent_item else 0
                due_day = int(rent_item["due_day"] or 1) if rent_item else 1

            income_goal = cfg["income_goal"] or 0
            daily_budget = cfg["daily_budget"] or 0
            cur = cfg["currency"] or "€"
            today_d = datetime.now().day

            # ---- plan feasibility: budget vs income goal ----
            # Habit-driven spend: Σ (weekly_target × cost_per) from the Habits tab
            habit_cost = 0.0
            try:
                from app.plugins.habits import HabitsPlugin
                habit_cost = HabitsPlugin().monthly_plan_cost()
            except Exception:
                pass

            # Budget € plans: Σ of per-occurrence costs normalized to monthly
            plan_cost = 0.0
            try:
                from app.plugins.planner import PlannerPlugin
                plan_cost = PlannerPlugin().monthly_plan_cost()
            except Exception:
                pass

            # Full plan = unified monthly budget (Σ items) + habit spend + budget € plan spend
            full_plan = _r2(budget + habit_cost + plan_cost)
            plan_delta = _r2(full_plan - income_goal) if income_goal > 0 else 0.0
            plan_over = bool(income_goal > 0 and full_plan > income_goal)
            if plan_over:
                plan_txt = f"⚠️ Plan €{full_plan:.2f} (€{budget:.2f} monthly budget + €{habit_cost:.2f} habits + €{plan_cost:.2f} plans) > income goal {cur}{income_goal:.2f} — short {cur}{plan_delta:.2f}/mo"
                plan_cls = "text-red-400"
            elif income_goal > 0:
                plan_txt = f"✅ Plan OK — saving {cur}{_r2(income_goal - full_plan):.2f}/mo"
                plan_cls = "text-emerald-400"
            else:
                plan_txt = "Set an income goal to check if the budget fits"
                plan_cls = "text-slate-400"

            # ---- runway: capital ÷ (budget − income) = months of sustain ----
            sustain_capital = 0.0
            try:
                from app.plugins.capital import CapitalPlugin
                with global_db.get_connection() as conn:
                    sustain_capital = CapitalPlugin()._sums(conn)["total_eur"]
            except Exception:
                pass
            net_burn = _r2(budget - income) if budget > 0 else 0.0  # planned spend − actual income so far
            if sustain_capital > 0 and net_burn > 0:
                runway_months = sustain_capital / net_burn
                if runway_months >= 3:
                    runway_txt = f"🛟 {runway_months:.1f} months (€{sustain_capital:.0f} cap ÷ €{net_burn:.2f}/mo net burn)"
                    runway_cls = "text-emerald-400"
                elif runway_months > 0:
                    runway_txt = f"⚠️ Only {runway_months:.1f} months left (€{sustain_capital:.0f} ÷ €{net_burn:.2f}/mo)"
                    runway_cls = "text-amber-400"
                else:
                    runway_txt = "Negative net burn — income covers the plan"
                    runway_cls = "text-emerald-400"
            elif budget <= 0:
                runway_txt = "Set a monthly budget to see runway"
                runway_cls = "text-slate-400"
            elif income >= budget:
                runway_txt = "Income ≥ budget — plan self-sustaining 🛟"
                runway_cls = "text-emerald-400"
            else:
                runway_txt = "No capital tracked — add accounts in 🏦 Capital"
                runway_cls = "text-slate-400"

            # Income goal progress & daily target (assuming ~30 days in month)
            days_in_month = 30
            daily_goal = _r2(income_goal / days_in_month) if income_goal > 0 else 0.0
            today_income = today

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

            if daily_goal > 0:
                daily_pct = min(100.0, (today_income / daily_goal) * 100)
                if today_income >= daily_goal:
                    daily_txt = f"🔥 Daily target met (+{cur}{_r2(today_income - daily_goal):.2f})"
                    daily_cls = "text-emerald-400"
                else:
                    daily_txt = f"{cur}{_r2(daily_goal - today_income):.2f} left for today"
                    daily_cls = "text-amber-400"
            else:
                daily_txt = "Set monthly goal to unlock"
                daily_cls = "text-slate-400"

            # Daily budget — explicit override, else monthly budget ÷ 30
            eff_daily_budget = daily_budget if daily_budget > 0 else (_r2(budget / 30) if budget > 0 else 0.0)
            if eff_daily_budget > 0:
                daily_spent_pct = min(100.0, (today_spent / eff_daily_budget) * 100)
                daily_left = _r2(max(0.0, eff_daily_budget - today_spent))
                if today_spent > eff_daily_budget:
                    daily_bud_txt = f"🔴 Over by {cur}{_r2(today_spent - eff_daily_budget):.2f} today"
                    daily_bud_cls = "text-red-400"
                    daily_bud_state = "OVER"
                elif today_spent >= eff_daily_budget * 0.8:
                    daily_bud_txt = f"🟠 {cur}{daily_left:.2f} left today"
                    daily_bud_cls = "text-amber-400"
                    daily_bud_state = "WARN"
                else:
                    daily_bud_txt = f"🟢 {cur}{daily_left:.2f} left today"
                    daily_bud_cls = "text-emerald-400"
                    daily_bud_state = "OK"
            else:
                daily_bud_txt = "Set a budget (or daily override) to unlock"
                daily_bud_cls = "text-slate-400"
                daily_bud_state = "—"

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

            # ---- daily metrics: income / spend / net / budget vs spend -------
            daily_net = _r2(today_income - today_spent)
            if today_spent > 0 or today_income > 0:
                if daily_net >= 0:
                    net_txt = f"+{cur}{daily_net:.2f} net today"
                    net_cls = "text-emerald-400"
                else:
                    net_txt = f"−{cur}{abs(daily_net):.2f} net today"
                    net_cls = "text-red-400"
            else:
                net_txt = "Nothing logged yet today"
                net_cls = "text-slate-500"

            metrics_html = f"""
            <div class='bg-dark-900 border border-dark-800 rounded-2xl p-3'>
                <div class='text-[10px] uppercase font-mono text-slate-400'>Today</div>
                <div class='flex flex-wrap gap-x-4 gap-y-1 text-sm'>
                    <span class='font-mono text-emerald-400'>+{cur}{today_income:.2f} in</span>
                    <span class='font-mono text-red-400'>−{cur}{today_spent:.2f} out</span>
                    <span class='font-mono {net_cls} font-bold'>{net_txt}</span>
                    <span class='font-mono {daily_bud_cls}'>daily budget {cur}{eff_daily_budget:.2f} · {daily_bud_state}</span>
                </div>
            </div>"""

            html = f"""
            <div id='money-area'>
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

                {metrics_html}

                <div class='grid grid-cols-2 md:grid-cols-6 gap-3'>
                    <div class='bg-dark-900 border border-dark-800 rounded-2xl p-3'>
                        <div class='text-[10px] uppercase font-mono text-slate-400'>Income (month)</div>
                        <div class='text-xl font-bold text-emerald-400 font-mono'>{cur}{income:.2f}</div>
                        <div class='text-[10px] text-slate-500'>today +{cur}{today:.2f}</div>
                    </div>
                    <div class='bg-dark-900 border border-dark-800 rounded-2xl p-3'>
                        <div class='text-[10px] uppercase font-mono text-slate-400'>Daily Target</div>
                        <div class='text-xl font-bold {daily_cls} font-mono'>{cur}{daily_goal:.2f}</div>
                        <div class='text-[10px] {daily_cls}'>{daily_txt}</div>
                    </div>
                    <div class='bg-dark-900 border border-dark-800 rounded-2xl p-3'>
                        <div class='text-[10px] uppercase font-mono text-slate-400'>Daily Budget</div>
                        <div class='text-xl font-bold {daily_bud_cls} font-mono'>{cur}{eff_daily_budget:.2f}</div>
                        <div class='text-[10px] {daily_bud_cls}'>{daily_bud_txt} · spent {cur}{today_spent:.2f}</div>
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
                        <div class='text-[10px] uppercase font-mono text-slate-400'>Runway</div>
                        <div class='text-xs font-bold {runway_cls} font-mono'>{runway_txt}</div>
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
                    <h4 class='font-semibold text-white text-xs uppercase mb-3'>⚙️ Config — income & alerts</h4>
                    <form hx-post='/api/money/config' hx-target='#money-area' hx-swap='outerHTML'
                          @submit="toast = 'Config saved ✓ — budget re-evaluated'"
                          class='grid grid-cols-2 md:grid-cols-5 gap-2 items-end'>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Daily budget {cur} <span class='text-slate-600'>(0=auto)</span></label>
                            <input type='number' step='0.01' name='daily_budget' value='{daily_budget:.2f}'
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
            income_goal: float = Form(0),
            daily_budget: float = Form(0),
        ):
            """Save income goal + daily budget only.
            Monthly budget is now auto-calculated from monthly_budget_items."""
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                cfg = self._cfg(conn)
                if cfg:
                    conn.execute(
                        "UPDATE money_config SET income_goal = ?, daily_budget = ? WHERE id = ?",
                        (income_goal, daily_budget, cfg["id"]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO money_config (income_goal, daily_budget) VALUES (?, ?)",
                        (income_goal, daily_budget),
                    )
            self.check_budget(notify=True)
            return money_view(request)

        @router.post("/budget-item", response_class=HTMLResponse)
        def add_budget_item(
            request: Request,
            name: str = Form(...),
            amount: float = Form(0),
            due_day: int = Form(None),
        ):
            """Add a monthly budget item (rent, food, subs...) — Σ = monthly budget goal."""
            from app.database import db as global_db
            name = (name or "").strip()
            if not name or amount <= 0:
                return money_view(request)
            with global_db.get_connection() as conn:
                conn.execute(
                    "INSERT INTO monthly_budget_items (name, amount, due_day) VALUES (?, ?, ?)",
                    (name, amount, due_day if due_day else None),
                )
            self.check_budget(notify=True)
            return money_view(request)

        @router.delete("/budget-item/{item_id}", response_class=HTMLResponse)
        def delete_budget_item(request: Request, item_id: int):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                conn.execute("DELETE FROM monthly_budget_items WHERE id = ?", (item_id,))
            self.check_budget(notify=True)
            return money_view(request)

        @router.post("/budget-item/{item_id}/expense", response_class=HTMLResponse)
        def budget_item_to_expense(request: Request, item_id: int):
            """One-click: log this month's budget item as an expense."""
            from app.database import db as global_db
            from app.plugins.expenses import ExpensesPlugin
            with global_db.get_connection() as conn:
                b = conn.execute(
                    "SELECT id, name, amount FROM monthly_budget_items WHERE id = ?", (item_id,)
                ).fetchone()
            if not b or not b["amount"]:
                return money_view(request)
            ExpensesPlugin().create_expense_direct(
                amount=float(b["amount"]),
                category=b["name"],
                note=f"budget item: {b['name']}",
            )
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
