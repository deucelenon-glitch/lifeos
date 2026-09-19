#!/usr/bin/env python3
"""
LIFEOS Telegram Bot Poller — input surface #3.

Long-polls Telegram for commands and logs them into the same SQLite DB
as the webapp and CLI. Runs as a background process in Termux.

Commands:
    /check <habit>          — log habit completion today
    /exp <amount> <note>    — log an expense (category optional: /exp 5 coffee food)
    /status                 — quick summary
    /streaks                — current habit streaks
    /help                   — this message

Requires: bot token + chat id saved via webapp (Telegram tab) or:
    python scripts/telegram_bot.py --token 123:ABC --chat 8738478074
"""
import argparse
import json
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.database import Database  # noqa: E402

API = "https://api.telegram.org/bot{token}/{method}"


def get_config() -> tuple:
    db = Database()
    try:
        with db.get_connection() as conn:
            row = conn.execute(
                "SELECT bot_token, chat_id FROM telegram_config ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return (row["bot_token"], row["chat_id"]) if row else (None, None)
    except Exception:
        return (None, None)


def call(method: str, token: str, **params) -> dict:
    r = requests.post(API.format(token=token, method=method), json=params, timeout=60)
    r.raise_for_status()
    return r.json()


def reply(token: str, chat_id: int, text: str):
    try:
        call("sendMessage", token, chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception as e:
        print(f"[reply failed] {e}")


def load_plugin_commands() -> dict:
    """Auto-discover bot commands from plugins (menu-style extension)."""
    import importlib
    import pkgutil
    commands = {}
    plugins_dir = REPO_ROOT / "app" / "plugins"
    if not plugins_dir.exists():
        return commands
    from app.plugins.base import LifeOSPlugin
    for _, module_name, _ in pkgutil.iter_modules([str(plugins_dir)]):
        if module_name == "base":
            continue
        try:
            module = importlib.import_module(f"app.plugins.{module_name}")
            for attr_name in dir(module):
                attribute = getattr(module, attr_name)
                if isinstance(attribute, type) and issubclass(attribute, LifeOSPlugin) and attribute is not LifeOSPlugin:
                    instance = attribute()
                    commands.update(instance.bot_commands())
        except Exception as e:
            print(f"[bot] plugin commands failed for {module_name}: {e}")
    return commands


def handle(chat_id: int, text: str) -> str:
    text = text.strip()
    parts = text.split()
    cmd = parts[0].lower().lstrip("/")

    db = Database()

    # Plugin commands first (planner, p2p, ...) — auto-discovered, extensible
    plugin_cmds = load_plugin_commands()
    if cmd in plugin_cmds:
        try:
            return plugin_cmds[cmd](chat_id, parts)
        except Exception as e:
            return f"⚠️ Plugin command error: {e}"

    if cmd in ("start", "help"):
        available = "/check gym • /exp 5 coffee food • /status • /streaks"
        extra = ", ".join(sorted(plugin_cmds.keys()))
        if extra:
            available += f" • {extra}"
        return (
            "*LIFEOS Bot*\n"
            f"{available}\n"
            "/plans — view goals\n"
            "/compare — plan vs actual"
        )

    if cmd == "check" and len(parts) >= 2:
        habit_name = " ".join(parts[1:]).lower()
        with db.get_connection() as conn:
            row = conn.execute("SELECT id, name FROM habits WHERE lower(name) = ?", (habit_name,)).fetchone()
            if not row:
                return f"❌ No habit named '{habit_name}'. Add it in the webapp first."
            conn.execute(
                "INSERT OR IGNORE INTO habit_logs (habit_id, completed_date) VALUES (?, ?)",
                (row["id"], date.today().isoformat()),
            )
        return f"✅ Checked *{row['name']}* for {date.today().isoformat()}"

    if cmd in ("exp", "expense") and len(parts) >= 2:
        try:
            amount = float(parts[1].replace(",", "."))
        except ValueError:
            return "❌ Amount must be a number: /exp 5.50 coffee"
        note = " ".join(parts[2:3])
        category = parts[3] if len(parts) >= 4 else note or "misc"
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO expenses (amount, category, note) VALUES (?, ?, ?)",
                (amount, category, note),
            )
        return f"💰 Logged €{amount:.2f} ({category})"

    if cmd == "status":
        with db.get_connection() as conn:
            exp = conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE strftime('%Y-%m', expense_date) = strftime('%Y-%m','now')"
            ).fetchone()[0]
            habits = conn.execute("SELECT COUNT(*) FROM habits").fetchone()[0]
        return f"*LIFEOS*\nMonthly burn: €{exp:.2f}\nHabits: {habits}"

    if cmd == "streaks":
        with db.get_connection() as conn:
            rows = conn.execute(
                """SELECT h.name, COUNT(DISTINCT hl.completed_date) as streak
                   FROM habits h LEFT JOIN habit_logs hl ON hl.habit_id = h.id
                   GROUP BY h.id ORDER BY streak DESC"""
            ).fetchall()
        if not rows:
            return "No habits defined yet."
        return "\n".join(f"🔥 {r['name']}: {r['streak']}d" for r in rows)

    return f"Unknown command: {cmd}. Send /help"


def poll_once(token: str, offset: dict) -> dict:
    """Fetch updates; returns new offset."""
    try:
        data = call("getUpdates", token, timeout=30, offset=offset.get("offset", 0), allowed_updates=["message"])
    except requests.exceptions.Timeout:
        return offset
    except Exception as e:
        print(f"[poll error] {e}")
        time.sleep(5)
        return offset

    result = data.get("result", [])
    for upd in result:
        msg = upd.get("message", {})
        chat_id = msg.get("chat", {}).get("id")
        text = msg.get("text", "")
        if text and chat_id:
            print(f"[msg] {chat_id}: {text}")
            reply(token, chat_id, handle(chat_id, text))
        offset["offset"] = upd["update_id"] + 1
    return offset


def main():
    ap = argparse.ArgumentParser(description="LIFEOS Telegram bot")
    ap.add_argument("--token", help="Bot token (overrides DB config)")
    ap.add_argument("--chat", help="Allowed chat id (overrides DB config)")
    ap.add_argument("--once", action="store_true", help="Poll once and exit (for testing)")
    args = ap.parse_args()

    token, chat_id = get_config()
    if args.token:
        token = args.token
    if args.chat:
        chat_id = args.chat

    if not token:
        print("No bot token configured. Set it in the webapp Telegram tab or via --token.")
        return 1

    print(f"[LIFEOS bot] polling with token {token[:8]}... (Ctrl+C to stop)")
    offset = {"offset": 0}

    if args.once:
        poll_once(token, offset)
        return 0

    while True:
        offset = poll_once(token, offset)


if __name__ == "__main__":
    sys.exit(main())