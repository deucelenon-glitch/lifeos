from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from app.plugins.base import LifeOSPlugin
from datetime import datetime, timezone
import sqlite3
import json
import urllib.request


def _round2(x: float) -> float:
    return round(float(x) + 1e-9, 2)


# Platforms this tracker knows about, with their default currency.
PLATFORMS = {
    "bybit":      {"label": "Bybit",      "icon": "📈", "currency": "USD"},
    "phoenix":    {"label": "Phoenix",    "icon": "🔥", "currency": "USD"},
    "mexc":       {"label": "MEXC",       "icon": "🧪", "currency": "USD"},
    "unicredit":  {"label": "UniCredit",  "icon": "🏦", "currency": "EUR"},
    "wise":       {"label": "Wise",       "icon": "🌍", "currency": "EUR"},
}


def _p2p_rate() -> float:
    """Use the rate the user set in the P2P tab when it exists.

    The P2P rate is the user's own EUR→USDT reference (set in the P2P tab),
    so it's the most accurate EUR→USD conversion for the capital tracker.
    Returns 0.0 when unset so callers fall back to live FX.
    """
    try:
        from app.database import db as global_db
        with global_db.get_connection() as conn:
            row = conn.execute("SELECT rate FROM p2p_config ORDER BY id DESC LIMIT 1").fetchone()
        if row and row["rate"]:
            return float(row["rate"])
    except Exception as e:
        print(f"[capital] p2p rate read failed ({e})")
    return 0.0


