from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from app.plugins.base import LifeOSPlugin
import datetime
import json
import re
import sqlite3
import subprocess

# ---------------------------------------------------------------------------
# Termux Notification -> Expense auto-collector
#
# Polls `termux-notification-list` (Termux:API) every periodic_check cycle,
# finds payment/bank/card notifications, parses the amount, and logs them as
# expenses automatically (once each — dedup by notification id).
#
# When auto-logging, the expense is deducted from the configured capital
# account (same as the Expenses tab) and the display shows:
#   normal balance  ->  -deducted  ->  new balance
# ---------------------------------------------------------------------------

PAYMENT_APPS = [
    "paypal", "revolut", "n26", "ing", "unicredit", "intesa", "fineco",
    "poste", "bper", "banco", "banca", "satispay", "nexi", "stripe",
    "wise", "transferwise", "sumup", "scalapay", "klarna", "amazon pay",
    "pay", "bank", "curve", "monzo", "bunq", "bbva", "santander",
]

PAYMENT_KEYWORDS = [
    "pagamento", "pagato", "addebito", "accredito", "bonifico", "spesa",
    "transazione", "carta", "acquisto", "rinnovo", "abbonamento", "fattura",
    "payment", "paid", "debit", "credit", "card", "purchase", "charge",
    "invoice", "subscription", "withdrawal", "deposit", "sent you",
    "spent", "refund", "rimborso", "movimento", "disponibile", "saldo",
]

AMOUNT_RE = re.compile(
    r"(?:€|eur|euro)\s*([0-9]{1,7}(?:[.,][0-9]{1,2})?)\b"
    r"|\b([0-9]{1,7}(?:[.,][0-9]{1,2})?)\s*(?:€|eur|euro)\b",
    re.IGNORECASE,
)


