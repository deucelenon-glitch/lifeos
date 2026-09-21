from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from app.plugins.base import LifeOSPlugin
import calendar
import datetime
import re
import sqlite3


class NotesPlugin(LifeOSPlugin):
    @property
    def name(self) -> str:
        return "notes"

    def init_tables(self, conn: sqlite3.Connection):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT 'note',
                title TEXT,
                body TEXT,
                remind_at TEXT,
                done INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(notes)").fetchall()]
        if "repeat_rule" not in cols:
            conn.execute("ALTER TABLE notes ADD COLUMN repeat_rule TEXT DEFAULT ''")
        if "repeat_until" not in cols:
            conn.execute("ALTER TABLE notes ADD COLUMN repeat_until TEXT DEFAULT ''")
        # Repair malformed remind_at values (e.g. '2026-09-21 :19:40' or '2026-09-21:19:40')
        for r in conn.execute("SELECT id, remind_at FROM notes WHERE kind='reminder' AND remind_at IS NOT NULL").fetchall():
            raw = (r["remind_at"] or "").strip().replace("T", " ")
            fixed = re.sub(r"(\d{4}-\d{2}-\d{2})\s*:\s*(\d{2}:\d{2})", r"\1 \2", raw)
            fixed = re.sub(r"\s*:\s*", ":", fixed)
            fixed = re.sub(r"\s+", " ", fixed)
            if fixed != r["remind_at"]:
                conn.execute("UPDATE notes SET remind_at = ? WHERE id = ?", (fixed, r["id"]))
        conn.commit()

    def _now(self) -> str:
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    def _due(self, conn, now: str) -> list:
        return conn.execute(
            "SELECT * FROM notes WHERE kind='reminder' AND done=0 AND remind_at IS NOT NULL AND remind_at <= ?",
            (now,),
        ).fetchall()

    def _next_occurrence(self, remind_at: str, rule: str) -> str:
        try:
            dt = datetime.datetime.strptime(remind_at, "%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return remind_at
        if rule == "daily":
            dt += datetime.timedelta(days=1)
        elif rule == "weekly":
            dt += datetime.timedelta(days=7)
        elif rule == "monthly":
            year = dt.year + (dt.month // 12)
            month = dt.month % 12 + 1
            day = min(dt.day, calendar.monthrange(year, month)[1])
            dt = dt.replace(year=year, month=month, day=day)
        elif rule == "yearly":
            year = dt.year + 1
            day = min(dt.day, calendar.monthrange(year, dt.month)[1])
            dt = dt.replace(year=year, day=day)
        elif rule.startswith("custom:"):
            try:
                n = int(rule.split(":", 1)[1])
            except (ValueError, IndexError):
                n = 1
            dt += datetime.timedelta(days=max(1, n))
        else:
            return remind_at
        return dt.strftime("%Y-%m-%d %H:%M")

    def _send(self, title: str, body: str):
        from app.database import db as global_db
        token, chat = None, None
        try:
            with global_db.get_connection() as conn:
                row = conn.execute(
                    "SELECT bot_token, chat_id FROM telegram_config ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row:
                    token, chat = row["bot_token"], row["chat_id"]
        except Exception:
            pass
        from app.utils.notifications import notify
        return notify(title, body, telegram_token=token, telegram_chat_id=chat)

    def periodic_check(self) -> bool:
        from app.database import db as global_db
        try:
            with global_db.get_connection() as conn:
                due = self._due(conn, self._now())
                if not due:
                    return False
                for r in due:
                    self._send(f"⏰ {r['title'] or 'Reminder'}", r["body"] or "It's time!")
                    rule = r["repeat_rule"] or ""
                    if rule:
                        nxt = self._next_occurrence(r["remind_at"], rule)
                        repeat_until = (r["repeat_until"] or "").strip()
                        if repeat_until and nxt[:10] > repeat_until:
                            conn.execute("UPDATE notes SET done = 1 WHERE id = ?", (r["id"],))
                        else:
                            conn.execute(
                                "UPDATE notes SET remind_at = ?, done = 0 WHERE id = ?",
                                (nxt, r["id"]),
                            )
                    else:
                        conn.execute("UPDATE notes SET done = 1 WHERE id = ?", (r["id"],))
                conn.commit()
                return True
        except Exception as e:
            print(f"[notes] periodic_check error: {e}")
            return False

    def get_dashboard_widgets(self) -> list:
        return []

    def menu(self) -> dict:
        from app.database import db as global_db
        try:
            with global_db.get_connection() as conn:
                n = conn.execute("SELECT COUNT(*) c FROM notes WHERE kind='reminder' AND done=0").fetchone()["c"]
        except Exception:
            n = 0
        return {"id": self.name, "icon": "🔔", "label": "Reminders", "badge": str(n) if n else "", "view": "/api/notes/reminders"}

    def register_routes(self) -> APIRouter:
        router = APIRouter()
        from app.database import db as global_db

        def _reminder_row(n):
            status = "✅ done" if n["done"] else ("🔴 overdue" if n["remind_at"] and n["remind_at"] < self._now() else "🟢 upcoming")
            repeat = (n["repeat_rule"] or "").strip()
            if repeat.startswith("custom:"):
                try:
                    n_days = int(repeat.split(":", 1)[1])
                    repeat_label = f"↻ every {n_days} day{'s' if n_days != 1 else ''}"
                except (ValueError, IndexError):
                    repeat_label = "↻ custom"
            elif repeat:
                repeat_label = f"↻ {repeat}"
            else:
                repeat_label = ""
            until = (n["repeat_until"] or "").strip()
            until_label = f" · until {until}" if until else ""
            repeat_html = f"<span class='text-[10px] px-1.5 rounded bg-dark-800 text-emerald-500/80'>{repeat_label}</span>" if repeat_label else ""
            done_btn = "" if n["done"] else (
                f"<button hx-post='/api/notes/{n['id']}/done' hx-target='#reminders-area' hx-swap='outerHTML' "
                f"class='text-emerald-500 hover:text-emerald-300 text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/10 border border-emerald-500/30'>✓ Done</button>"
            )
            return f"""
            <div class='flex items-start justify-between gap-2 py-2 border-b border-dark-800/60 text-xs'>
                <div class='min-w-0'>
                    <span class='text-slate-200 font-semibold'>⏰ {n['title'] or 'Reminder'}</span>
                    <span class='text-slate-500 font-mono ml-1'>{n['remind_at']}</span>
                    <span class='text-[10px] px-1.5 rounded bg-dark-700 text-slate-400'>{status}</span>
                    {repeat_html}{until_label and f"<span class='text-slate-500 font-mono ml-1'>{until_label}</span>" or ''}
                    {(f"<p class='text-slate-400 mt-0.5'>{n['body'][:160]}</p>") if n["body"] else ""}
                </div>
                <div class='flex items-center gap-1'>
                    {done_btn}
                    <button hx-delete='/api/notes/{n['id']}' hx-target='#reminders-area' hx-swap='outerHTML'
                            class='text-slate-600 hover:text-red-400 shrink-0 text-[10px]'>✕</button>
                </div>
            </div>"""

        def _note_row(n):
            return f"""
            <div id='note-{n['id']}' class='flex items-start justify-between gap-2 py-2 border-b border-dark-800/60 text-xs'>
                <div class='min-w-0'>
                    <span class='text-slate-200'>{(n['title'] and ('📝 ' + n['title'])) or '📝 Untitled'}</span>
                    <p class='text-slate-400 whitespace-pre-wrap mt-0.5'>{n['body'] or ''}</p>
                    <span class='text-[10px] text-slate-600 font-mono'>{n['created_at'][:16]}</span>
                </div>
                <div class='flex items-center gap-2 shrink-0'>
                    <button hx-get='/api/notes/{n['id']}/edit' hx-target='#note-{n['id']}' hx-swap='outerHTML'
                            class='text-slate-500 hover:text-emerald-400 text-[10px] px-1.5 py-0.5 rounded bg-dark-800/60 border border-dark-700'>✏️</button>
                    <button hx-delete='/api/notes/{n['id']}' hx-target='#notepad-area' hx-swap='outerHTML'
                            class='text-slate-600 hover:text-red-400 text-[10px]'>✕</button>
                </div>
            </div>"""

        def _edit_note_form(n):
            return f"""
            <div id='note-{n['id']}' class='py-2'>
                <form hx-post='/api/notes/{n['id']}/edit' hx-target='#notepad-area' hx-swap='outerHTML'
                      @submit="toast = 'Note updated ✓'"
                      class='space-y-3 bg-dark-950 border-2 border-emerald-500/40 rounded-2xl p-5 shadow-lg shadow-emerald-500/5'>
                    <div>
                        <label class='block text-[10px] uppercase font-mono text-slate-400 mb-1'>Title</label>
                        <input type='text' name='title' value='{(n['title'] or '').replace(chr(39), '&#39;')}'
                               placeholder='Title (optional)'
                               class='w-full bg-dark-800 border border-dark-700 rounded-xl px-4 py-3 text-white text-base font-medium focus:outline-none focus:border-emerald-500'>
                    </div>
                    <div>
                        <label class='block text-[10px] uppercase font-mono text-slate-400 mb-1'>Note</label>
                        <textarea name='body' rows='8' required
                                  placeholder='Write something…'
                                  class='w-full bg-dark-800 border border-dark-700 rounded-xl px-4 py-3 text-white text-base leading-relaxed focus:outline-none focus:border-emerald-500 resize-y'>{n['body'] or ''}</textarea>
                    </div>
                    <div class='flex gap-3 pt-1'>
                        <button type='submit' class='flex-1 bg-emerald-600 hover:bg-emerald-500 text-white font-semibold px-4 py-3 rounded-xl text-sm transition'>💾 Save</button>
                        <button type='button' hx-get='/api/notes/notepad' hx-target='#notepad-area' hx-swap='outerHTML'
                                class='bg-dark-800 hover:bg-dark-700 text-slate-300 px-5 py-3 rounded-xl text-sm transition'>Cancel</button>
                    </div>
                </form>
            </div>"""

        @router.get("/reminders", response_class=HTMLResponse)
        def reminders_view(request: Request, q: str = ""):
            with global_db.get_connection() as conn:
                if q.strip():
                    like = f"%{q.strip()}%"
                    notes = conn.execute(
                        "SELECT * FROM notes WHERE kind='reminder' AND (body LIKE ? OR title LIKE ?) ORDER BY remind_at, id DESC LIMIT 100",
                        (like, like),
                    ).fetchall()
                else:
                    notes = conn.execute(
                        "SELECT * FROM notes WHERE kind='reminder' ORDER BY remind_at, done, id DESC LIMIT 200"
                    ).fetchall()
                open_count = conn.execute("SELECT COUNT(*) c FROM notes WHERE kind='reminder' AND done=0").fetchone()["c"]

            rows = "".join(_reminder_row(n) for n in notes) or "<p class='text-slate-500 py-4 text-center text-xs'>No reminders yet — set one below.</p>"
            return f"""
            <div id='reminders-area'>
            <div class='space-y-4'>
                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4'>
                    <div class='flex justify-between items-center'>
                        <div>
                            <h3 class='font-bold text-white text-sm'>🔔 Reminders</h3>
                            <p class='text-xs text-slate-400 mt-0.5'>Set a date/time — the worker pings Termux/Telegram when it's due.</p>
                        </div>
                        <span class='text-xs px-2.5 py-1 rounded-full bg-dark-800 text-slate-400 font-mono'>{open_count} open</span>
                    </div>
                </div>
                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4'>
                    <h4 class='font-semibold text-white text-xs uppercase mb-3'>➕ New reminder</h4>
                    <form hx-post='/api/notes/reminder' hx-target='#reminders-area' hx-swap='outerHTML'
                          @submit="toast = 'Reminder set ✓'"
                          class='grid grid-cols-1 md:grid-cols-4 gap-2 items-end'>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Title</label>
                            <input type='text' name='title' placeholder='Pay rent' required
                                   class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm'>
                        </div>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>When</label>
                            <input type='datetime-local' name='remind_at' required
                                   class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm font-mono'>
                        </div>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Note (optional)</label>
                            <input type='text' name='body' placeholder='€850 to landlord'
                                   class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm'>
                        </div>
                        <button type='submit' class='w-full bg-emerald-600 hover:bg-emerald-500 text-white font-medium py-2 rounded-xl text-sm'>⏰ Set</button>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Repeat</label>
                            <select name='repeat_rule' class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm'>
                                <option value='' selected>🔁 No repeat</option>
                                <option value='daily'>↻ Daily</option>
                                <option value='weekly'>↻ Weekly</option>
                                <option value='monthly'>↻ Monthly</option>
                                <option value='yearly'>↻ Yearly</option>
                                <option value='custom:2'>↻ Every 2 days</option>
                                <option value='custom:3'>↻ Every 3 days</option>
                                <option value='custom:7'>↻ Every 7 days</option>
                                <option value='custom:14'>↻ Every 14 days</option>
                                <option value='custom:30'>↻ Every 30 days</option>
                            </select>
                        </div>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Repeat until (YYYY-MM-DD)</label>
                            <input type='date' name='repeat_until' value=''
                                   class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-xs font-mono'>
                        </div>
                    </form>
                </div>
                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4'>
                    <div class='flex items-center justify-between mb-2'>
                        <h4 class='font-semibold text-white text-xs uppercase'>🗂️ All ({len(notes)})</h4>
                        <form hx-get='/api/notes/reminders' hx-target='#reminders-area' hx-swap='outerHTML'
                              class='flex items-center gap-1.5'>
                            <input type='text' name='q' placeholder='Search…' value='{q}'
                                   class='w-36 bg-dark-950 border border-dark-800 rounded-lg px-2 py-1.5 text-white text-xs'>
                            <button type='submit' class='bg-dark-800 hover:bg-dark-700 text-emerald-400 px-2.5 py-1.5 rounded-lg text-xs'>🔍</button>
                        </form>
                    </div>
                    {rows}
                </div>
            </div>
            </div>
            """

        @router.get("/notepad", response_class=HTMLResponse)
        def notepad_view(request: Request, q: str = ""):
            with global_db.get_connection() as conn:
                if q.strip():
                    like = f"%{q.strip()}%"
                    notes = conn.execute(
                        "SELECT * FROM notes WHERE kind='note' AND (body LIKE ? OR title LIKE ?) ORDER BY id DESC LIMIT 100",
                        (like, like),
                    ).fetchall()
                else:
                    notes = conn.execute("SELECT * FROM notes WHERE kind='note' ORDER BY id DESC LIMIT 200").fetchall()

            rows = "".join(_note_row(n) for n in notes) or "<p class='text-slate-500 py-4 text-center text-xs'>Nothing noted yet — jot something down.</p>"
            return f"""
            <div id='notepad-area'>
            <div class='space-y-4'>
                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4'>
                    <h3 class='font-bold text-white text-sm'>📝 Notepad</h3>
                    <p class='text-xs text-slate-400 mt-0.5'>Quick scratchpad — no dates, no fuss.</p>
                </div>
                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4'>
                    <h4 class='font-semibold text-white text-xs uppercase mb-3'>➕ New note</h4>
                    <form hx-post='/api/notes/note' hx-target='#notepad-area' hx-swap='outerHTML'
                          @submit="toast = 'Note saved ✓'"
                          class='grid grid-cols-1 md:grid-cols-3 gap-2 items-end'>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Title (optional)</label>
                            <input type='text' name='title' placeholder='Shopping list'
                                   class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm'>
                        </div>
                        <div>
                            <label class='text-[10px] uppercase font-mono text-slate-400'>Body</label>
                            <input type='text' name='body' placeholder='Milk, eggs, bread…' required
                                   class='w-full bg-dark-950 border border-dark-800 rounded-lg px-2 py-2 text-white text-sm'>
                        </div>
                        <button type='submit' class='w-full bg-dark-800 hover:bg-dark-700 text-emerald-400 font-medium py-2 rounded-xl text-sm'>📝 Save</button>
                    </form>
                </div>
                <div class='bg-dark-900 border border-dark-800 rounded-2xl p-4'>
                    <div class='flex items-center justify-between mb-2'>
                        <h4 class='font-semibold text-white text-xs uppercase'>🗂️ All ({len(notes)})</h4>
                        <form hx-get='/api/notes/notepad' hx-target='#notepad-area' hx-swap='outerHTML'
                              class='flex items-center gap-1.5'>
                            <input type='text' name='q' placeholder='Search…' value='{q}'
                                   class='w-36 bg-dark-950 border border-dark-800 rounded-lg px-2 py-1.5 text-white text-xs'>
                            <button type='submit' class='bg-dark-800 hover:bg-dark-700 text-emerald-400 px-2.5 py-1.5 rounded-lg text-xs'>🔍</button>
                        </form>
                    </div>
                    {rows}
                </div>
            </div>
            </div>
            """

        @router.post("/p2p-reminder", response_class=HTMLResponse)
        def save_p2p_reminder(request: Request, reminder_time: str = Form("")):
            reminder_time = reminder_time.strip()
            with global_db.get_connection() as conn:
                cfg = conn.execute("SELECT id FROM p2p_config ORDER BY id DESC LIMIT 1").fetchone()
                if cfg:
                    conn.execute("UPDATE p2p_config SET reminder_time = ? WHERE id = ?", (reminder_time or None, cfg["id"]))
                else:
                    conn.execute("INSERT INTO p2p_config (interval_minutes, rate, reminder_time, enabled) VALUES (480, 1.15, ?, 1)", (reminder_time or None,))
                conn.commit()
            return reminders_view(request)

        @router.post("/reminder", response_class=HTMLResponse)
        def add_reminder(
            request: Request,
            title: str = Form(...),
            remind_at: str = Form(...),
            body: str = Form(""),
            repeat_rule: str = Form(""),
            repeat_until: str = Form(""),
        ):
            # Normalize: accept 'YYYY-MM-DDTHH:MM' (datetime-local) or 'YYYY-MM-DD HH:MM'
            dt_raw = remind_at.strip().replace("T", " ")
            dt_raw = re.sub(r"\s*:\s*", ":", dt_raw)
            dt_raw = re.sub(r"\s+", " ", dt_raw)
            try:
                parsed = datetime.datetime.strptime(dt_raw[:16], "%Y-%m-%d %H:%M")
                dt_norm = parsed.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                return reminders_view(request)  # invalid — re-render
            with global_db.get_connection() as conn:
                conn.execute(
                    "INSERT INTO notes (kind, title, body, remind_at, done, repeat_rule, repeat_until) VALUES ('reminder', ?, ?, ?, 0, ?, ?)",
                    (title.strip(), body.strip(), dt_norm, repeat_rule.strip(), repeat_until.strip()),
                )
            return reminders_view(request)

        @router.post("/note", response_class=HTMLResponse)
        def add_note(request: Request, title: str = Form(""), body: str = Form(...)):
            with global_db.get_connection() as conn:
                conn.execute(
                    "INSERT INTO notes (kind, title, body) VALUES ('note', ?, ?)",
                    (title.strip(), body.strip()),
                )
            return notepad_view(request)

        @router.get("/{note_id}/edit", response_class=HTMLResponse)
        def edit_note_form(request: Request, note_id: int):
            with global_db.get_connection() as conn:
                n = conn.execute("SELECT * FROM notes WHERE id = ? AND kind='note'", (note_id,)).fetchone()
            if not n:
                return notepad_view(request)
            return _edit_note_form(n)

        @router.post("/{note_id}/edit", response_class=HTMLResponse)
        def edit_note(request: Request, note_id: int, title: str = Form(""), body: str = Form(...)):
            with global_db.get_connection() as conn:
                conn.execute(
                    "UPDATE notes SET title = ?, body = ? WHERE id = ? AND kind='note'",
                    (title.strip(), body.strip(), note_id),
                )
            return notepad_view(request)

        @router.post("/{note_id}/done", response_class=HTMLResponse)
        def mark_done(request: Request, note_id: int):
            with global_db.get_connection() as conn:
                conn.execute("UPDATE notes SET done = 1 WHERE id = ? AND kind='reminder'", (note_id,))
            return reminders_view(request)

        @router.delete("/{note_id}", response_class=HTMLResponse)
        def delete_note(request: Request, note_id: int):
            with global_db.get_connection() as conn:
                row = conn.execute("SELECT kind FROM notes WHERE id = ?", (note_id,)).fetchone()
                kind = row["kind"] if row else "note"
                conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            if kind == "reminder":
                return reminders_view(request)
            return notepad_view(request)

        return router