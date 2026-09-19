# LIFEOS

Personal life-tracking hub — 100% on-device. Track habits, expenses, health, time, and tasks with sharp precision.

## Stack

- FastAPI + SQLite (single-file DB, zero cloud)
- HTMX + Tailwind (optional front-end layer)
- Plugin-based architecture — add trackers without touching core

## Quick Start

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./scripts/serve.sh            # starts on :8080
./venv/bin/python cli/life.py status
```

## Architecture

```
lifeos/
├── app/
│   ├── main.py               # app factory + plugin loader
│   ├── config.py             # settings (paths, env)
│   ├── database.py           # SQLite connection layer
│   ├── plugins/              # ← drop a new tracker here
│   │   ├── base.py           # LifeOSPlugin ABC
│   │   └── habits.py         # example plugin
│   ├── routers/              # shared routers (not plugin-bound)
│   ├── services/             # business logic (streaks, aggregation)
│   ├── templates/            # Jinja2 templates (if used)
│   ├── static/               # CSS/JS
│   └── utils/                # notifications, helpers
├── cli/life.py               # terminal input surface
├── scripts/                  # backup.sh, serve.sh
├── tests/                    # pytest suite
└── data/                     # lifeos.db
```

## Adding a Plugin

Create `app/plugins/<name>.py`:

```python
from app.plugins.base import LifeOSPlugin

class MyPlugin(LifeOSPlugin):
    name = "myplugin"

    def init_tables(self, conn): ...
    def register_routes(self): ...
    def get_dashboard_widgets(self): ...
```

Drop the file in, restart — auto-discovered at `/api/<name>`.

## Notifications

`app/utils/notifications.py` — tiered delivery:
termux-notification → Telegram → ntfy.

## Backup

`./scripts/backup.sh` — daily sqlite backup to `backups/`, kept 14 days, committed to git.