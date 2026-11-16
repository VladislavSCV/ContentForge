"""Оповещения автору о поломках: сообщение в Telegram и пинг «бот жив».

Бот не может сообщить о собственной смерти, поэтому пинги уходят на внешний сервис (HEARTBEAT_URL,
например healthchecks.io): когда они прекращаются, тот сам пишет автору. Всё остальное — сбои внутри
работающего бота — приходит сообщением. Одинаковые сообщения не чаще раза в час, чтобы одна
поломка не засыпала чат.
"""

import logging
import time

import httpx
from aiogram import Bot

from .config import settings

log = logging.getLogger(__name__)

_bot: Bot | None = None
_last: dict[str, float] = {}


def init(bot: Bot) -> None:
    global _bot
    _bot = bot


async def notify(text: str, key: str | None = None, every: float = 3600) -> bool:
    """Сообщение первому администратору. key — для подавления повторов; True, если отправлено."""
    now = time.monotonic()
    if key and now - _last.get(key, -every) < every:
        return False
    if key:
        _last[key] = now
    if not _bot or not settings.admin_id_set:
        log.warning("оповещение без адресата: %s", text)
        return False
    try:
        await _bot.send_message(sorted(settings.admin_id_set)[0], text[:3500], parse_mode=None)
        return True
    except Exception:  # noqa: BLE001 — оповещение не должно ронять то, о чём оно сообщает
        log.exception("не удалось отправить оповещение")
        return False


async def heartbeat() -> None:
    """Пинг внешнему сторожу. Сбой сети здесь не повод для тревоги: сторож сам заметит тишину."""
    if not settings.heartbeat_url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.get(settings.heartbeat_url)
    except httpx.HTTPError as e:
        log.warning("heartbeat не прошёл: %s", e)
