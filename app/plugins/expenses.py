from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from app.plugins.base import LifeOSPlugin
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
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

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
                        <p class='text-xs text-slate-400 mt-1'>{e['note'] or 'No note'} • <span class='font-mono'>{e['expense_date']}</span></p>
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
        def create_expense(request: Request, amount: float = Form(...), category: str = Form(...), note: str = Form("")):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                conn.execute("INSERT INTO expenses (amount, category, note) VALUES (?, ?, ?)", (amount, category, note))
            return expenses_list_html(request)

        @router.delete("/{expense_id}", response_class=HTMLResponse)
        def delete_expense(request: Request, expense_id: int):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
            return expenses_list_html(request)

        return router

    def get_dashboard_widgets(self) -> list:
        return []
