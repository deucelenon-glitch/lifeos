# Reminders + Notepad Tabs — Master Plan

Status: IN PROGRESS (2026-09-21). Owner: Cody. Operator: Dex.

## Goal (Dex's explicit asks, in order)
1. Reminders and Notepad are **separate tabs** (not one "Notes" tab).
2. Habits must be **3rd tab** (Home → Planner → Cashflow → Habits ✓ already true).
3. P2P order reminder **moves into the Reminders tab** (drop the embedded one from P2P view).
4. **Make the reminder actually work** — daily P2P reminder must fire (it never has).
5. Monthly → daily budget breakdown (separate thread, NOT part of this tab work).

## Backend — DONE (app/plugins/notes.py, already rewritten)
- `GET /api/notes/reminders` → `#reminders-area` wrapper:
  - new-reminder form (title, when `YYYY-MM-DD HH:MM`, body note)
  - list with status chips: ✅ done / 🔴 overdue / 🟢 upcoming
  - ✓ Done button → `hx-post /api/notes/{id}/done`
  - ✕ delete → `hx-delete /api/notes/{id}`
  - search box → `hx-get /api/notes/reminders?q=`
- `GET /api/notes/notepad` → `#notepad-area` wrapper:
  - scratchpad (title + body), list, delete, search
- `periodic_check()`: finds due one-shot reminders (`remind_at <= now AND done=0`),
  sends via tiered `notify()` (termux toast → telegram → ntfy), marks done.
- `menu()`: badge = open reminder count (🔔 Reminders).
- Worker: `app/main.py` `_worker_loop` already calls every plugin's
  `periodic_check()` every 60s → reminders WILL fire once wired. ✓ confirmed live.

## Frontend — TODO (app/templates/index.html)
- Add two nav buttons: 🔔 Reminders + 📝 Notepad (place after P2P, before Auto-Expense).
- Add panel divs: `x-show="activeTab === 'reminders'"` → `#reminders-area`
  `hx-get /api/notes/reminders hx-trigger load`; same for `notepad` → `/api/notes/notepad`.
- Both must render as SELF-WRAPPING shells (view returns `<div id='reminders-area'>` +
  `hx-target` swaps target those ids — DO NOT wrap again in index.html).
- No `hx-trigger='load'` inside the returned fragment (flicker loop rule — see memory).
- Nav badge for Reminders = open count (nice-to-have via /api/notes/menu).

## P2P reminder — TODO (app/plugins/p2p.py)
- REMOVE the ⏰ Order Reminder section from the p2p view; keep order size,
  monthly goal + progress in the config form (config route keeps writing those).
- `reminder_time` stays in `p2p_config` (column exists) — the Reminders tab
  gets a "Daily P2P reminder" sub-section: time input + save → writes
  `p2p_config.reminder_time` via a small route (or reuse notes route with a flag).
- `p2p.periodic_check()` (currently returns False): fire a daily notification at
  `reminder_time` using the same `notify()` helper as notes.py; add
  `last_reminder_date` column to p2p_config to fire at most once per day.

## Rules / gotchas (from memory, non-negotiable)
- Never put `//` comments inside Alpine `x-data` (breaks the whole body).
- Returned fragments must NOT self-trigger `hx-trigger='load'` (infinite swap loop).
- Don't wrap `#reminders-area`/`#notepad-area` again in index.html — swap targets
  are those ids.
- Restart server via literal PID kill, then `nohup ./venv/bin/uvicorn run:app
  --host 0.0.0.0 --port 8080 > data/server.log 2>&1 &` (never pkill -f "uvicorn").
- Verify after changes: `curl localhost:8080/api/notes/reminders`,
  `/api/notes/notepad`, `/api/dashboard` all HTTP 200.

## Definition of done (checklist)
- [ ] Nav has 🔔 Reminders + 📝 Notepad; Habits is 3rd
- [ ] Reminders tab: add form, status chips, Done, delete, search all work
- [ ] Notepad tab: scratchpad add/list/delete/search work
- [ ] P2P view no longer has ⏰ Order Reminder; config still saves order size/goal
- [ ] Daily P2P reminder fires via worker (periodic_check), ≤1/day
- [ ] No self-referencing load loops; no flicker
- [ ] Server restarted, all endpoints 200, committed