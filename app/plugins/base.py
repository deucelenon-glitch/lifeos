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
