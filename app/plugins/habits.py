from fastapi import APIRouter
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
                target_streak INTEGER DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS habit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                habit_id INTEGER,
                completed_date TEXT NOT NULL,
                FOREIGN KEY (habit_id) REFERENCES habits (id)
            )
        """)

    def register_routes(self) -> APIRouter:
        router = APIRouter()

        @router.get("/")
        def list_habits():
            return {"plugin": self.name, "status": "active"}

        return router

    def get_dashboard_widgets(self) -> list:
        return ["<div class='p-4 bg-slate-800 rounded shadow'>Habits Tracker Widget</div>"]
