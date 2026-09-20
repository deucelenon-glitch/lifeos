from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from app.plugins.base import LifeOSPlugin
from datetime import datetime, timedelta
import sqlite3


def _round2(x: float) -> float:
    return round(float(x) + 1e-9, 2)


class P2PPlugin(LifeOSPlugin):
    """USDT arbitrage ledger — P2P/OTC profit tracker.

    Sheet math (user spreadsheet):
        buy_usdt    = receive_eur * rate
        profit_usd  = buy_usdt - sent_usdt
    User sets the rate once, enters EUR received + USDT sent, everything else
    is computed. Profit auto-feeds the Money/Cashflow plugin as daily income.
    """

    @property
    def name(self) -> str:
        return "p2p"

    def init_tables(self, conn: sqlite3.Connection):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS p2p_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                interval_minutes INTEGER DEFAULT 480,
                rate REAL DEFAULT 1.15,
                min_sats INTEGER DEFAULT 50,
                max_sats INTEGER DEFAULT 500,
                enabled INTEGER DEFAULT 1,
                last_nudged_at DATETIME,
                reminder_time TEXT,
                order_size REAL,
                monthly_goal REAL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS p2p_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                receive_eur REAL NOT NULL DEFAULT 0,
                buy_usdt REAL NOT NULL DEFAULT 0,
                sent_usdt REAL NOT NULL DEFAULT 0,
                profit_usd REAL NOT NULL DEFAULT 0,
                rate REAL DEFAULT 1.15,
                side TEXT DEFAULT 'buy',
                note TEXT,
                trade_date TEXT DEFAULT (date('now')),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cols = [c[1] for c in conn.execute("PRAGMA table_info(p2p_orders)").fetchall()]
        if "trade_date" not in cols:
            conn.execute("ALTER TABLE p2p_orders ADD COLUMN trade_date TEXT")
            conn.execute("UPDATE p2p_orders SET trade_date = substr(created_at, 1, 10) WHERE trade_date IS NULL OR trade_date = ''")
        if "amount_sats" in cols and "receive_eur" not in cols:
            # Legacy schema -> migrate preserving data
            conn.execute("ALTER TABLE p2p_orders RENAME TO p2p_orders_legacy")
            conn.execute("""
                CREATE TABLE p2p_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receive_eur REAL NOT NULL DEFAULT 0,
                    buy_usdt REAL NOT NULL DEFAULT 0,
                    sent_usdt REAL NOT NULL DEFAULT 0,
                    profit_usd REAL NOT NULL DEFAULT 0,
                    rate REAL DEFAULT 1.15,
                    side TEXT DEFAULT 'buy',
                    note TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                INSERT INTO p2p_orders (receive_eur, buy_usdt, sent_usdt, profit_usd, rate, side, note, created_at)
                SELECT amount_sats, amount_sats * 1.15, amount_sats, 0, 1.15, side, note, created_at
                FROM p2p_orders_legacy
            """)
            conn.execute("DROP TABLE p2p_orders_legacy")
            conn.commit()

        cols = [c[1] for c in conn.execute("PRAGMA table_info(p2p_config)").fetchall()]
        if "rate" not in cols:
            conn.execute("ALTER TABLE p2p_config ADD COLUMN rate REAL DEFAULT 1.15")
            conn.commit()
        if "reminder_time" not in cols:
            conn.execute("ALTER TABLE p2p_config ADD COLUMN reminder_time TEXT")
        if "order_size" not in cols:
            conn.execute("ALTER TABLE p2p_config ADD COLUMN order_size REAL")
        if "monthly_goal" not in cols:
            conn.execute("ALTER TABLE p2p_config ADD COLUMN monthly_goal REAL")
        conn.commit()

        row = conn.execute("SELECT COUNT(*) FROM p2p_config").fetchone()
        if not row or row[0] == 0:
            conn.execute("INSERT INTO p2p_config (interval_minutes, rate, min_sats, max_sats) VALUES (480, 1.15, 50, 500)")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _cfg(self, conn):
        return conn.execute("SELECT * FROM p2p_config ORDER BY id DESC LIMIT 1").fetchone()

    def _rate(self, conn) -> float:
        cfg = self._cfg(conn)
        return float(cfg["rate"]) if cfg and cfg["rate"] else 1.15

    def _orders(self, conn, limit: int = 200):
        return conn.execute("SELECT * FROM p2p_orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def _totals(self, conn):
        row = conn.execute("""
            SELECT COALESCE(SUM(receive_eur),0) eur,
                   COALESCE(SUM(buy_usdt),0)    buy,
                   COALESCE(SUM(sent_usdt),0)   sent,
                   COALESCE(SUM(profit_usd),0)  profit
            FROM p2p_orders
        """).fetchone()
        return {
            "eur": _round2(row["eur"]),
            "buy": _round2(row["buy"]),
            "sent": _round2(row["sent"]),
            "profit": _round2(row["profit"]),
        }

    def _push_income_into_money(self, profit_usd: float, order_id: int = None):
        """Link: P2P profit feeds the Money plugin as today's income (USD).
        ref_id = order id so deleting a trade also removes its income entry."""
        if not profit_usd:
            return None
        try:
            from app.plugins.money import MoneyPlugin
            return MoneyPlugin().record_income(profit_usd, source="p2p", note="P2P profit", ref_id=order_id)
        except Exception as e:
            print(f"[p2p] income push skipped: {e}")
            return None

    # ------------------------------------------------------------------
    # routes
    # ------------------------------------------------------------------
    def register_routes(self) -> APIRouter:
        router = APIRouter()

        @router.get("/view", response_class=HTMLResponse)
        def p2p_view(request: Request):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                cfg = self._cfg(conn)
                rate = self._rate(conn)
                orders = self._orders(conn, 200)
                totals = self._totals(conn)

            order_size = cfg["order_size"] or 0
            reminder_time = cfg["reminder_time"] or ""
            monthly_goal = cfg["monthly_goal"] or 0

            # Monthly progress toward the P2P order goal (EUR received this month)
            month_prefix = datetime.now().strftime("%Y-%m")
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                mrow = conn.execute(
                    "SELECT COALESCE(SUM(receive_eur),0) s, COUNT(*) c FROM p2p_orders WHERE strftime('%Y-%m', COALESCE(trade_date, date('now'))) = ?",
                    (month_prefix,),
                ).fetchone()
            month_eur = _round2(mrow["s"] or 0)
            month_orders = mrow["c"] or 0

            # Projections: hit the monthly goal every month → 6-month / 1-year
            proj_6 = _round2(monthly_goal * 6)
            proj_12 = _round2(monthly_goal * 12)
            pct = min(100.0, (month_eur / monthly_goal * 100) if monthly_goal > 0 else 0.0)
            goal_left = _round2(max(0.0, monthly_goal - month_eur))

            # Reminder options every €50
            size_opts = "".join(
                f"<option value='{v}' {'selected' if order_size == v else ''}>€{v}</option>"
                for v in range(50, 1001, 50)
            )

            rows_html = ""
            for o in orders:
                profit = _round2(o["profit_usd"])
                cls = "text-emerald-400" if profit >= 0 else "text-red-400"
                rows_html += f"""
                <tr class='border-b border-dark-800/60'>
                    <td class='px-2 py-1.5 text-slate-500 font-mono'>{o['trade_date'] or str(o['created_at'])[:10]}</td>
                    <td class='px-2 py-1.5 text-slate-400 font-mono'>{o['note'] or ''}</td>
                    <td class='px-2 py-1.5 text-white font-mono text-right'>{o['receive_eur']:.2f}</td>
                    <td class='px-2 py-1.5 text-emerald-300 font-mono text-right'>{o['buy_usdt']:.2f}</td>
                    <td class='px-2 py-1.5 text-amber-300 font-mono text-right'>{o['sent_usdt']:.2f}</td>
                    <td class='px-2 py-1.5 {cls} font-mono text-right'>{'+' if profit >= 0 else ''}{profit:.2f}</td>
                    <td class='px-2 py-1.5 text-slate-500 font-mono text-right'>{o['rate']:.3f}</td>
                    <td class='px-2 py-1.5'>
                        <button hx-delete='/api/p2p/orders/{o['id']}' hx-target='#p2p-area' hx-swap='outerHTML'
                                class='text-slate-500 hover:text-red-400 text-xs'>✕</button>
                    </td>
                </tr>"""

            html = f"""
            <div class='space-y-4'>
                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4 grid grid-cols-2 md:grid-cols-5 gap-3'>
                    <div>
                        <div class='text-[10px] uppercase font-mono text-slate-400'>Live Rate</div>
                        <div class='text-lg font-bold text-white font-mono'>{rate:.3f}</div>
                        <form hx-post='/api/p2p/config' hx-target='#p2p-area' hx-swap='outerHTML' class='flex mt-1'>
                            <input type='number' step='0.001' name='rate' value='{rate:.3f}' min='0.5' max='3'
                                   class='w-full bg-dark-950 border border-dark-800 rounded-lg px-1.5 py-1 text-white text-xs font-mono text-center'>
                            <button type='submit' class='bg-dark-800 hover:bg-dark-700 text-emerald-400 text-xs px-2 rounded-lg shrink-0'>set</button>
                        </form>
                    </div>
                    <div>
                        <div class='text-[10px] uppercase font-mono text-slate-400'>Total Profit</div>
                        <div class='text-lg font-bold {"text-emerald-400" if totals["profit"] >= 0 else "text-red-400"} font-mono'>${totals['profit']:.2f}</div>
                        <div class='text-[10px] text-slate-500'>{len(orders)} trades</div>
                    </div>
                    <div>
                        <div class='text-[10px] uppercase font-mono text-slate-400'>EUR In</div>
                        <div class='text-lg font-bold text-white font-mono'>€{totals['eur']:.2f}</div>
                        <div class='text-[10px] text-slate-500'>→ {totals['buy']:.2f} USDT</div>
                    </div>
                    <div>
                        <div class='text-[10px] uppercase font-mono text-slate-400'>USDT Sent</div>
                        <div class='text-lg font-bold text-white font-mono'>{totals['sent']:.2f}</div>
                        <div class='text-[10px] text-slate-500'>spread {totals['buy'] - totals['sent']:.2f}</div>
                    </div>
                    <div>
                        <div class='text-[10px] uppercase font-mono text-slate-400'>Month vs Goal</div>
                        <div class='text-lg font-bold {"text-emerald-400" if monthly_goal and month_eur >= monthly_goal else "text-amber-400"} font-mono'>€{month_eur:.2f}</div>
                        <div class='text-[10px] text-slate-500'>/ €{monthly_goal:.0f} goal · {month_orders} orders</div>
                    </div>
                </div>

                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4'>
                    <h4 class='font-semibold text-white text-xs uppercase mb-3'>⏰ Order Reminder</h4>
                    <form hx-post='/api/p2p/config' hx-target='#p2p-area' hx-swap='outerHTML'
                          @submit="toast = 'Reminder saved ✓ — notified daily at the set time'"
                          class='grid grid-cols-2 md:grid-cols-5 gap-2 items-end'>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Order size</label>
                            <select name='order_size' class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm font-mono'>
                                {size_opts}
                            </select>
                        </div>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Remind at</label>
                            <input type='time' name='reminder_time' value='{reminder_time}'
                                   class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm font-mono'>
                        </div>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Monthly goal €</label>
                            <input type='number' step='50' name='monthly_goal' value='{monthly_goal:.0f}' min='0'
                                   class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm font-mono'>
                        </div>
                        <div class='bg-dark-950 rounded-lg px-2 py-2 text-center'>
                            <div class='text-[10px] uppercase font-mono text-slate-400'>Progress</div>
                            <span class='{"text-emerald-400" if monthly_goal and pct >= 100 else "text-amber-400"} font-mono text-sm font-bold'>{pct:.0f}%</span>
                            <div class='text-[10px] text-slate-500'>€{goal_left:.0f} left</div>
                        </div>
                        <button type='submit' class='w-full bg-emerald-600 hover:bg-emerald-500 text-white font-medium py-2 rounded-xl text-sm'>Save</button>
                    </form>
                    <div class='mt-2.5 flex gap-2 items-center text-xs'>
                        <span class='text-slate-400'>📈 Projection:</span>
                        <span class='px-2 py-1 rounded bg-dark-800 font-mono text-slate-200'>6mo → <b class='text-emerald-400'>€{proj_6:,.0f}</b></span>
                        <span class='px-2 py-1 rounded bg-dark-800 font-mono text-slate-200'>1yr → <b class='text-emerald-400'>€{proj_12:,.0f}</b></span>
                        <span class='text-slate-500'>at €{monthly_goal:.0f}/mo × {_round2(monthly_goal / order_size) if order_size else 0:.0f} orders</span>
                    </div>
                </div>

                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4'>
                    <h4 class='font-semibold text-white text-xs uppercase mb-3'>+ Log Trade</h4>
                    <form hx-post='/api/p2p/orders' hx-target='#p2p-area' hx-swap='outerHTML'
                          @submit="toast = 'Trade logged ✓ — profit auto-fed to Money'"
                          class='grid grid-cols-2 md:grid-cols-5 gap-2 items-end'>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Receive EUR</label>
                            <input type='number' step='0.01' name='receive_eur' placeholder='50' required
                                   class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm font-mono text-center' id='p2p-receive'>
                        </div>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Sent USDT</label>
                            <input type='number' step='0.01' name='sent_usdt' placeholder='53.5' required
                                   class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm font-mono text-center' id='p2p-sent'>
                        </div>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Note (owner)</label>
                            <input type='text' name='note' placeholder='dex' value='dex'
                                   class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm w-32'>
                        </div>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Date</label>
                            <input type='date' name='trade_date'
                                   class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm font-mono'>
                        </div>
                        <div class='bg-dark-950 rounded-lg px-2 py-2 text-center'>
                            <div class='text-[10px] uppercase font-mono text-slate-400'>Buy USDT</div>
                            <span class='text-emerald-300 font-mono text-sm font-bold' id='p2p-buy-preview'>—</span>
                        </div>
                        <div class='bg-dark-950 rounded-lg px-2 py-2 text-center'>
                            <div class='text-[10px] uppercase font-mono text-slate-400'>Profit</div>
                            <span class='text-emerald-300 font-mono text-sm font-bold' id='p2p-profit-preview'>—</span>
                        </div>
                        <button type='submit' class='col-span-2 md:col-span-5 w-full bg-emerald-600 hover:bg-emerald-500 text-white font-medium py-2 rounded-xl text-sm mt-1'>✓ Log Trade (rate {rate:.3f})</button>
                    </form>
                    <script>
                        (function () {{
                            const rate = {rate};
                            const recv = document.getElementById('p2p-receive');
                            const sent = document.getElementById('p2p-sent');
                            const buyP = document.getElementById('p2p-buy-preview');
                            const profP = document.getElementById('p2p-profit-preview');
                            const up = () => {{
                                const r = parseFloat(recv.value) || 0;
                                const s = parseFloat(sent.value) || 0;
                                const b = r * rate;
                                buyP.textContent = b.toFixed(2);
                                const p = b - s;
                                profP.textContent = (p >= 0 ? '+' : '') + p.toFixed(2);
                                profP.className = p >= 0 ? 'text-emerald-300 font-mono text-sm font-bold' : 'text-red-400 font-mono text-sm font-bold';
                            }};
                            recv.oninput = up; sent.oninput = up;
                        }})();
                    </script>
                </div>

                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4 overflow-x-auto'>
                    <h4 class='font-semibold text-white text-xs uppercase mb-2'>Ledger ({len(orders)})</h4>
                    <table class='w-full text-xs'>
                        <thead>
                            <tr class='text-slate-500 uppercase font-mono text-[10px] border-b border-dark-700'>
                                <th class='px-2 py-1.5 text-left'>date</th>
                                <th class='px-2 py-1.5 text-left'>owner</th>
                                <th class='px-2 py-1.5 text-right'>Recv €</th>
                                <th class='px-2 py-1.5 text-right'>Buy USDT</th>
                                <th class='px-2 py-1.5 text-right'>Sent USDT</th>
                                <th class='px-2 py-1.5 text-right'>Profit $</th>
                                <th class='px-2 py-1.5 text-right'>rate</th>
                                <th class='px-2 py-1.5'></th>
                            </tr>
                        </thead>
                        <tbody>
                            {rows_html if rows_html else "<tr><td colspan='8' class='px-2 py-4 text-slate-500 text-center'>No trades yet — log your first one above.</td></tr>"}
                        </tbody>
                    </table>
                    <div class='text-[10px] text-slate-500 mt-1 font-mono'>profit = (receive × rate) − sent · profits feed 💰 Money as daily income</div>
                </div>
            </div>
            """
            return html

        @router.post("/config", response_class=HTMLResponse)
        def save_config(
            request: Request,
            rate: float = Form(...),
            order_size: float = Form(0),
            reminder_time: str = Form(""),
            monthly_goal: float = Form(0),
        ):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                cfg = self._cfg(conn)
                reminder_time = reminder_time.strip()
                if cfg:
                    conn.execute(
                        "UPDATE p2p_config SET rate = ?, order_size = ?, reminder_time = ?, monthly_goal = ? WHERE id = ?",
                        (rate, order_size or None, reminder_time or None, monthly_goal or None, cfg["id"]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO p2p_config (interval_minutes, rate, order_size, reminder_time, monthly_goal) VALUES (480, ?, ?, ?, ?)",
                        (rate, order_size or None, reminder_time or None, monthly_goal or None),
                    )
            return p2p_view(request)

        @router.post("/orders", response_class=HTMLResponse)
        def log_order(
            request: Request,
            receive_eur: float = Form(...),
            sent_usdt: float = Form(0),
            note: str = Form(""),
            trade_date: str = Form(""),
        ):
            from app.database import db as global_db
            trade_date = trade_date.strip() or None
            with global_db.get_connection() as conn:
                live_rate = self._rate(conn)
                buy = _round2(receive_eur * live_rate)
                profit = _round2(buy - sent_usdt)
                cur = conn.execute(
                    "INSERT INTO p2p_orders (receive_eur, buy_usdt, sent_usdt, profit_usd, rate, note, trade_date) VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, date('now')))",
                    (receive_eur, buy, sent_usdt, profit, live_rate, note, trade_date),
                )
                order_id = cur.lastrowid
            self._push_income_into_money(profit, order_id=order_id)

        @router.delete("/orders/{order_id}", response_class=HTMLResponse)
        def delete_order(request: Request, order_id: int):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                conn.execute("DELETE FROM p2p_orders WHERE id = ?", (order_id,))
            self._unpush_income_from_money(order_id)
            return p2p_view(request)

        @router.get("/data")
        def orders_api():
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                return {
                    "rate": self._rate(conn),
                    "orders": [dict(r) for r in self._orders(conn, 500)],
                    "totals": self._totals(conn),
                }

        return router

    def _unpush_income_from_money(self, order_id: int):
        """On delete: remove the linked income entry so Money stays consistent."""
        try:
            from app.plugins.money import MoneyPlugin
            MoneyPlugin().remove_income_ref(order_id, source="p2p")
        except Exception as e:
            print(f"[p2p] income unlink skipped: {e}")

    def _status(self, cfg, last) -> tuple:
        return "Ledger active — log trades above.", "—"

    def menu(self) -> dict:
        return {"id": self.name, "icon": "🛰️", "label": "P2P Ledger", "badge": "", "view": "/api/p2p/view"}

    def bot_commands(self) -> dict:
        def cmd_p2p(chat_id, parts):
            from app.database import Database
            db = Database()
            with db.get_connection() as conn:
                t = self._totals(conn)
                n = conn.execute("SELECT COUNT(*) c FROM p2p_orders").fetchone()["c"]
            return (f"*P2P Ledger*\n"
                    f"Trades: {n}\n"
                    f"Profit: ${t['profit']:.2f}\n"
                    f"EUR in: €{t['eur']:.2f} → {t['buy']:.2f} USDT")

        def cmd_order(chat_id, parts):
            # /order <receive_eur> <sent_usdt> [note]
            if len(parts) < 3:
                return "Usage: /order <receive_eur> <sent_usdt> [note]  →  e.g. /order 50 53.5 dex"
            try:
                recv = float(parts[1]); sent = float(parts[2])
            except ValueError:
                return "Amounts must be numbers: /order 50 53.5"
            note = " ".join(parts[3:])
            from app.database import Database
            db = Database()
            with db.get_connection() as conn:
                rate = self._rate(conn)
                buy = _round2(recv * rate)
                profit = _round2(buy - sent)
                conn.execute(
                    "INSERT INTO p2p_orders (receive_eur, buy_usdt, sent_usdt, profit_usd, rate, note) VALUES (?, ?, ?, ?, ?, ?)",
                    (recv, buy, sent, profit, rate, note),
                )
            self._push_income_into_money(profit)
            return f"✅ Logged: €{recv:.2f} @ {rate:.3f} → {buy:.2f} USDT, sent {sent:.2f}, profit ${profit:.2f}"

        return {"p2p": cmd_p2p, "order": cmd_order}

    def periodic_check(self):
        """Nothing time-critical for the ledger; kept for worker compat."""
        return False

    def get_dashboard_widgets(self) -> list:
        return []
