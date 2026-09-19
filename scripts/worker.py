#!/usr/bin/env python3
"""
LIFEOS Periodic Worker — powers plugin reminders (P2P order nudges etc).

Every CHECK_INTERVAL seconds it calls each plugin's periodic_check().
Plugins decide internally whether a nudge is due (e.g. interval elapsed).

Run alongside the webapp and bot:
    nohup ./venv/bin/python scripts/worker.py > data/worker.log 2>&1 &
"""
import importlib
import pkgutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.plugins.base import LifeOSPlugin  # noqa: E402

CHECK_INTERVAL = 60  # seconds — worker heartbeat; plugins check their own due times


def collect_plugins() -> list:
    plugins = []
    plugins_dir = REPO_ROOT / "app" / "plugins"
    for _, module_name, _ in pkgutil.iter_modules([str(plugins_dir)]):
        if module_name == "base":
            continue
        try:
            module = importlib.import_module(f"app.plugins.{module_name}")
            for attr_name in dir(module):
                attribute = getattr(module, attr_name)
                if (
                    isinstance(attribute, type)
                    and issubclass(attribute, LifeOSPlugin)
                    and attribute is not LifeOSPlugin
                ):
                    plugins.append(attribute())
        except Exception as e:
            print(f"[worker] failed to load {module_name}: {e}")
    return plugins


def main():
    plugins = collect_plugins()
    print(f"[worker] loaded {len(plugins)} plugins: {', '.join(p.name for p in plugins)}")
    print(f"[worker] checking every {CHECK_INTERVAL}s (Ctrl+C to stop)")
    while True:
        for p in plugins:
            try:
                p.periodic_check()
            except Exception as e:
                print(f"[worker] {p.name} periodic_check error: {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())