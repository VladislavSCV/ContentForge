"""Очередь публикаций, ежедневная подборка тем, итоги недели, статья для Хабра и проверки здоровья.

У каналов свои графики: Telegram выходит по дням недели (TG_DAYS в TG_TIME), YouTube — каждый день
в окна PUBLISH_SLOTS, но не раньше поста в канале: ролик зовёт «в Telegram», и пост должен уже висеть.
Время местное, задаётся смещением от UTC: часовые зоны потребовали бы tzdata в образе.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import LinkPreviewOptions

from . import alerts, db, features, pipeline, reminders, youtube
from .config import settings

log = logging.getLogger(__name__)

TICK_SEC = 30
HEARTBEAT_SEC = 300
STALE = timedelta(hours=6)       # после долгого простоя не публикуем пачкой, а переносим на свободные окна
QUEUE_LIMIT = 4                  # столько постов в очереди — новую подборку тем не присылаем
YT_AFTER_TG = timedelta(minutes=30)


def _tz() -> timezone:
    return timezone(timedelta(hours=settings.schedule_utc_offset))


def _hhmm(raw: str) -> tuple[int, int]:
    h, m = raw.strip().split(":")
    return int(h), int(m)


def slots() -> list[tuple[int, int]]:
    """Окна YouTube."""
    return sorted(_hhmm(x) for x in settings.publish_slots.split(",") if x.strip())


def tg_weekdays() -> set[int]:
    return {int(x) for x in settings.tg_days.split(",") if x.strip()}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def next_slot(taken: set[str], now: datetime | None = None, times: list[tuple[int, int]] | None = None,
              weekdays: set[int] | None = None, after: datetime | None = None) -> datetime:
    """Ближайшее свободное окно (местное время) не раньше «сейчас + 5 минут» и не раньше after.

    times — окна в течение дня (по умолчанию окна YouTube), weekdays — допустимые дни недели (1 = понедельник).
    """
    now = (now or datetime.now(timezone.utc)).astimezone(_tz())
    earliest = now + timedelta(minutes=5)
    if after:
        earliest = max(earliest, after.astimezone(_tz()))
    for day in range(90):
        base = now + timedelta(days=day)
        if weekdays and base.isoweekday() not in weekdays:
            continue
        for h, m in times or slots():
            dt = base.replace(hour=h, minute=m, second=0, microsecond=0)
            if dt >= earliest and _iso(dt) not in taken:
                return dt
    raise RuntimeError("нет свободных окон на 90 дней вперёд — проверь TG_DAYS и PUBLISH_SLOTS")


def local_text(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(_tz()).strftime("%d.%m %H:%M")


_DAYS = {1: "пн", 2: "вт", 3: "ср", 4: "чт", 5: "пт", 6: "сб", 7: "вс"}


async def describe() -> str:
    """Расписание человеческим языком и ближайшие свободные окна: что и когда выйдет."""
    now = datetime.now(timezone.utc)
    queued = await db.queued_posts()
    tg_taken = {p.scheduled_at for p in queued if p.scheduled_at}
    yt_taken = {p.yt_scheduled_at for p in queued if p.yt_scheduled_at}
    tg_next = next_slot(tg_taken, now, [_hhmm(settings.tg_time)], tg_weekdays())
    yt_next = next_slot(yt_taken, now, after=tg_next + YT_AFTER_TG)
    days = ", ".join(_DAYS[d] for d in sorted(tg_weekdays()))
    off = settings.schedule_utc_offset
    return "\n".join([
        f"🕒 Расписание (время местное, UTC{off:+d}; сейчас {now.astimezone(_tz()).strftime('%d.%m %H:%M')})",
        f"• Telegram: {days} в {settings.tg_time}. Ближайшее свободное окно: {tg_next.strftime('%d.%m %H:%M')}",
        f"• YouTube: каждый день в {', '.join(f'{h:02d}:{m:02d}' for h, m in slots())}, не раньше поста в канале. "
        f"Ближайшее окно для нового поста: {yt_next.strftime('%d.%m %H:%M')}",
        f"• Подборка тем: каждый день в {settings.topics_time} (не приходит, если в очереди {QUEUE_LIMIT}+ постов)",
        *([f"• Итоги недели: воскресенье в {settings.digest_time}"] if features.enabled("week") else []),
        "• Напоминания про ручные шаги: " + ("включены (пн план, ср Reddit, пт LinkedIn, вс выгрузка, 2-го Хабр, 15-го доклад)"
                                           if settings.reminders else "выключены (REMINDERS=false)"),
        *(["• Статья для Хабра: 1-го числа в " + settings.topics_time + " (нужно 2+ опубликованных поста за 30 дней)"]
          if features.enabled("habr") else []),
        f"• В очереди сейчас: {len(queued)}",
    ])


async def enqueue(post_id: int, now: datetime | None = None) -> tuple[datetime | None, datetime | None]:
    """Ставит пост в очередь: окно в Telegram и окно на YouTube (если есть видео и YouTube подключён)."""
    post = await db.get(post_id)
    queued = [p for p in await db.queued_posts() if p.id != post_id]
    tg_taken = {p.scheduled_at for p in queued if p.scheduled_at}
    yt_taken = {p.yt_scheduled_at for p in queued if p.yt_scheduled_at}
    tg = None if post.tg_message_id else next_slot(tg_taken, now, [_hhmm(settings.tg_time)], tg_weekdays())
    yt = None
    if post.video and settings.youtube_enabled and not post.youtube_video_id:
        yt = next_slot(yt_taken, now, after=tg + YT_AFTER_TG if tg else None)
    if not tg and not yt:
        raise RuntimeError("пост уже опубликован везде, ставить в очередь нечего")
    await db.update(post_id, status=db.SCHEDULED, scheduled_at=_iso(tg) if tg else None,
                    yt_scheduled_at=_iso(yt) if yt else None)
    return tg, yt


async def dequeue(post_id: int) -> None:
    await db.update(post_id, status=db.REVIEW, scheduled_at=None, yt_scheduled_at=None)


def when_text(post: db.Post) -> str:
    parts = []
    if post.scheduled_at:
        parts.append(f"Telegram {local_text(post.scheduled_at)}")
    if post.yt_scheduled_at:
        parts.append(f"YouTube {local_text(post.yt_scheduled_at)}")
    return ", ".join(parts)


async def _publish_due(bot: Bot) -> None:
    now = datetime.now(timezone.utc)
    now_iso = _iso(now)
    for post in await db.due_posts(now_iso):
        due = [x for x in (post.scheduled_at, post.yt_scheduled_at) if x and x <= now_iso]
        if any(now - datetime.fromisoformat(x) > STALE for x in due):
            # прозевали окно (бот был выключен): не публикуем пачкой, берём следующие свободные окна
            tg, yt = await enqueue(post.id)
            log.info("пост %s просрочен, перенесён: %s", post.id, when_text(await db.get(post.id)))
            await alerts.notify(f"⏰ Пост #{post.id} пропустил окно публикации (бот был недоступен). "
                                f"Перенёс: {when_text(await db.get(post.id))}", key=f"stale:{post.id}", every=86400)
            continue
        to_tg = bool(post.scheduled_at and post.scheduled_at <= now_iso)
        to_yt = bool(post.yt_scheduled_at and post.yt_scheduled_at <= now_iso)
        # снимаем сразу: ни при каких сбоях не публиковать дважды
        await db.update(post.id, **({"scheduled_at": None} if to_tg else {}), **({"yt_scheduled_at": None} if to_yt else {}))
        report = await pipeline.publish(bot, post.id, to_tg=to_tg, to_yt=to_yt)
        fresh = await db.get(post.id)
        if fresh.scheduled_at or fresh.yt_scheduled_at:       # второй канал ещё ждёт своего окна
            await db.update(post.id, status=db.SCHEDULED)
        failed = any("❌" in r for r in report)
        head = "⚠️ Не всё вышло по расписанию" if failed else "🕒 Вышло по расписанию"
        await bot.send_message(post.admin_chat_id, f"{head}, пост #{post.id}:\n" + "\n".join(report), parse_mode=None,
                               link_preview_options=LinkPreviewOptions(is_disabled=True))


async def _daily_topics(bot: Bot, send_topics: Callable[[Bot, int], Awaitable[None]]) -> None:
    local = datetime.now(timezone.utc).astimezone(_tz())
    h, m = _hhmm(settings.topics_time)
    today = local.strftime("%Y-%m-%d")
    if (local.hour, local.minute) < (h, m) or await db.get_state("topics_day") == today:
        return
    await db.set_state("topics_day", today)    # отметка до отправки: сбой не должен слать подборку каждые 30 секунд
    if len(await db.queued_posts()) >= QUEUE_LIMIT:
        return
    for admin in sorted(settings.admin_id_set)[:1]:   # лички: id пользователя совпадает с id чата
        await send_topics(bot, admin)


async def _weekly_digest(bot: Bot, send_digest: Callable[[Bot, int], Awaitable[None]]) -> None:
    local = datetime.now(timezone.utc).astimezone(_tz())
    week = local.strftime("%G-W%V")
    h, m = _hhmm(settings.digest_time)
    if local.weekday() != 6 or (local.hour, local.minute) < (h, m) or await db.get_state("digest_week") == week:
        return
    await db.set_state("digest_week", week)
    for admin in sorted(settings.admin_id_set)[:1]:
        await send_digest(bot, admin)


async def _monthly_habr(bot: Bot, send_habr: Callable[..., Awaitable[None]]) -> None:
    """Первого числа сама собирает статью для Хабра из постов прошедшего месяца."""
    local = datetime.now(timezone.utc).astimezone(_tz())
    month = local.strftime("%Y-%m")
    h, m = _hhmm(settings.topics_time)
    if local.day != 1 or (local.hour, local.minute) < (h, m) or await db.get_state("habr_month") == month:
        return
    await db.set_state("habr_month", month)    # отметка до запуска: сбой не должен перезапускать часовую генерацию
    for admin in sorted(settings.admin_id_set)[:1]:
        await send_habr(bot, admin, quiet=True)


async def _reminders(bot: Bot) -> None:
    """Напоминания автору про ручные шаги (app/reminders.py). Сбой отправки не должен слать то же самое каждые 30 секунд."""
    if not settings.reminders or not settings.admin_id_set:
        return
    local = datetime.now(timezone.utc).astimezone(_tz())
    for key, text in await reminders.due_messages(local):
        await db.set_state(f"remind:{key}", local.strftime("%Y-%m-%d"))    # отметка до отправки, как у подборки тем
        if text:
            await bot.send_message(sorted(settings.admin_id_set)[0], text, parse_mode=None)


async def _health(bot: Bot) -> None:
    """Раз в день: живы ли токены YouTube и не опустела ли очередь. Главный риск — тихо протухший токен."""
    local = datetime.now(timezone.utc).astimezone(_tz())
    today = local.strftime("%Y-%m-%d")
    if local.hour < 8 or await db.get_state("health_day") == today:
        return
    await db.set_state("health_day", today)
    for name, enabled, stats in (("загрузки", settings.youtube_enabled, False),
                                 ("статистики", settings.youtube_stats_enabled, True)):
        if not enabled:
            continue
        try:
            await youtube.check(stats=stats)
        except youtube.YouTubeError as e:
            await alerts.notify(f"🔑 Токен YouTube для {name} не работает: {e}\n"
                                f"Перевыпусти: scripts/youtube_auth.py{' --stats' if stats else ''}", key=f"yt-token:{stats}", every=43200)
    posts = await db.latest(100)
    published = [p for p in posts if p.status == db.PUBLISHED]
    waiting = [p for p in posts if p.status in (db.SCHEDULED, db.REVIEW, db.GENERATING)]
    if not waiting and published:
        last = max(datetime.fromisoformat(p.updated_at) for p in published)
        days = (datetime.now(timezone.utc) - last).days
        if days >= 3:
            await alerts.notify(f"📭 Очередь пуста, последняя публикация {days} дн. назад. /topics — подобрать темы.",
                                key="empty-queue", every=86400)


async def run(bot: Bot, send_topics: Callable[[Bot, int], Awaitable[None]],
              send_habr: Callable[..., Awaitable[None]],
              send_digest: Callable[[Bot, int], Awaitable[None]]) -> None:
    last_beat = 0.0
    steps = (("публикация", lambda: _publish_due(bot)), ("темы", lambda: _daily_topics(bot, send_topics)),
             *((("Хабр", lambda: _monthly_habr(bot, send_habr)),) if features.enabled("habr") else ()),
             *((("итоги", lambda: _weekly_digest(bot, send_digest)),) if features.enabled("week") else ()),
             ("напоминания", lambda: _reminders(bot)), ("проверки", lambda: _health(bot)))
    while True:
        for name, step in steps:
            try:
                await step()
            except Exception as e:  # noqa: BLE001 — планировщик не должен останавливаться из-за одного шага
                log.exception("сбой планировщика: %s", name)
                await alerts.notify(f"⚠️ Сбой планировщика ({name}): {type(e).__name__}: {e}", key=f"loop:{name}")
        if time.monotonic() - last_beat >= HEARTBEAT_SEC:
            last_beat = time.monotonic()
            await alerts.heartbeat()
        await asyncio.sleep(TICK_SEC)
