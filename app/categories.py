"""Shared category system for LIFEOS.

One `categories` table, seeded with sensible defaults per domain.
Every plugin that has a "category" concept reads/writes through here,
so pickers stay consistent and planner targets can link to real categories.
"""
import sqlite3

DEFAULT_CATEGORIES = {
    "expense": ["Food", "Transport", "Rent", "Bills", "Fun", "Health", "Subscriptions", "Shopping", "Other"],
    "income": ["Manual", "Freelance", "P2P", "Cash", "Other"],
    "habit": ["Health", "Discipline", "Learning", "Creativity", "Social", "Other"],
    "p2p": ["RoboSats", "Peer-to-peer", "Local", "Other"],
}


def init_categories_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            domain TEXT NOT NULL,
            name TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0,
            UNIQUE (domain, name)
        )
    """)
    for domain, names in DEFAULT_CATEGORIES.items():
        for i, name in enumerate(names):
            conn.execute(
                "INSERT OR IGNORE INTO categories (domain, name, sort_order) VALUES (?, ?, ?)",
                (domain, name, i),
            )


def list_categories(conn: sqlite3.Connection, domain: str) -> list:
    rows = conn.execute(
        "SELECT name FROM categories WHERE domain = ? ORDER BY sort_order, name",
        (domain,),
    ).fetchall()
    return [r["name"] for r in rows]


def ensure_category(conn: sqlite3.Connection, domain: str, name: str) -> str:
    """Normalize a raw category string: trim, fall back to Other on empty."""
    name = (name or "").strip()
    if not name:
        return "Other"
    conn.execute(
        "INSERT OR IGNORE INTO categories (domain, name, sort_order) VALUES (?, ?, 99)",
        (domain, name),
    )
    return name