def _fx_eur_to_usd() -> float:
    """EUR→USD rate: user's P2P rate if set, otherwise live mid rate.
    Live via open.er-api.com; falls back to 1.08 offline."""
    p2p = _p2p_rate()
    if p2p:
        return p2p
    try:
        req = urllib.request.Request(
            "https://open.er-api.com/v6/latest/EUR",
            headers={"User-Agent": "lifeos-capital-tracker/1.0"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        if data.get("result") == "success":
            return float(data["rates"]["USD"])
    except Exception as e:
        print(f"[capital] FX fetch failed ({e}) — using cached/fallback rate")
    return 1.08


class CapitalPlugin(LifeOSPlugin):
    """Capital tracker — balances across Bybit / Phoenix / MEXC / UniCredit / Wise.

    Every account stores a native balance (EUR for UniCredit & Wise, USD for the
    exchanges). The dashboard sums EUR and USD separately, then shows a
    combined total in EUR and USD using the live EUR→USD rate.
    """

    @property
    def name(self) -> str:
        return "capital"

    def init_tables(self, conn: sqlite3.Connection):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS capital_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,           -- bybit | phoenix | mexc | unicredit | wise
                label TEXT,                       -- optional custom label
                balance REAL NOT NULL DEFAULT 0,  -- stored in the platform's native currency
                currency TEXT NOT NULL DEFAULT 'USD',
                note TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migration: add currency column if missing
        cols = [c[1] for c in conn.execute("PRAGMA table_info(capital_accounts)").fetchall()]
        if "currency" not in cols:
            conn.execute("ALTER TABLE capital_accounts ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD'")
        if "label" not in cols:
            conn.execute("ALTER TABLE capital_accounts ADD COLUMN label TEXT")

        existing = {r[0] for r in conn.execute("SELECT platform FROM capital_accounts")}
        for key, meta in PLATFORMS.items():
            if key not in existing:
                conn.execute(
                    "INSERT INTO capital_accounts (platform, label, balance, currency, note) VALUES (?, ?, 0, ?, 'seed')",
                    (key, meta["label"], meta["currency"]),
                )
        conn.commit()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _accounts(self, conn) -> list:
        return conn.execute("SELECT * FROM capital_accounts ORDER BY platform, id").fetchall()

    def _sums(self, conn):
        """EUR totals, USD totals, and combined totals (EUR & USD) at P2P/live rate."""
        rows = self._accounts(conn)
        eur = sum((r["balance"] or 0) for r in rows if r["currency"] == "EUR")
        usd = sum((r["balance"] or 0) for r in rows if r["currency"] == "USD")
        eur2usd = _fx_eur_to_usd()
        total_eur = eur + (usd / eur2usd if eur2usd else usd)
        total_usd = usd + (eur * eur2usd if eur2usd else eur)
        return {
            "eur": _round2(eur),
            "usd": _round2(usd),
            "total_eur": _round2(total_eur),
            "total_usd": _round2(total_usd),
            "rate": eur2usd,
        }

    # ------------------------------------------------------------------
    # routes
    # ------------------------------------------------------------------
    def register_routes(self) -> APIRouter:
        router = APIRouter()

        @router.get("/view", response_class=HTMLResponse)
        def capital_view(request: Request):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                accounts = self._accounts(conn)
                sums = self._sums(conn)

            cards = ""
            for a in accounts:
                meta = PLATFORMS.get(a["platform"], {"label": a["platform"], "icon": "🏦", "currency": a["currency"]})
                label = a["label"] or meta["label"]
                sign = "€" if a["currency"] == "EUR" else "$"
                cards += f"""
                <div class='bg-dark-900 border border-dark-700 rounded-xl p-4'>
                    <div class='flex justify-between items-start'>
                        <div>
                            <h4 class='font-semibold text-white text-sm'>{meta['icon']} {label}</h4>
                            <p class='text-[10px] uppercase font-mono text-slate-400 mt-0.5'>{a['platform']} • {a['currency']}</p>
                        </div>
                        <button hx-delete='/api/capital/accounts/{a['id']}' hx-target='#capital-area' hx-swap='outerHTML'
                                class='text-slate-600 hover:text-red-400 text-xs'>✕</button>
                    </div>
                    <div class='mt-2 text-2xl font-bold text-white font-mono'>{sign}{a['balance']:.2f}</div>
                    <form hx-post='/api/capital/accounts/{a['id']}/adjust' hx-target='#capital-area' hx-swap='outerHTML'
                          @submit="toast = '{label} updated ✓'"
                          class='flex gap-1 mt-2'>
                        <input type='number' step='0.01' name='amount' placeholder='+50.00 / -20.00' required
                               class='flex-1 bg-dark-950 border border-dark-800 rounded-lg px-2 py-1.5 text-white text-xs font-mono'>
                        <button class='bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium px-3 py-1.5 rounded-lg'>set</button>
                    </form>
                </div>"""

            html = f"""
            <div id='capital-area'>
            <div class='space-y-4'>
                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4 grid grid-cols-2 md:grid-cols-4 gap-3'>
                    <div>
                        <div class='text-[10px] uppercase font-mono text-slate-400'>💶 EUR</div>
                        <div class='text-lg font-bold text-white font-mono'>€{sums['eur']:.2f}</div>
                        <div class='text-[10px] text-slate-500'>All EUR accounts</div>
                    </div>
                    <div>
                        <div class='text-[10px] uppercase font-mono text-slate-400'>💵 USD</div>
                        <div class='text-lg font-bold text-white font-mono'>${sums['usd']:.2f}</div>
                        <div class='text-[10px] text-slate-500'>All USD accounts</div>
                    </div>
                    <div>
                        <div class='text-[10px] uppercase font-mono text-slate-400'>🌍 Total (EUR)</div>
                        <div class='text-lg font-bold text-emerald-400 font-mono'>€{sums['total_eur']:.2f}</div>
                        <div class='text-[10px] text-slate-500'>@ 1 EUR = ${sums['rate']:.3f}</div>
                    </div>
                    <div>
                        <div class='text-[10px] uppercase font-mono text-slate-400'>🌍 Total (USD)</div>
                        <div class='text-lg font-bold text-emerald-400 font-mono'>${sums['total_usd']:.2f}</div>
                        <div class='text-[10px] text-slate-500'>P2P rate / realtime fx</div>
                    </div>
                </div>

                <div class='grid gap-3 md:grid-cols-2 lg:grid-cols-3'>
                    {cards}
                    <div class='bg-dark-950 border border-dashed border-dark-700 rounded-xl p-4 flex flex-col items-center justify-center text-center'>
                        <div class='text-slate-500 text-xs font-medium mb-2'>➕ New account</div>
                        <form hx-post='/api/capital/accounts' hx-target='#capital-area' hx-swap='outerHTML'
                              @submit="toast = 'Account added ✓'"
                              class='grid grid-cols-2 gap-1 w-full'>
                            <input type='text' name='label' placeholder='Label' required
                                   class='bg-dark-950 border border-dark-800 rounded-lg px-2 py-1.5 text-white text-xs'>
                            <input type='text' name='platform' placeholder='e.g. binance' 
                                   class='bg-dark-950 border border-dark-800 rounded-lg px-2 py-1.5 text-white text-xs'>
                            <select name='currency' class='bg-dark-950 border border-dark-800 rounded-lg px-2 py-1.5 text-white text-xs'>
                                <option value='USD'>USD $</option>
                                <option value='EUR'>EUR €</option>
                            </select>
                            <input type='number' step='0.01' name='balance' placeholder='0.00'
                                   class='bg-dark-950 border border-dark-800 rounded-lg px-2 py-1.5 text-white text-xs'>
                            <button class='bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium px-3 py-1.5 rounded-lg w-full mt-0.5'>Add</button>
                        </form>
                    </div>
                </div>
            </div>
            </div>
            """
            return html

        @router.post("/accounts/{account_id}/adjust", response_class=HTMLResponse)
        def adjust_account(request: Request, account_id: int, amount: float = Form(...)):
            """Relative adjust: +N / -N against the current balance."""
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                conn.execute(
                    "UPDATE capital_accounts SET balance = balance + ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (amount, account_id),
                )
                conn.commit()
            return capital_view(request)

        @router.post("/accounts/{account_id}/set", response_class=HTMLResponse)
        def set_account(request: Request, account_id: int, amount: float = Form(...)):
            """Absolute set: overwrite the balance directly."""
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                conn.execute(
                    "UPDATE capital_accounts SET balance = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (amount, account_id),
                )
                conn.commit()
            return capital_view(request)

        @router.delete("/accounts/{account_id}", response_class=HTMLResponse)
        def delete_account(request: Request, account_id: int):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                conn.execute("DELETE FROM capital_accounts WHERE id = ?", (account_id,))
                conn.commit()
            return capital_view(request)

        @router.post("/accounts", response_class=HTMLResponse)
        def create_account(
            request: Request,
            label: str = Form(...),
            platform: str = Form(""),
            currency: str = Form("USD"),
            balance: float = Form(0.0),
        ):
            """Create a new account, either from a known platform or a custom one."""
            from app.database import db as global_db
            platform = platform.strip().lower()
            meta = PLATFORMS.get(platform)
            if meta:
                # Known platform → use its canonical currency unless user overrode it
                currency = meta["currency"]
                label = label.strip() or meta["label"]
            else:
                # Custom platform → derive platform slug from label if left blank
                platform = platform or label.strip().lower().replace(" ", "_")
                label = label.strip() or platform
            if not platform:
                platform = label.strip().lower().replace(" ", "_")
            with global_db.get_connection() as conn:
                conn.execute(
                    "INSERT INTO capital_accounts (platform, label, balance, currency, note) VALUES (?, ?, ?, ?, 'user')",
                    (platform, label, balance, currency),
                )
                conn.commit()
            return capital_view(request)

        @router.get("/totals")
        def totals_api():
            """JSON totals for CLI / bots / widgets."""
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                sums = self._sums(conn)
            return sums

        return router

    def menu(self) -> dict:
        return {"id": self.name, "icon": "🏦", "label": "Capital", "badge": "", "view": "/api/capital/view"}

    def bot_commands(self) -> dict:
        def cmd_capital(chat_id, parts):
            from app.database import Database
            db = Database()
            with db.get_connection() as conn:
                sums = self._sums(conn)
            return (
                f"💶 EUR: €{sums['eur']:.2f}\n"
                f"💵 USD: ${sums['usd']:.2f}\n"
                f"🌍 {sums['rate']:.3f} → Total €{sums['total_eur']:.2f} / ${sums['total_usd']:.2f}"
            )
        return {"capital": cmd_capital, "balance": cmd_capital}

    def get_dashboard_widgets(self) -> list:
        return []