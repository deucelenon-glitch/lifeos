from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from app.plugins.base import LifeOSPlugin
from datetime import datetime, timezone
import sqlite3

class ExpensesPlugin(LifeOSPlugin):
    @property
    def name(self) -> str:
        return "expenses"

    def init_tables(self, conn: sqlite3.Connection):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                note TEXT,
                expense_date TEXT DEFAULT (date('now')),
                source_account TEXT,
                balance_before REAL,
                balance_after REAL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migration: add columns if missing
        cols = [c[1] for c in conn.execute("PRAGMA table_info(expenses)").fetchall()]
        if "source_account" not in cols:
            conn.execute("ALTER TABLE expenses ADD COLUMN source_account TEXT")
        if "balance_before" not in cols:
            conn.execute("ALTER TABLE expenses ADD COLUMN balance_before REAL")
        if "balance_after" not in cols:
            conn.execute("ALTER TABLE expenses ADD COLUMN balance_after REAL")
        conn.commit()

    def _deduct_from_capital(self, amount: float, currency: str, platform: str):
        """Deduct an expense from a capital account, converting EUR<->USD.
        Returns (balance_before, balance_after) or (None, None)."""
        if not platform or amount <= 0:
            return (None, None)
        from app.database import db as global_db
        with global_db.get_connection() as conn:
            acc = conn.execute(
                "SELECT * FROM capital_accounts WHERE platform = ? ORDER BY id LIMIT 1",
                (platform,),
            ).fetchone()
            if not acc:
                return (None, None)
            # Convert the expense to the account's native currency
            if acc["currency"] == currency:
                deduct = amount
            elif acc["currency"] == "USD":  # expense in EUR -> USD
                from app.plugins.capital import _fx_eur_to_usd
                deduct = amount * _fx_eur_to_usd()
            else:  # expense in USD -> EUR
                from app.plugins.capital import _fx_eur_to_usd
                deduct = amount / _fx_eur_to_usd()
            before = float(acc["balance"])
            after = max(0.0, before - deduct)
            conn.execute(
                "UPDATE capital_accounts SET balance = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (after, acc["id"]),
            )
            return (before, after)

    def _refund_to_capital(self, amount: float, currency: str, platform: str):
        """Refund a deleted expense back to its capital account."""
        if not platform:
            return
        from app.database import db as global_db
        from app.plugins.capital import _fx_eur_to_usd
        with global_db.get_connection() as conn:
            acc = conn.execute(
                "SELECT * FROM capital_accounts WHERE platform = ? ORDER BY id LIMIT 1",
                (platform,),
            ).fetchone()
            if not acc:
                return
            if acc["currency"] == currency:
                refund = amount
            elif acc["currency"] == "USD":
                refund = amount * _fx_eur_to_usd()
            else:
                refund = amount / _fx_eur_to_usd()
            conn.execute(
                "UPDATE capital_accounts SET balance = balance + ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (refund, acc["id"]),
            )

    def _credit_to_capital(self, amount: float, currency: str, platform: str):
        """Credit income to a capital account, converting EUR<->USD.
        With a Fee. Mirror of _refund_to_capital — balances flow into capital."""
        if not platform or amount <= 0:
            return
        from app.database import db as global_db
        from app.plugins.capital import _fx_eur_to_usd
        with global_db.get_connection() as conn:
            acc = conn.execute(
                "SELECT * FROM capital_accounts WHERE platform = ? ORDER BY id LIMIT 1",
                (platform,),
            ).fetchone()
            if not acc:
                return
            if acc["currency"] == currency:
                credit = amount
            elif acc["currency"] == "USD":
                credit = amount * _fx_eur_to_usd()
            else:
                credit = amount / _fx_eur_to_usd()
            conn.execute(
                "UPDATE capital_accounts SET balance = balance + ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (credit, acc["id"]),
            )

    def create_expense_direct(self, amount: float, category: str, note: str = "", expense_date: str = "", source_account: str = ""):
        """Programmatic expense creation (used by habits one-click buttons).
        Doesn't need HTTP request/response, just logs + deducts from capital."""
        from app.database import db as global_db
        date_val = (expense_date or "").strip()
        if not date_val or date_val == "date('now')":
            date_val = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        source = (source_account or "").strip() or None
        with global_db.get_connection() as conn:
            cur = conn.execute(
                "INSERT INTO expenses (amount, category, note, expense_date, source_account) VALUES (?, ?, ?, ?, ?)",
                (float(amount), category, note, date_val, source),
            )
            expense_id = cur.lastrowid
        if source:
            before, after = self._deduct_from_capital(float(amount), "EUR", source)
            if before is not None and expense_id:
                with global_db.get_connection() as conn:
                    conn.execute(
                        "UPDATE expenses SET balance_before = ?, balance_after = ? WHERE id = ?",
                        (before, after, expense_id),
                    )
        return expense_id

    def register_routes(self) -> APIRouter:
        router = APIRouter()

        @router.get("/total")
        def get_total_expenses():
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                row = conn.execute("SELECT SUM(amount) FROM expenses WHERE strftime('%Y-%m', expense_date) = strftime('%Y-%m', 'now')").fetchone()
                total = row[0] if row and row[0] else 0.0
            return f"€{total:.2f}"

        @router.get("/list", response_class=HTMLResponse)
        def expenses_list_html(request: Request):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                expenses = conn.execute("SELECT * FROM expenses ORDER BY id DESC LIMIT 20").fetchall()
            
            if not expenses:
                return "<div class='text-slate-500 py-8 text-center'>No expenses logged this month.</div>"

            html = "<div class='space-y-3'>"
            for e in expenses:
                html += f"""
                <div class='bg-dark-900 border border-dark-700 rounded-xl p-4 flex items-center justify-between'>
                    <div>
                        <div class='flex items-center space-x-2'>
                            <span class='font-bold text-white text-lg'>€{e['amount']:.2f}</span>
                            <span class='text-xs px-2 py-0.5 rounded bg-dark-700 text-emerald-400 font-mono uppercase'>{e['category']}</span>
                        </div>
                        <p class='text-xs text-slate-400 mt-1'>{e['note'] or 'No note'} • <span class='font-mono'>{e['expense_date']}</span>{" • <span class='font-mono text-slate-500'>" + e['source_account'] + "</span>" if e['source_account'] else ''}</p>
                        {"<p class='text-[10px] font-mono text-slate-500 mt-0.5'>bal " + f"{e['balance_before']:.2f}" + " → <span class='text-red-400'>−" + f"{e['balance_before'] - e['balance_after']:.2f}" + "</span> → <span class='text-emerald-400'>" + f"{e['balance_after']:.2f}" + "</span></p>" if e['source_account'] and e['balance_before'] is not None else ''}
                    </div>
                    <button hx-delete='/api/expenses/{e['id']}' hx-target='#expenses-list' class='text-slate-500 hover:text-red-400 p-1.5 transition'>✕</button>
                </div>
                """
            html += "</div>"
            return html

        @router.get("/widget", response_class=HTMLResponse)
        def expenses_widget():
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                expenses = conn.execute("SELECT * FROM expenses ORDER BY id DESC LIMIT 5").fetchall()
            if not expenses:
                return "<div class='text-slate-500 py-6 text-center'>No expenses yet — log one in the Expenses tab.</div>"
            html = "<div class='space-y-2 text-sm'>"
            for e in expenses:
                html += f"""<div class='flex justify-between items-center'>
                    <span class='text-slate-200'>{e['category']} <span class='text-slate-500'>{e['note'] or ''}</span></span>
                    <span class='text-white font-mono'>€{e['amount']:.2f}</span></div>"""
            html += "</div>"
            return html

        @router.post("/", response_class=HTMLResponse)
        def create_expense(
            request: Request,
            amount: float = Form(...),
            category: str = Form(...),
            note: str = Form(""),
            expense_date: str = Form(""),
            source_account: str = Form(""),
        ):
            from app.database import db as global_db
            expense_date = (expense_date or "").strip()
            if not expense_date or expense_date == "date('now')":
                expense_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            source = (source_account or "").strip() or None
            with global_db.get_connection() as conn:
                cur = conn.execute(
                    "INSERT INTO expenses (amount, category, note, expense_date, source_account) VALUES (?, ?, ?, ?, ?)",
                    (amount, category, note, expense_date, source),
                )
                expense_id = cur.lastrowid
            if source:
                before, after = self._deduct_from_capital(amount, "EUR", source)
                if before is not None and expense_id:
                    with global_db.get_connection() as conn:
                        conn.execute(
                            "UPDATE expenses SET balance_before = ?, balance_after = ? WHERE id = ?",
                            (before, after, expense_id),
                        )
            return expenses_list_html(request)

        @router.delete("/{expense_id}", response_class=HTMLResponse)
        def delete_expense(request: Request, expense_id: int):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                row = conn.execute("SELECT amount, source_account FROM expenses WHERE id = ?", (expense_id,)).fetchone()
                if row and row["source_account"]:
                    self._refund_to_capital(row["amount"], "EUR", row["source_account"])
                conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
            return expenses_list_html(request)

        return router

    def get_dashboard_widgets(self) -> list:
        return []

    def menu(self) -> dict:
        return {"id": self.name, "icon": "💰", "label": "Expenses & Burn", "badge": "", "view": "/api/expenses/view"}
