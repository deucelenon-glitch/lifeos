from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from app.plugins.base import LifeOSPlugin
from datetime import datetime, timedelta
import sqlite3

class P2PPlugin(LifeOSPlugin):
    """RoboSats order reminder tracker.

    Reminds you to create a P2P order (amount in a configurable range, default 50-500 sats)
    at a configurable interval. No trading logic — just nudge + log.
    """

    @property
    def name(self) -> str:
        return "p2p"

    def init_tables(self, conn: sqlite3.Connection):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS p2p_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                interval_minutes INTEGER DEFAULT 480,   -- 8h
                min_sats INTEGER DEFAULT 50,
                max_sats INTEGER DEFAULT 500,
                enabled INTEGER DEFAULT 1,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS p2p_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                amount_sats INTEGER NOT NULL,
                side TEXT DEFAULT 'buy',        -- buy | sell
                note TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Seed default config
        row = conn.execute("SELECT COUNT(*) FROM p2p_config").fetchone()
        if not row or row[0] == 0:
            conn.execute("INSERT INTO p2p_config (interval_minutes, min_sats, max_sats) VALUES (480, 50, 500)")

    def register_routes(self) -> APIRouter:
        router = APIRouter()

        @router.get("/view", response_class=HTMLResponse)
        def p2p_view(request: Request):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                cfg = conn.execute("SELECT * FROM p2p_config ORDER BY id DESC LIMIT 1").fetchone()
                last = conn.execute("SELECT * FROM p2p_orders ORDER BY id DESC LIMIT 1").fetchone()
                orders = conn.execute("SELECT * FROM p2p_orders ORDER BY id DESC LIMIT 10").fetchall()

            interval = cfg["interval_minutes"] if cfg else 480
            status_html, next_in = self._status(cfg, last)

            html = f"""
            <div class='space-y-4'>
                <!-- Status card -->
                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4'>
                    <div class='flex justify-between items-start'>
                        <div>
                            <h3 class='font-bold text-white text-sm'>RoboSats Order Reminder</h3>
                            <p class='text-xs text-slate-400 mt-0.5'>{status_html}</p>
                            <p class='text-xs text-slate-500 mt-0.5'>Next reminder: <span class='font-mono text-emerald-400'>{next_in}</span></p>
                        </div>
                        <span class='text-xs px-2 py-1 rounded-full bg-emerald-600/20 text-emerald-400 font-mono'>{"ON" if cfg and cfg['enabled'] else "OFF"}</span>
                    </div>
                </div>

                <!-- Config form -->
                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4'>
                    <h4 class='font-semibold text-white text-xs uppercase mb-3'>Reminder Schedule</h4>
                    <form hx-post='/api/p2p/config' hx-target='#p2p-area' hx-swap='outerHTML' @submit="toast = 'P2P reminder updated!'" class='space-y-3'>
                        <div class='grid grid-cols-3 gap-2'>
                            <div>
                                <label class='block text-[10px] text-slate-500 mb-1'>Interval (min)</label>
                                <input type='number' name='interval_minutes' value='{interval if cfg else 480}' min='5' step='5'
                                       class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-center text-sm focus:outline-none'>
                            </div>
                            <div>
                                <label class='block text-[10px] text-slate-500 mb-1'>Min sats</label>
                                <input type='number' name='min_sats' value='{cfg['min_sats'] if cfg else 50}' min='1'
                                       class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-center text-sm focus:outline-none'>
                            </div>
                            <div>
                                <label class='block text-[10px] text-slate-500 mb-1'>Max sats</label>
                                <input type='number' name='max_sats' value='{cfg['max_sats'] if cfg else 500}' min='1'
                                       class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-center text-sm focus:outline-none'>
                            </div>
                        </div>
                        <div class='flex gap-2 items-center'>
                            <button type='submit' class='flex-1 bg-emerald-600 hover:bg-emerald-500 text-white font-medium py-2 rounded-xl text-sm transition'>Update Schedule</button>
                            <button type='button' hx-post='/api/p2p/toggle' hx-target='#p2p-area' hx-swap='outerHTML'
                                    class='px-4 py-2 rounded-xl bg-dark-800 hover:bg-dark-700 text-slate-300 text-sm'>{'Pause' if cfg and cfg['enabled'] else 'Resume'}</button>
                        </div>
                    </form>
                </div>

                <!-- Log order -->
                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4'>
                    <h4 class='font-semibold text-white text-xs uppercase mb-3'>Log Order Posted</h4>
                    <form hx-post='/api/p2p/orders' hx-target='#p2p-area' hx-swap='outerHTML' @submit="toast = 'Order logged!'" class='space-y-2'>
                        <div class='grid grid-cols-3 gap-2'>
                            <input type='number' name='amount_sats' placeholder='sats' required
                                   class='bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-center text-sm placeholder-slate-500'>
                            <select name='side' class='bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm'>
                                <option value='buy'>Buy</option>
                                <option value='sell'>Sell</option>
                            </select>
                            <input type='text' name='note' placeholder='note'
                                   class='bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm placeholder-slate-500'>
                        </div>
                        <button type='submit' class='w-full bg-emerald-600 hover:bg-emerald-500 text-white font-medium py-2 rounded-xl text-sm'>✓ Post Order</button>
                    </form>
                </div>

                <!-- Recent orders -->
                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4'>
                    <h4 class='font-semibold text-white text-xs uppercase mb-2'>Recent Orders</h4>
                    {"".join(
                        f"<div class='flex justify-between text-xs py-1.5'><span class='font-mono text-emerald-400'>{o['amount_sats']} sats</span>"
                        f"<span class='text-slate-400'>{o['side']}</span><span class='text-slate-500 font-mono'>{o['created_at']}</span></div>"
                        for o in orders
                    ) if orders else "<p class='text-slate-500 text-xs py-2'>No orders logged yet.</p>"}
                </div>
            </div>
            """
            return html

        @router.post("/config", response_class=HTMLResponse)
        def save_config(
            request: Request,
            interval_minutes: int = Form(...),
            min_sats: int = Form(...),
            max_sats: int = Form(...),
        ):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                conn.execute("DELETE FROM p2p_config")
                conn.execute(
                    "INSERT INTO p2p_config (interval_minutes, min_sats, max_sats, enabled) VALUES (?, ?, ?, 1)",
                    (interval_minutes, min_sats, max_sats),
                )
            return p2p_view(request)

        @router.post("/toggle", response_class=HTMLResponse)
        def toggle(request: Request):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                conn.execute("UPDATE p2p_config SET enabled = 1 - enabled WHERE id = (SELECT MAX(id) FROM p2p_config)")
            return p2p_view(request)

        @router.post("/orders", response_class=HTMLResponse)
        def log_order(request: Request, amount_sats: int = Form(...), side: str = Form("buy"), note: str = Form("")):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                conn.execute(
                    "INSERT INTO p2p_orders (amount_sats, side, note) VALUES (?, ?, ?)",
                    (amount_sats, side, note),
                )
            return p2p_view(request)

        # JSON API
        @router.get("/status")
        def status_api():
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                cfg = conn.execute("SELECT * FROM p2p_config ORDER BY id DESC LIMIT 1").fetchone()
                last = conn.execute("SELECT * FROM p2p_orders ORDER BY id DESC LIMIT 1").fetchone()
            due, next_in = self._status(cfg, last)
            return {
                "enabled": bool(cfg and cfg["enabled"]),
                "interval_minutes": cfg["interval_minutes"] if cfg else 480,
                "min_sats": cfg["min_sats"] if cfg else 50,
                "max_sats": cfg["max_sats"] if cfg else 500,
                "due": due,
                "next_in": next_in,
            }

        return router

    def _status(self, cfg, last) -> tuple:
        """Return (status_text, next_reminder_text)."""
        if not cfg or not cfg["enabled"]:
            return "Reminder paused.", "—"
        interval = cfg["interval_minutes"]
        if not last:
            return "No orders yet — time to post your first one!", "now 🎯"
        created = datetime.fromisoformat(last["created_at"].replace("Z", "")) if isinstance(last["created_at"], str) else last["created_at"]
        elapsed = datetime.now() - created
        remaining = timedelta(minutes=interval) - elapsed
        if remaining.total_seconds() <= 0:
            return f"⏰ Order due! Last was {last['amount_sats']} sats {int(elapsed.total_seconds() // 3600)}h ago.", "NOW ⏰"
        mins = int(remaining.total_seconds() // 60)
        hrs, m = divmod(mins, 60)
        return f"Next order window opens in {hrs}h{m:02d}m.", f"in {hrs}h{m:02d}m"

    def reminder_due(self) -> bool:
        """Called by the periodic worker — True if an order nudge is due."""
        from app.database import Database
        db = Database()
        with db.get_connection() as conn:
            cfg = conn.execute("SELECT * FROM p2p_config ORDER BY id DESC LIMIT 1").fetchone()
            last = conn.execute("SELECT * FROM p2p_orders ORDER BY id DESC LIMIT 1").fetchone()
        if not cfg or not cfg["enabled"]:
            return False
        if not last:
            return True
        created = datetime.fromisoformat(last["created_at"].replace("Z", "")) if isinstance(last["created_at"], str) else last["created_at"]
        return (datetime.now() - created) >= timedelta(minutes=cfg["interval_minutes"])

    def periodic_check(self):
        """Worker calls this periodically; when due, fire a Termux/Telegram nudge."""
        if not self.reminder_due():
            return
        from app.utils.notifications import send_termux, send_telegram
        from app.database import Database
        db = Database()
        with db.get_connection() as conn:
            cfg = conn.execute("SELECT * FROM p2p_config ORDER BY id DESC LIMIT 1").fetchone()
        msg = f"⏰ P2P ORDER DUE — post {cfg['min_sats']}-{cfg['max_sats']} sats on RoboSats"
        send_termux("LifeOS P2P", msg, "urgent")
        try:
            send_telegram(msg)
        except Exception:
            pass

    def menu(self) -> dict:
        return {"id": self.name, "icon": "🛰️", "label": "P2P Order Reminder", "badge": "", "view": "/api/p2p/view"}

    def bot_commands(self) -> dict:
        def cmd_p2p(chat_id, parts):
            from app.database import Database
            db = Database()
            with db.get_connection() as conn:
                cfg = conn.execute("SELECT * FROM p2p_config ORDER BY id DESC LIMIT 1").fetchone()
            due, next_in = self._status(cfg, self._last_order())
            return (f"*P2P Reminder*\n"
                    f"Interval: {cfg['interval_minutes']} min\n"
                    f"Range: {cfg['min_sats']}-{cfg['max_sats']} sats\n"
                    f"Status: {'ON' if cfg['enabled'] else 'OFF'}\n"
                    f"Next: {next_in}")

        def cmd_log_order(chat_id, parts):
            # /order 300 buy   or   /order 150
            if len(parts) < 2:
                return "Usage: /order <sats> [buy|sell] [note]"
            try:
                amount = int(parts[1])
            except ValueError:
                return "Amount must be a number: /order 300 buy"
            side = parts[2].lower() if len(parts) >= 3 and parts[2].lower() in ("buy", "sell") else "buy"
            note = " ".join(parts[3:])
            from app.database import Database
            db = Database()
            with db.get_connection() as conn:
                conn.execute("INSERT INTO p2p_orders (amount_sats, side, note) VALUES (?, ?, ?)", (amount, side, note))
            return f"✅ Order logged: {amount} sats ({side})"

        return {"p2p": cmd_p2p, "order": cmd_log_order}

    def _last_order(self):
        from app.database import Database
        db = Database()
        with db.get_connection() as conn:
            return conn.execute("SELECT * FROM p2p_orders ORDER BY id DESC LIMIT 1").fetchone()

    def get_dashboard_widgets(self) -> list:
        return []