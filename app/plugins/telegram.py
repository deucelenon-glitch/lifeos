from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from app.plugins.base import LifeOSPlugin
import sqlite3

class TelegramPlugin(LifeOSPlugin):
    @property
    def name(self) -> str:
        return "telegram"

    def init_tables(self, conn: sqlite3.Connection):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_token TEXT,
                chat_id TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

    def register_routes(self) -> APIRouter:
        router = APIRouter()

        @router.post("/config")
        def save_config(request: Request, bot_token: str = Form(...), chat_id: str = Form(...)):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                conn.execute("DELETE FROM telegram_config")
                conn.execute("INSERT INTO telegram_config (bot_token, chat_id) VALUES (?, ?)", (bot_token, chat_id))
            return {"status": "saved"}

        @router.post("/test")
        def test_notification():
            from app.utils.notifications import send_termux
            success = send_termux("LIFEOS", "Test push notification from LIFEOS Telegram / Notification engine!", "high")
            return {"status": "sent", "termux": success}

        return router

    def get_dashboard_widgets(self) -> list:
        return []

    def menu(self) -> dict:
        return {"id": self.name, "icon": "🤖", "label": "Telegram Bot", "badge": "", "view": "/api/telegram/view"}
