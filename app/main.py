import importlib
import pkgutil
import threading
import time
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.config import settings
from app.database import db
from app.plugins.base import LifeOSPlugin


def _worker_loop(app):
    """Background worker: runs every plugin's periodic_check() (except termux,
    which has its own fast-capture loop) + termux collect.

    Fires:
      · termux  — collect_once() (auto-log expowatch notifications) [fast loop]
      · money   — budget/rent alerts
      · notes   — due reminders
      · ...     — any plugin with a periodic_check
    """
    with app.state.worker_lock:
        plugins = dict(app.state.plugins)
    while True:
        try:
            for name, p in plugins.items():
                if not hasattr(p, "periodic_check") or name == "termux":
                    continue
                try:
                    p.periodic_check()
                except Exception as e:
                    print(f"[worker] {name} periodic_check error: {e}")
        except Exception as e:
            print(f"[worker] loop error: {e}")
        time.sleep(60)


def _notif_capture_loop(app):
    """Fast poller for transient payment notifications.

    Google Wallet NFC tap-to-pay toasts live ~2-3 seconds; the 60s main loop
    misses them. Runs ONLY the termux collector every 2s so a tap is caught
    in its window. Dedup by notif_id makes extra polls harmless.
    """
    with app.state.worker_lock:
        plugins = dict(app.state.plugins)
    termux = plugins.get("termux")
    if termux is None:
        return
    while True:
        try:
            summary = termux.collect_once()
            if summary.get("logged"):
                print(f"[termux-fast] auto-logged {summary['logged']} expense(s)")
        except Exception as e:
            print(f"[worker] termux fast-capture error: {e}")
        time.sleep(2)


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        docs_url="/docs",
        redoc_url=None
    )

    # Serve local static assets (vendored JS) — no CDN dependency
    static_dir = settings.BASE_DIR / "app" / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Initialize core database tables
    db.init_db()

    # Discover and load plugins dynamically
    load_plugins(app)

    # Background worker: periodic_check + termux collection every 60s
    app.state.worker_lock = threading.Lock()
    threading.Thread(target=_worker_loop, args=(app,), daemon=True).start()
    # Fast capture loop: polls termux notification list every 2s to catch
    # transient Google Wallet NFC toasts that the 60s loop would miss.
    threading.Thread(target=_notif_capture_loop, args=(app,), daemon=True).start()

    @app.get("/api/status")
    def status():
        return {
            "system": settings.PROJECT_NAME,
            "status": "active",
            "plugins_loaded": list(app.state.plugins.keys())
        }

    return app

def load_plugins(app: FastAPI):
    app.state.plugins = {}
    plugins_dir = settings.PLUGINS_DIR

    # Plugins stripped from the UI per Dex (2026-09-21) — re-enabled 2026-09-21: notes (Reminders tab).
    DISABLED_PLUGINS = set()

    # Ensure plugins directory exists
    plugins_dir.mkdir(parents=True, exist_ok=True)

    # Iterate over packages in app.plugins
    for _, module_name, _ in pkgutil.iter_modules([str(plugins_dir)]):
        if module_name == "base" or module_name in DISABLED_PLUGINS:
            continue
        try:
            module = importlib.import_module(f"app.plugins.{module_name}")
            for attribute_name in dir(module):
                attribute = getattr(module, attribute_name)
                if (
                    isinstance(attribute, type)
                    and issubclass(attribute, LifeOSPlugin)
                    and attribute is not LifeOSPlugin
                ):
                    plugin_instance = attribute()
                    # Init plugin database tables
                    with db.get_connection() as conn:
                        plugin_instance.init_tables(conn)
                    
                    # Register routes
                    router = plugin_instance.register_routes()
                    # Webapp plugin serves the UI at root; other plugins under /api/<name>
                    prefix = "" if plugin_instance.name == "webapp" else f"/api/{plugin_instance.name}"
                    app.include_router(router, prefix=prefix, tags=[plugin_instance.name])
                    
                    app.state.plugins[plugin_instance.name] = plugin_instance
        except Exception as e:
            print(f"Failed to load plugin {module_name}: {e}")
