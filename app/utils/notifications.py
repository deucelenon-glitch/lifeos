"""
Notification service for LIFEOS.

Tiered delivery:
1. termux-notification (via termux-api / termux-notification binary) - on-device toasts
2. Telegram bot push (optional, requires bot token + chat id)
3. Browser Notification API (JS side, in templates)
4. ntfy.sh (optional, requires ntfy topic)
"""
import json
import logging
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger("lifeos.notify")


def _termux_available() -> bool:
    return shutil.which("termux-notification") is not None


def send_termux(title: str, message: str, priority: str = "normal") -> bool:
    """Send a toast notification via termux-notification."""
    try:
        if not _termux_available():
            logger.debug("termux-notification binary not found, skipping")
            return False
        subprocess.Popen(
            ["termux-notification", "--title", title, "--content", message, "--priority", priority],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("Termux notification sent: %s — %s", title, message)
        return True
    except Exception as e:
        logger.warning("termux-notification failed: %s", e)
        return False


def send_telegram(bot_token: Optional[str], chat_id: Optional[str], message: str) -> bool:
    """Send a Telegram message via Bot API. No-op unless configured."""
    if not bot_token or not chat_id:
        logger.debug("Telegram not configured, skipping")
        return False
    try:
        import requests

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        resp = requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=10)
        resp.raise_for_status()
        logger.info("Telegram notification sent")
        return True
    except Exception as e:
        logger.warning("Telegram notification failed: %s", e)
        return False


def send_ntfy(topic: Optional[str], message: str, title: Optional[str] = None, priority: str = "default") -> bool:
    """Send a push via ntfy.sh. No-op unless a topic is configured."""
    if not topic:
        logger.debug("ntfy not configured, skipping")
        return False
    try:
        import requests

        headers = {"Title": title or "LIFEOS", "Priority": priority, "Tags": "laptop"}
        url = f"https://ntfy.sh/{topic}"
        resp = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=10)
        resp.raise_for_status()
        logger.info("ntfy notification sent")
        return True
    except Exception as e:
        logger.warning("ntfy notification failed: %s", e)
        return False


def notify(
    title: str,
    message: str,
    telegram_token: Optional[str] = None,
    telegram_chat_id: Optional[str] = None,
    ntfy_topic: Optional[str] = None,
    priority: str = "normal",
) -> dict:
    """
    Tiered notify: tries termux first, then Telegram, then ntfy.
    Returns per-channel delivery report.
    """
    results = {
        "termux": send_termux(title, message, priority),
        "telegram": send_telegram(telegram_token, telegram_chat_id, message),
        "ntfy": send_ntfy(ntfy_topic, message, title, priority),
    }
    delivered = any(results.values())
    logger.info("Notify result: %s (delivered=%s)", results, delivered)
    return {"delivered": delivered, "channels": results}