import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from . import alerts, db, scheduler, version
from .bot import router, send_digest, send_habr, send_topics
from .config import settings


def make_bot() -> Bot:
    """Bot client honouring TELEGRAM_API_URL: с сервера в РФ api.telegram.org недоступен — ходим через Cloudflare Worker."""
    session = None
    if settings.telegram_api_url:
        session = AiohttpSession()
        session.api = TelegramAPIServer.from_base(settings.telegram_api_url.rstrip("/"))
    return Bot(settings.telegram_bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx на уровне INFO пишет адрес запроса целиком, а ключ Pixabay передаётся в адресе (?key=…): в журнал он попадать не должен
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    interrupted = await db.init()
    bot = make_bot()
    alerts.init(bot)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logging.info(
        "ContentOps started: llm=%s/%s tts=%s youtube=%s channel=%s telegram_api=%s",
        settings.llm_provider, settings.llm_model, settings.tts_enabled, settings.youtube_enabled, settings.tg_channel_id,
        settings.telegram_api_url or "direct",
    )
    # перезапуск виден сразу: если бот падал сам, автор узнает об этом и о прерванных постах
    note = f"🟢 Бот запущен ({version.short()})."
    if interrupted:
        note += f" Перезапуск прервал посты: {', '.join(f'#{i}' for i in interrupted)}. Открой их через /post и нажми «Повторить»."
    await alerts.notify(note)
    task = asyncio.create_task(scheduler.run(bot, send_topics, send_habr, send_digest))   # очередь публикаций и подборка тем
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
