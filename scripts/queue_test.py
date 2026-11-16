"""Проверка очереди на настоящей базе (во временной папке) с подменой публикации и бота.

    docker compose run --rm -v "$PWD":/srv -w /srv contentops python scripts/queue_test.py
Ничего не публикует и не ходит в сеть.
"""

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.update(TELEGRAM_BOT_TOKEN="1:x", ADMIN_IDS="1", DATA_DIR=tempfile.mkdtemp(prefix="queue_test_"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, pipeline, scheduler  # noqa: E402
from app.config import settings  # noqa: E402

settings.youtube_client_id = settings.youtube_client_secret = settings.youtube_refresh_token = "x"
settings.tg_days, settings.tg_time, settings.publish_slots = "1,2,3,4,5,6,7", "12:00", "19:00"


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)


async def fake_publish(bot, post_id, to_tg, to_yt):
    """Как pipeline.publish: отмечает опубликованное и ставит статус."""
    fields, report = {}, []
    if to_tg:
        fields["tg_message_id"] = 10
        report.append("Telegram: ✅")
    if to_yt:
        fields["youtube_video_id"] = "vid"
        report.append("YouTube: ✅")
    await db.update(post_id, status=db.PUBLISHED, **fields)
    return report


async def main() -> None:
    interrupted = await db.init()
    assert interrupted == []
    pipeline.publish = fake_publish
    bot = FakeBot()

    pid = await db.create(1, "текст", None)
    await db.update(pid, status=db.REVIEW, video_path=str(Path(__file__)))   # любой существующий файл сойдёт за видео
    tg, yt = await scheduler.enqueue(pid)
    post = await db.get(pid)
    assert post.status == db.SCHEDULED and post.scheduled_at and post.yt_scheduled_at
    assert yt >= tg + scheduler.YT_AFTER_TG, (tg, yt)                        # ролик не раньше поста в канале
    assert [p.id for p in await db.queued_posts()] == [pid]

    # второй пост занимает следующие окна, а не те же
    pid2 = await db.create(1, "текст 2", None)
    await db.update(pid2, status=db.REVIEW, video_path=str(Path(__file__)))
    tg2, yt2 = await scheduler.enqueue(pid2)
    assert tg2 != tg and yt2 != yt, (tg, tg2, yt, yt2)

    # наступило окно Telegram, окно YouTube ещё впереди: уходит только Telegram, пост остаётся в очереди
    past, future = datetime.now(timezone.utc) - timedelta(minutes=1), datetime.now(timezone.utc) + timedelta(days=1)
    await db.update(pid, scheduled_at=scheduler._iso(past), yt_scheduled_at=scheduler._iso(future))
    await scheduler._publish_due(bot)
    post = await db.get(pid)
    assert post.tg_message_id and not post.youtube_video_id, post
    assert post.status == db.SCHEDULED and post.scheduled_at is None and post.yt_scheduled_at, post
    assert any("по расписанию" in m for m in bot.sent), bot.sent

    # потом наступает окно YouTube: пост опубликован полностью
    await db.update(pid, yt_scheduled_at=scheduler._iso(past))
    await scheduler._publish_due(bot)
    post = await db.get(pid)
    assert post.status == db.PUBLISHED and post.youtube_video_id and not post.yt_scheduled_at, post

    # окно прозевано на полдня (бот был выключен): не публикуем, а переносим
    old = datetime.now(timezone.utc) - timedelta(hours=12)
    await db.update(pid2, scheduled_at=scheduler._iso(old), yt_scheduled_at=scheduler._iso(old))
    await scheduler._publish_due(bot)
    post2 = await db.get(pid2)
    assert post2.status == db.SCHEDULED and not post2.tg_message_id and post2.scheduled_at > scheduler._iso(datetime.now(timezone.utc)), post2

    # описание расписания для /slots: дни, окна и ближайшее свободное место
    desc = await scheduler.describe()
    assert "Telegram:" in desc and "YouTube:" in desc and "В очереди сейчас:" in desc and "Хабра" in desc, desc

    # перезапуск во время генерации: пост помечается упавшим и попадает в список для оповещения
    pid3 = await db.create(1, "текст 3", None)
    await db.update(pid3, status=db.GENERATING)
    assert await db.init() == [pid3]
    assert (await db.get(pid3)).status == db.FAILED
    print("queue OK")


asyncio.run(main())