class TermuxPlugin(LifeOSPlugin):
    @property
    def name(self) -> str:
        return "termux"

    # ------------------------------------------------------------------
    # schema
    # ------------------------------------------------------------------
    def init_tables(self, conn: sqlite3.Connection):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS termux_scan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                notif_id TEXT UNIQUE,
                package TEXT,
                title TEXT,
                content TEXT,
                amount REAL,
                category TEXT DEFAULT 'auto',
                status TEXT DEFAULT 'captured',  -- captured | logged | skipped | deleted
                expense_id INTEGER,
                source_account TEXT,
                balance_before REAL,
                balance_after REAL,
                seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                logged_at DATETIME
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS termux_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                enabled INTEGER DEFAULT 1,
                auto_log INTEGER DEFAULT 1,
                source_account TEXT DEFAULT 'wise',
                watched TEXT DEFAULT '',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        row = conn.execute("SELECT COUNT(*) FROM termux_config").fetchone()
        if not row or row[0] == 0:
            conn.execute(
                "INSERT INTO termux_config (enabled, auto_log, source_account) VALUES (1, 1, 'wise')"
            )
        cols = [c[1] for c in conn.execute("PRAGMA table_info(termux_scan)").fetchall()]
        if "balance_before" not in cols:
            conn.execute("ALTER TABLE termux_scan ADD COLUMN balance_before REAL")
        if "balance_after" not in cols:
            conn.execute("ALTER TABLE termux_scan ADD COLUMN balance_after REAL")
        if "source_account" not in cols:
            conn.execute("ALTER TABLE termux_scan ADD COLUMN source_account TEXT")
        conn.commit()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _cfg(self, conn):
        return conn.execute("SELECT * FROM termux_config ORDER BY id DESC LIMIT 1").fetchone()

    def _is_payment(self, package: str, title: str, content: str, conn) -> bool:
        blob = f"{title} {content}".lower()
        pkg = (package or "").lower()
        cfg = self._cfg(conn)
        extra = [w.strip().lower() for w in (cfg["watched"] or "").split(",") if w.strip()]
        watched = PAYMENT_APPS + extra
        if any(p in pkg for p in watched if p):
            return True
        return any(kw in blob for kw in PAYMENT_KEYWORDS)

    def _parse_amount(self, text: str):
        """First currency amount found in text (EUR). Returns float or None."""
        if not text:
            return None
        m = AMOUNT_RE.search(text)
        if not m:
            return None
        raw = (m.group(1) or m.group(2) or "").replace(",", ".")
        try:
            val = float(raw)
            return val if val > 0 else None
        except ValueError:
            return None

    def _fetch_notifications(self) -> list:
        try:
            out = subprocess.run(
                ["termux-notification-list"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            data = json.loads(out)
            return data if isinstance(data, list) else []
        except Exception as e:
            print(f"[termux] notification list failed: {e}")
            return []

    def _categorize(self, text: str) -> str:
        t = text.lower()
        for cat, kws in [
            ("food", ["ristorante", "supermercato", "conad", "esselunga", "lidl", "aldi", "coop", "deliveroo", "glovo", "justeat", "uber eats", "mcdonald"]),
            ("transport", ["taxi", "uber", "bolt", "trenitalia", "trenord", "bus", "metro", "parcheggio", "parking", "benzina", "fuel"]),
            ("subscriptions", ["netflix", "spotify", "youtube premium", "icloud", "google one", "chatgpt", "openai", "claude", "prime", "canva", "figma", "adobe"]),
            ("shopping", ["amazon", "ebay", "zalando", "mediaworld", "unieuro", "decathlon", "ikea", "shein", "temu", "aliexpress"]),
            ("utilities", ["enel", "energia", "luce", "gas", "acqua", "telecom", "vodafone", "windtre", "fastweb", "bolletta"]),
            ("health", ["farmacia", "pharmacy", "dottore", "doctor", "ospedale", "dentista"]),
        ]:
            if any(k in t for k in kws):
                return cat
        return "auto"

    def _deduct_and_log(self, conn, amount: float, category: str, note: str,
                        package: str, title: str, dedup_key: str) -> dict:
        """Insert expense + deduct from chosen capital account. Returns summary."""
        cfg = self._cfg(conn)
        platform = (cfg["source_account"] or "wise").strip()
        today = datetime.date.today().isoformat()

        # Look up the capital account first so the expense row can carry
        # balance_before / balance_after (shown in both tabs).
        balance_before = balance_after = None
        acc = conn.execute(
            "SELECT * FROM capital_accounts WHERE platform = ? ORDER BY id LIMIT 1",
            (platform,),
        ).fetchone()
        if acc:
            balance_before = float(acc["balance"])
            from app.plugins.capital import _fx_eur_to_usd
            deduct = amount if acc["currency"] == "EUR" else amount * _fx_eur_to_usd()
            balance_after = max(0.0, balance_before - deduct)

        cur = conn.execute(
            "INSERT INTO expenses (amount, category, note, expense_date, source_account, balance_before, balance_after) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (amount, category, note, today, platform, balance_before, balance_after),
        )
        expense_id = cur.lastrowid

        if acc:
            conn.execute(
                "UPDATE capital_accounts SET balance = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (balance_after, acc["id"]),
            )

        conn.execute(
            """INSERT INTO termux_scan
               (notif_id, package, title, content, amount, category, status,
                expense_id, source_account, balance_before, balance_after, logged_at)
               VALUES (?, ?, ?, ?, ?, ?, 'logged', ?, ?, ?, ?, ?)""",
            (dedup_key, package, title[:200], "", amount, category,
             expense_id, platform, balance_before, balance_after,
             datetime.datetime.now().isoformat(timespec="seconds")),
        )
        return {"logged": 1, "expense_id": expense_id,
                "balance_before": balance_before, "balance_after": balance_after,
                "platform": platform}

    # ------------------------------------------------------------------
    # collection — called by worker every cycle
    # ------------------------------------------------------------------
    def collect_once(self) -> dict:
        from app.database import db as global_db
        with global_db.get_connection() as conn:
            cfg = self._cfg(conn)
            if not cfg or not cfg["enabled"]:
                return {"captured": 0, "logged": 0, "skipped": 0, "disabled": True}

            notifs = self._fetch_notifications()
            summary = {"captured": 0, "logged": 0, "skipped": 0, "disabled": False}

            for n in notifs:
                package = n.get("packageName", "")
                title = n.get("title", "")
                content = n.get("content", "")
                nid = n.get("id")
                dedup_key = f"{package}|{nid}"

                row = conn.execute(
                    "SELECT id, status FROM termux_scan WHERE notif_id = ?", (dedup_key,)
                ).fetchone()
                if row:
                    continue

                is_payment = self._is_payment(package, title, content, conn)
                amount = self._parse_amount(f"{title} {content}") if is_payment else None

                if is_payment and amount is None:
                    conn.execute(
                        "INSERT INTO termux_scan (notif_id, package, title, content, amount, status) VALUES (?, ?, ?, ?, NULL, 'skipped')",
                        (dedup_key, package, title[:200], content[:400]),
                    )
                    summary["skipped"] += 1
                    continue

                if not (is_payment and amount is not None):
                    continue

                summary["captured"] += 1
                category = self._categorize(f"{title} {content}")

                if cfg["auto_log"]:
                    note = f"{package} • {title}".strip(" •")[:120]
                    res = self._deduct_and_log(conn, amount, category, note,
                                               package, title, dedup_key)
                    summary["logged"] += res["logged"]
                else:
                    conn.execute(
                        "INSERT INTO termux_scan (notif_id, package, title, content, amount, category, status) VALUES (?, ?, ?, ?, ?, ?, 'captured')",
                        (dedup_key, package, title[:200], content[:400], amount, category),
                    )

            conn.commit()
            return summary

    # ------------------------------------------------------------------
    # worker compat
    # ------------------------------------------------------------------
    def periodic_check(self):
        from app.database import db as global_db
        try:
            with global_db.get_connection() as conn:
                cfg = self._cfg(conn)
                if not cfg or not cfg["enabled"]:
                    return False
            summary = self.collect_once()
            if summary.get("logged"):
                print(f"[termux] auto-logged {summary['logged']} expense(s) from notifications")
            return bool(summary.get("logged"))
        except Exception as e:
            print(f"[termux] periodic_check error: {e}")
            return False

    def get_dashboard_widgets(self) -> list:
        return []

    # ------------------------------------------------------------------
    # routes
    # ------------------------------------------------------------------
    def register_routes(self) -> APIRouter:
        router = APIRouter()

        @router.get("/view", response_class=HTMLResponse)
        def termux_view(request: Request):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                cfg = self._cfg(conn)
                rows = conn.execute(
                    "SELECT * FROM termux_scan ORDER BY id DESC LIMIT 30"
                ).fetchall()

            enabled = bool(cfg and cfg["enabled"])
            auto_log = bool(cfg and cfg["auto_log"])
            source = (cfg and cfg["source_account"]) or "wise"
            watched = (cfg and cfg["watched"]) or ""

            rows_html = ""
            for r in rows:
                badge_cls = {
                    "logged": "bg-emerald-500/10 text-emerald-400",
                    "captured": "bg-amber-500/10 text-amber-400",
                    "skipped": "bg-dark-700 text-slate-400",
                    "deleted": "bg-dark-700 text-slate-500",
                }.get(r["status"], "bg-dark-700 text-slate-400")
                # Balance movement: normal balance → deducted → new balance
                bal_html = ""
                if r["status"] == "logged" and r["balance_before"] is not None:
                    deduct = r["balance_before"] - (r["balance_after"] or 0)
                    bal_html = (
                        f"<div class='text-[10px] text-slate-500 font-mono mt-0.5'>"
                        f"bal {r['balance_before']:.2f} → <span class='text-red-400'>−{deduct:.2f}</span> → "
                        f"<span class='text-emerald-400'>{r['balance_after']:.2f}</span></div>"
                    )
                rows_html += f"""
                <div class='flex items-start justify-between gap-2 py-2 border-b border-dark-800/60 text-xs'>
                    <div class='min-w-0'>
                        <div class='flex items-center gap-2 flex-wrap'>
                            <span class='font-mono text-slate-500'>{r['package'].split('.')[-1]}</span>
                            <span class='px-1.5 py-0.5 rounded text-[10px] font-mono {badge_cls}'>{r['status']}</span>
                            {"<span class='text-white font-mono font-bold'>€" + f"{r['amount']:.2f}" + "</span>" if r['amount'] else "<span class='text-slate-600'>no amount</span>"}
                            <span class='text-slate-500 font-mono'>{r['seen_at'][:16]}</span>
                        </div>
                        <p class='text-slate-400 truncate mt-0.5'>{r['title'] or ''}{(' — ' + r['content'][:90]) if r['content'] else ''}</p>
                        {bal_html}
                    </div>
                    <button hx-delete='/api/termux/scan/{r['id']}' hx-target='#termux-area' hx-swap='outerHTML'
                            class='text-slate-600 hover:text-red-400 shrink-0'>✕</button>
                </div>"""

            if not rows_html:
                rows_html = "<p class='text-slate-500 py-4 text-center text-xs'>No notifications captured yet — they appear here as the worker scans.</p>"

            # Build the source-account select with live balance hints
            acc_opts = ""
            for platform, label, cur in [
                ("bybit", "Bybit", "USD"), ("phoenix", "Phoenix", "USD"),
                ("mexc", "MEXC", "USD"), ("unicredit", "UniCredit", "EUR"),
                ("wise", "Wise", "EUR"),
            ]:
                sel = " selected" if platform == source else ""
                acc_opts += f"<option value='{platform}'{sel}>{label} ({cur})</option>"

            html = f"""
            <div id='termux-area' hx-get='/api/termux/view' hx-trigger='load'>
            <div class='space-y-4'>
                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4'>
                    <div class='flex justify-between items-center'>
                        <div>
                            <h3 class='font-bold text-white text-sm'>📲 Termux Auto-Expense</h3>
                            <p class='text-xs text-slate-400 mt-0.5'>Reads notifications → detects payments → logs + deducts automatically</p>
                        </div>
                        <span class='text-xs px-2.5 py-1 rounded-full {"bg-emerald-500/10 text-emerald-400" if enabled else "bg-dark-700 text-slate-500"} font-mono'>{'ON' if enabled else 'OFF'}</span>
                    </div>
                </div>

                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4'>
                    <h4 class='font-semibold text-white text-xs uppercase mb-3'>⚙️ Settings</h4>
                    <form hx-post='/api/termux/config' hx-target='#termux-area' hx-swap='outerHTML'
                          @submit="toast = 'Termux settings saved ✓'"
                          class='grid grid-cols-2 md:grid-cols-4 gap-2 items-end'>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Collector</label>
                            <select name='enabled' class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm'>
                                <option value='1' {"selected" if enabled else ""}>Enabled</option>
                                <option value='0' {"selected" if not enabled else ""}>Disabled</option>
                            </select>
                        </div>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Auto-log to expenses</label>
                            <select name='auto_log' class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm'>
                                <option value='1' {"selected" if auto_log else ""}>Auto-log ✓</option>
                                <option value='0' {"selected" if not auto_log else ""}>Capture only</option>
                            </select>
                        </div>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Deduct from</label>
                            <select name='source_account' class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm'>
                                {acc_opts}
                            </select>
                        </div>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Also watch apps</label>
                            <input type='text' name='watched' value='{watched}'
                                   placeholder='com.example.bank, revolut'
                                   class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm'>
                        </div>
                        <button type='submit' class='w-full bg-dark-800 hover:bg-dark-700 text-emerald-400 font-medium py-2 rounded-xl text-sm mt-2'>Save</button>
                    </form>
                    <p class='text-[10px] text-slate-500 mt-1.5 font-mono'>watched by default: {', '.join(PAYMENT_APPS[:8])}… · amounts EUR € · dedup by notification id · expense deducts from the capital account above</p>
                </div>

                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4'>
                    <h4 class='font-semibold text-white text-xs uppercase mb-2'>📥 Captured ({len(rows)})</h4>
                    {rows_html}
                </div>
            </div>
            </div>
            """
            return html

        @router.post("/config", response_class=HTMLResponse)
        def save_config(
            request: Request,
            enabled: int = Form(1),
            auto_log: int = Form(1),
            source_account: str = Form("wise"),
            watched: str = Form(""),
        ):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                cfg = self._cfg(conn)
                if cfg:
                    conn.execute(
                        "UPDATE termux_config SET enabled = ?, auto_log = ?, source_account = ?, watched = ? WHERE id = ?",
                        (enabled, auto_log, source_account, watched, cfg["id"]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO termux_config (enabled, auto_log, source_account, watched) VALUES (?, ?, ?, ?)",
                        (enabled, auto_log, source_account, watched),
                    )
            return termux_view(request)

        @router.delete("/scan/{scan_id}", response_class=HTMLResponse)
        def delete_scan(request: Request, scan_id: int):
            from app.database import db as global_db
            with global_db.get_connection() as conn:
                row = conn.execute(
                    "SELECT * FROM termux_scan WHERE id = ?", (scan_id,)
                ).fetchone()
                if row and row["status"] == "logged" and row["expense_id"]:
                    # Refund the capital account and remove the linked expense
                    from app.plugins.expenses import ExpensesPlugin
                    ExpensesPlugin()._refund_to_capital(
                        row["amount"], "EUR", row["source_account"] or "wise"
                    )
                    conn.execute(
                        "DELETE FROM expenses WHERE id = ?", (row["expense_id"],)
                    )
                conn.execute(
                    "UPDATE termux_scan SET status = 'deleted' WHERE id = ?", (scan_id,)
                )
            return termux_view(request)

        @router.post("/scan")
        def manual_scan():
            """Trigger a collection pass manually (debug/testing)."""
            summary = self.collect_once()
            return summary

        return router