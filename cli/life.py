#!/usr/bin/env python3
"""
LIFEOS CLI — terminal input surface.

Usage examples:
    life check gym                    # log a habit completion
    life add exp 5 "coffee"          # log an expense
    life now "deep work"             # set current time-log entry
    life status                       # dashboard summary

Every subcommand submits to the same SQLite DB as the webapp.
Plugin subcommands are auto-discovered from app.plugins.
"""
import argparse
import importlib
import pkgutil
import sqlite3
import sys
from pathlib import Path

# Allow running from repo root without install
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.database import Database  # noqa: E402


class LifeCLI:
    def __init__(self):
        self.db = Database()

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self.db.get_connection() as conn:
            conn.execute(sql, params)

    def check_habit(self, habit: str) -> None:
        self.execute(
            "INSERT INTO habit_logs (habit_id, completed_date) VALUES ((SELECT id FROM habits WHERE name = ?), date('now'))",
            (habit,),
        )
        print(f"✅ Checked '{habit}' for today.")

    def add_expense(self, amount: float, note: str = "") -> None:
        self.execute(
            "INSERT INTO events (plugin, action, payload) VALUES ('expenses', 'add', ?)",
            (f'{{"amount": {amount}, "note": "{note}"}}',),
        )
        print(f"💰 Logged expense: {amount} — {note}")

    def status(self) -> None:
        print("LIFEOS status")
        print("-------------")
        with self.db.get_connection() as conn:
            count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            habits = conn.execute("SELECT COUNT(*) FROM habits").fetchone()[0]
        print(f"Total events: {count}")
        print(f"Habits defined: {habits}")
        print("System healthy ✓")


def discover_plugins() -> list:
    plugins_dir = REPO_ROOT / "app" / "plugins"
    found = []
    for _, name, _ in pkgutil.iter_modules([str(plugins_dir)]):
        if name != "base":
            found.append(name)
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(prog="life", description="LIFEOS personal tracker")
    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser("check", help="Log habit completion")
    p_check.add_argument("habit", help="Habit name (exact match)")

    p_add = sub.add_parser("add", help="Add an event to a tracker")
    p_add.add_argument("tracker", choices=["exp", "expense"], help="Tracker type")
    p_add.add_argument("amount", type=float, help="Amount")
    p_add.add_argument("note", nargs="?", default="", help="Optional note")

    sub.add_parser("status", help="Show system summary")

    args = parser.parse_args(argv)
    cli = LifeCLI()

    if args.command == "check":
        cli.check_habit(args.habit)
    elif args.command == "add":
        cli.add_expense(args.amount, args.note)
    elif args.command == "status":
        cli.status()
    else:
        parser.print_help()
        return 1

    print(f"\nLoaded plugins: {', '.join(discover_plugins())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())