from abc import ABC, abstractmethod
from fastapi import APIRouter
import sqlite3

class LifeOSPlugin(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Plugin identifier name"""
        pass

    @abstractmethod
    def register_routes(self) -> APIRouter:
        """Return FastAPI router with plugin endpoints"""
        pass

    @abstractmethod
    def init_tables(self, conn: sqlite3.Connection):
        """Initialize database tables for this plugin"""
        pass

    @abstractmethod
    def get_dashboard_widgets(self) -> list:
        """Return widget html fragments or metadata for the main dashboard"""
        pass

    def menu(self) -> dict:
        """Menu entry metadata shown in the webapp drawer. New plugins auto-appear below existing entries."""
        return {
            "id": self.name,
            "icon": "🧩",
            "label": self.name.title(),
            "badge": "",
            "view": f"/api/{self.name}/view",
        }

    def bot_commands(self) -> dict:
        """Optional Telegram bot command handlers.
        Returns {command: callable(chat_id, parts) -> reply_text}
        """
        return {}

    def periodic_check(self):
        """Optional periodic job (reminders etc). Called on a timer by the worker."""
        pass
