"""Telegram Admin Bot: приём постов, карточка модерации (Human-in-the-Loop), публикация."""

import asyncio
import json
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from datetime import datetime, timezone

from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardMarkup, LinkPreviewOptions, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from aiogram.types import ErrorEvent

from . import ai, alerts, analytics, cta, db, features, learning, linkedin, longform, pipeline, quality, reddit, scheduler, topics, version, youtube
from .config import settings
from .tg_format import TG_TEXT_LIMIT, escape, sanitize, truncate, visible_length

log = logging.getLogger(__name__)

router = Router()
router.message.filter(F.from_user.id.in_(settings.admin_id_set))
router.callback_query.filter(F.from_user.id.in_(settings.admin_id_set))

_tasks: set[asyncio.Task] = set()


async def _feature_off(message: Message, name: str) -> bool:
    """True и ответ пользователю, если функция выключена в FEATURES этого инстанса."""
    if features.enabled(name):
        return False
    await message.answer(f"Функция «{name}» выключена в этом инстансе (FEATURES в .env).")
    return True
MIN_POST_LEN = 150
LINKEDIN_COMPOSE_URL = "https://www.linkedin.com/feed/?shareActive=true"  # открывает окно нового поста; параметр не проверен

HELP_HEAD = (
    "<b>ContentOps Engine</b>\n\n"
    "Пришли сюда <b>ссылку на пост LinkedIn</b> или <b>его текст</b> (можно переслать сообщение).\n"
    "Я сделаю пост для Telegram и Shorts для YouTube и пришлю карточку модерации.\n\n"
)
# (функция из FEATURES или None, строка справки); строки выключенных функций в справке не показываются
HELP_LINES = [
    (None, "/topics — подобрать темы из лент (бот и сам присылает их каждый день)\n"),
    (None, "/queue — что стоит в очереди на публикацию\n"),
    ('habr', "/habr — собрать статью для Хабра из постов за месяц (раз в месяц бот делает это сам)\n"),
    ('habr', "/habr_from — статья для Хабра из твоего материала (текст или файл), а не из постов бота\n"),
    ('longvideo', "/longvideo — ролик на 5–7 минут (16:9) из твоего материала: сценарий, проверка вёрстки, сборка с озвучкой (прототип)\n"),
    ('devto', "/devto — английская версия последней статьи с Хабра для dev.to\n"),
    ('reddit', "/reddit — свежие обсуждения r/devops, r/ExperiencedDevs, r/kubernetes и заготовка ответа (отправляешь сам)\n"),
    ('week', "/week — итоги недели (по воскресеньям бот присылает их сам); пришли .xlsx из LinkedIn — добавлю цифры\n"),
    ('week', "/stats — таблица «пост → охват, пересылки, сохранения» по всем площадкам\n"),
    ('week', "/tgstats 12 1500 14 — записать просмотры и пересылки поста №12 из Telegram (Bot API их не отдаёт)\n"),
    (None, "/list — последние посты\n"),
    (None, "/health — проверить LLM, озвучку и токены YouTube\n"),
    (None, "/slots — расписание: когда что выходит и ближайшие свободные окна\n"),
    (None, "/version — какая версия бота сейчас работает и когда выложена\n"),
    (None, "/post &lt;id&gt; — показать карточку поста\n"),
    (None, "/cancel — выйти из режима редактирования"),
]


def help_text() -> str:
    return HELP_HEAD + "".join(t for f, t in HELP_LINES if f is None or features.enabled(f))


HELP = help_text()


class Flow(StatesGroup):
    waiting_source_text = State()  # ссылку не удалось прочитать — ждём текст
    editing = State()  # ждём новый текст поста или «ai: правки»
    regen_wishes = State()  # ждём пожелания к перегенерации
    waiting_opinion = State()  # тема принята, ждём позицию автора в одну фразу
    habr_material = State()  # /habr_from: ждём материал для статьи (текст или файл .txt/.md)
    habr_opinion = State()  # материал принят, ждём позицию автора для тезиса статьи
    long_material = State()  # /longvideo: ждём материал для ролика на 5–7 минут


class PostCB(CallbackData, prefix="p"):
    action: str
    post_id: int


class TopicCB(CallbackData, prefix="t"):
    action: str  # make | skip
    topic_id: int


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)

    def done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        if t.cancelled() or not t.exception():
            return
        exc = t.exception()
        log.error("фоновая задача упала", exc_info=exc)
        # без оповещения такая поломка видна только в логах: автор ждёт карточку, а она не придёт
        alert = asyncio.create_task(alerts.notify(f"⚠️ Фоновая задача упала: {type(exc).__name__}: {exc}", key=f"task:{type(exc).__name__}"))
        _tasks.add(alert)
        alert.add_done_callback(_tasks.discard)

    task.add_done_callback(done)


@router.error()
async def on_error(event: ErrorEvent) -> bool:
    """Любое необработанное исключение в обработчике: в лог и автору, чтобы кнопка не молчала без объяснения."""
    log.error("ошибка в обработчике", exc_info=event.exception)
    await alerts.notify(f"⚠️ Ошибка в боте: {type(event.exception).__name__}: {event.exception}",
                        key=f"handler:{type(event.exception).__name__}", every=600)
    return True


def _keyboard(post: db.Post) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    cb = lambda action: PostCB(action=action, post_id=post.id).pack()  # noqa: E731
    has_video = bool(post.video)
    if has_video:
        kb.button(text="✅ Опубликовать везде", callback_data=cb("all"))
    kb.button(text="🚀 Только Telegram", callback_data=cb("tg"))
    if has_video:
        kb.button(text="🎬 Только YouTube", callback_data=cb("yt"))
    queue_row = post.status in (db.REVIEW, db.SCHEDULED)
    if post.status == db.SCHEDULED:
        kb.button(text="🕒 Убрать из очереди", callback_data=cb("unqueue"))
    elif queue_row:
        kb.button(text="📅 В очередь на публикацию", callback_data=cb("queue"))
    kb.button(text="✏️ Редактировать текст", callback_data=cb("edit"))
    kb.button(text="🔁 Перегенерировать", callback_data=cb("regen"))
    if features.enabled("linkedin"):
        kb.button(text="💼 Пост для LinkedIn", callback_data=cb("li"))
    other =cta.TELEGRAM if cta.normalize(post.cta_mode) == cta.SUBSCRIBE else cta.SUBSCRIBE
    kb.button(text=f"📣 Призыв → {cta.LABELS[other]}", callback_data=cb("cta"))
    kb.button(text="🗑 Отклонить", callback_data=cb("drop"))
    sizes = [1, 2] if has_video else [1]
    sizes += ([1] if queue_row else []) + [2, 1, 1, 1]
    kb.adjust(*sizes)
    return kb.as_markup()


def _status_line(post: db.Post) -> str:
    lines = [f"<b>Пост #{post.id}</b> · статус: <code>{post.status}</code> · "
             f"призыв: {cta.LABELS[cta.normalize(post.cta_mode)]}"]
    if post.tg_message_id:
        lines.append(f"Telegram: опубликован {pipeline.channel_post_url(post.tg_message_id) or ''}")
    if post.youtube_video_id:
        lines.append(f"YouTube: https://youtube.com/shorts/{post.youtube_video_id}")
    if post.error:
        lines.append(f"⚠️ {escape(post.error)}")
    if post.status == db.SCHEDULED:
        lines.append(f"🕒 В очереди: {scheduler.when_text(post)}")
    if post.package and post.status in (db.REVIEW, db.SCHEDULED):
        lines.append(escape(quality.summary(pipeline.quality_report(post))))
    return "\n".join(lines)


async def send_card(bot: Bot, chat_id: int, post_id: int) -> None:
    post = await db.get(post_id)
    if post.status == db.FAILED or not post.package:
        kb = InlineKeyboardBuilder()
        kb.button(text="🔁 Повторить", callback_data=PostCB(action="regen", post_id=post.id).pack())
        await bot.send_message(chat_id, f"❌ {_status_line(post)}", reply_markup=kb.as_markup())
        return

    # 1. Превью поста ровно так, как он уйдёт в канал
    await bot.send_message(chat_id, "📄 <b>Превью поста для Telegram:</b>")
    length = visible_length(post.tg_html)
    problem = f"длина {length} > {TG_TEXT_LIMIT}" if length > TG_TEXT_LIMIT else None
    if not problem:
        try:
            await bot.send_message(chat_id, post.tg_html, link_preview_options=LinkPreviewOptions(is_disabled=True))
        except TelegramBadRequest as e:
            problem = str(e)
    if problem:
        await bot.send_message(chat_id, f"⚠️ Telegram не примет этот текст ({escape(problem)}) — поправь через ✏️. Исходник:")
        await bot.send_message(chat_id, truncate(post.tg_html, TG_TEXT_LIMIT), parse_mode=None)

    # 2. Видео + метаданные YouTube
    s = post.package.youtube_shorts_stream
    meta = (
        f"🎬 <b>{escape(s.youtube_title)}</b>\n\n"
        f"<b>Описание:</b> {escape(s.youtube_description)}\n\n"
        f"<b>Первый комментарий:</b> {escape(s.first_comment)}"
    )
    if post.video:
        try:
            await bot.send_video(
                chat_id, FSInputFile(post.video), caption=truncate(meta, 1000), width=1080, height=1920, supports_streaming=True,
                request_timeout=300,  # видео грузится дольше обычного запроса, особенно через прокси
            )
        except (TelegramBadRequest, TelegramNetworkError) as e:
            await bot.send_message(chat_id, f"{meta}\n\n⚠️ Видео не отправилось в чат ({escape(str(e))}), файл: <code>{post.video}</code>")
    else:
        await bot.send_message(chat_id, f"{meta}\n\n<b>Сценарий:</b>\n<blockquote expandable>{escape(s.full_audio_script)}</blockquote>")

    # 3. Пульт управления
    await bot.send_message(chat_id, _status_line(post), reply_markup=_keyboard(post))


async def _send_linkedin(bot: Bot, chat_id: int, post_id: int, data: dict, issues: list[str]) -> None:
    """Черновик LinkedIn обычным текстом, чтобы его можно было скопировать целиком."""
    # <pre> в Telegram копируется одним нажатием: тап по тексту — и он в буфере целиком
    await bot.send_message(chat_id, f"💼 <b>LinkedIn, пост #{post_id}</b> — нажми на текст, чтобы скопировать:\n\n"
                                    f"<pre>{escape(data['text'])}</pre>")
    await bot.send_message(chat_id, f"Первый комментарий (вставь после публикации):\n\n<pre>{escape(data['first_comment'])}</pre>",
                           link_preview_options=LinkPreviewOptions(is_disabled=True))
    kb = InlineKeyboardBuilder()
    # Cyrillic в ссылке раздувается втрое-вшестеро, и пост не влезает в URL: текст не подставляем, он уже в буфере
    kb.button(text="🔗 Открыть LinkedIn: новый пост", url=LINKEDIN_COMPOSE_URL)
    kb.button(text="🔁 Переписать", callback_data=PostCB(action="li_regen", post_id=post_id).pack())
    kb.adjust(1)
    note = "🟢 Автопроверка: замечаний нет" if not issues else "🟡 Проверь:\n" + "\n".join(f"• {i}" for i in issues)
    await bot.send_message(chat_id, note, parse_mode=None, reply_markup=kb.as_markup())


async def _run_generation(bot: Bot, chat_id: int, post_id: int, extra: str = "") -> None:
    status = await bot.send_message(chat_id, f"⏳ Пост #{post_id}: в очереди…")

    async def progress(text: str) -> None:
        try:
            await status.edit_text(f"⏳ Пост #{post_id}: {text}")
        except TelegramBadRequest:
            pass

    saved = await db.get(post_id)
    if saved and saved.opinion:
        # позиция идёт первой и с пометкой «главное»: слабая модель иначе уходит в пересказ исходника
        extra = f"ПОЗИЦИЯ АВТОРА (главное: пост и ролик несут её, исходник только материал): {saved.opinion}\n{extra}".strip()
    post = await pipeline.generate(post_id, extra, progress)
    # автоочередь: только то, что прошло автопроверку целиком; остальное ждёт глаз автора
    if settings.auto_queue and post.status == db.REVIEW and post.package and not pipeline.quality_report(post):
        try:
            await scheduler.enqueue(post_id)
        except RuntimeError:
            log.exception("автоочередь: не вышло поставить пост %s", post_id)
    await progress("готово 👇")
    await send_card(bot, chat_id, post_id)


async def _ask_opinion(bot: Bot, chat_id: int, post_id: int, state: FSMContext) -> None:
    """Перед генерацией спрашиваем позицию автора: без неё выходит пересказ чужого, как у всех."""
    await state.set_state(Flow.waiting_opinion)
    await state.update_data(post_id=post_id)
    await bot.send_message(
        chat_id,
        f"✍️ Пост #{post_id}. Твоя позиция в одну фразу: согласен, споришь, что видел на практике?\n"
        "Если есть случай из работы (что сломалось, сколько, как починил), добавь второй фразой: "
        "реальный опыт — то, что читатели запоминают и пересылают.\n"
        "Всё пойдёт в тезис поста и хук ролика. «-» — пропустить, будет пересказ.")


async def _start_from_text(message: Message, bot: Bot, text: str, url: str | None, state: FSMContext) -> None:
    post_id = await db.create(message.chat.id, text, url)
    await _ask_opinion(bot, message.chat.id, post_id, state)


# ---------- команды ----------

@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("health"))
async def cmd_health(message: Message) -> None:
    lines = [
        f"🧠 LLM: <code>{settings.llm_provider} / {settings.llm_model}</code>",
        f"🎙 TTS: <code>{settings.tts_provider}</code> {'✅' if settings.tts_enabled else '❌ нет ключа'}",
        f"📣 Канал: <code>{escape(settings.tg_channel_id)}</code>",
    ]
    via = "через Cloudflare Worker" if settings.telegram_api_url else "напрямую"
    try:
        scope = await youtube.check()
        rights = "только загрузка 🔒" if scope.strip() == youtube.SCOPES[0] else f"⚠️ лишние права: {escape(scope)}"
        lines.append(f"▶️ YouTube ({via}) ✅ · {rights}")
    except youtube.YouTubeError as e:
        lines.append(f"▶️ YouTube ({via}) ❌ {escape(str(e))}")
    if settings.youtube_stats_enabled:
        try:
            await youtube.check(stats=True)
            lines.append("📊 YouTube-статистика ✅ (токен только на чтение)")
        except youtube.YouTubeError as e:
            lines.append(f"📊 YouTube-статистика ❌ {escape(str(e))}")
    else:
        lines.append("📊 YouTube-статистика: не подключена (scripts/youtube_auth.py --stats)")
    await message.answer("\n".join(lines))


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ок, отменил.")


@router.message(Command("list"))
async def cmd_list(message: Message) -> None:
    posts = await db.latest(15)
    if not posts:
        await message.answer("Пока пусто. Пришли ссылку или текст поста.")
        return
    lines = []
    for p in posts:
        title = p.package.youtube_shorts_stream.youtube_title if p.package else p.source_text[:60]
        lines.append(f"#{p.id} · <code>{p.status}</code> · {escape(truncate(title, 60))}")
    await message.answer("\n".join(lines) + "\n\n/post &lt;id&gt; — открыть карточку")


def _topic_text(t: db.Topic) -> str:
    return (f"💡 <b>{escape(t.title)}</b>\n{escape(truncate(t.summary, 300))}\n\n"
            f"<i>{escape(t.source)} · оценка {t.score:.0f}</i>\n{escape(t.url)}")


async def send_topics(bot: Bot, chat_id: int) -> None:
    """Подборка тем с кнопками «Сделать» / «Пропустить». Вызывается и по команде, и планировщиком."""
    found = await topics.find()
    if not found:
        await bot.send_message(chat_id, "Новых подходящих тем нет: ленты пусты, недоступны или всё уже предлагалось. "
                                        "Список лент: assets/sources.txt. Можно прислать свою ссылку или текст.")
        return
    await bot.send_message(chat_id, f"🗞 Темы на сегодня ({len(found)}). Выбери, что снимать:")
    for t in found:
        kb = InlineKeyboardBuilder()
        kb.button(text="🎬 Сделать", callback_data=TopicCB(action="make", topic_id=t.id).pack())
        kb.button(text="⏭ Пропустить", callback_data=TopicCB(action="skip", topic_id=t.id).pack())
        await bot.send_message(chat_id, _topic_text(t), reply_markup=kb.as_markup(),
                               link_preview_options=LinkPreviewOptions(is_disabled=True))


@router.message(Command("topics"))
async def cmd_topics(message: Message, bot: Bot) -> None:
    wait = await message.answer("🔎 Смотрю ленты…")
    await send_topics(bot, message.chat.id)
    await wait.delete()


@router.message(Command("version"))
async def cmd_version(message: Message) -> None:
    await message.answer(escape(version.describe()))


@router.message(Command("slots"))
async def cmd_slots(message: Message) -> None:
    await message.answer(escape(await scheduler.describe()))


@router.message(Command("queue"))
async def cmd_queue(message: Message) -> None:
    queued = await db.queued_posts()
    if not queued:
        await message.answer("Очередь пуста. Кнопка «📅 В очередь» есть в карточке поста.")
        return
    lines = []
    for p in queued:
        title = p.package.youtube_shorts_stream.youtube_title if p.package else p.source_text[:60]
        lines.append(f"#{p.id} · {escape(truncate(title, 60))}\n    {scheduler.when_text(p)}")
    await message.answer("🕒 <b>Очередь:</b>\n" + "\n".join(lines))


async def send_habr(bot: Bot, chat_id: int, quiet: bool = False) -> None:
    """Статья для Хабра из постов месяца. quiet — вызов планировщика: если постов мало, молчим."""
    posts = await longform.month_posts(30)
    if len(posts) < 2:
        if not quiet:
            await bot.send_message(chat_id, f"Для статьи нужно хотя бы 2 опубликованных поста за 30 дней, сейчас {len(posts)}.")
        return
    status = await bot.send_message(chat_id, f"⏳ Статья для Хабра из {len(posts)} постов: несколько запросов к модели, "
                                             "до часа. Пришлю сама, ничего нажимать не нужно.")

    async def progress(text: str) -> None:
        try:
            await status.edit_text(f"⏳ Хабр: {text}")
        except TelegramBadRequest:
            pass

    try:
        md, plan = await longform.write_habr(posts, progress)
    except ai.AIError as e:
        await status.edit_text(f"❌ Не вышло собрать статью: {escape(str(e))}")
        return
    await _deliver_habr(bot, chat_id, md, plan, status, f"habr_{datetime.now().strftime('%Y-%m')}.md")


async def _deliver_habr(bot: Bot, chat_id: int, md: str, plan, status: Message, name: str) -> None:
    """Общий конец /habr и /habr_from: запомнить статью для /devto и отправить файл с подписью."""
    await db.set_state("habr_last", json.dumps({"md": md, "name": name}, ensure_ascii=False))   # для /devto
    # всё, что Хабр просит при публикации, лежит в подписи: заголовок, теги, хабы — копируй и вставляй
    caption = (f"📰 <b>Черновик для Хабра</b>\n\n<b>Заголовок:</b>\n<pre>{escape(plan.title)}</pre>\n"
               f"<b>Теги:</b> <code>{escape(', '.join(plan.tags))}</code>\n"
               f"<b>Хабы:</b> {escape(', '.join(plan.hubs))}\n\n"
               "Открой файл, выдели всё, скопируй и вставь в редактор Хабра (режим Markdown). Факты перечитай.")
    await bot.send_document(chat_id, BufferedInputFile(md.encode("utf-8"), name), caption=truncate(caption, 1000))
    await status.delete()


async def send_digest(bot: Bot, chat_id: int) -> None:
    posts = await db.latest(200)
    yt, yt_error = (await analytics.youtube_rows(posts)) if settings.youtube_stats_enabled else (None, "")
    li = await db.linkedin_stats()
    perf, adj = await learning.update(posts, li, yt or [])   # заодно подстраивает подбор тем, когда данных хватает
    text = analytics.build_digest(posts, li, yt=yt, yt_error=yt_error, learned=learning.summary(perf, adj))
    await bot.send_message(chat_id, text, parse_mode=None)


@router.message(Command("week"))
async def cmd_week(message: Message, bot: Bot) -> None:
    if await _feature_off(message, "week"):
        return
    await send_digest(bot, message.chat.id)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if await _feature_off(message, "week"):
        return
    posts = await db.latest(200)
    yt, _ = (await analytics.youtube_rows(posts, days=90)) if settings.youtube_stats_enabled else ([], "")
    text = analytics.build_post_table(posts, await db.linkedin_stats(), yt or [], await db.tg_stats())
    await message.answer(text, parse_mode=None)


@router.message(Command("tgstats"))
async def cmd_tgstats(message: Message, command: CommandObject) -> None:
    """/tgstats 12 1500 14 [3] — пост №12: просмотров 1500, пересылок 14, реакций 3. Цифры видны автору под постом в канале."""
    if await _feature_off(message, "week"):
        return
    nums = (command.args or "").replace("#", "").split()
    if len(nums) not in (3, 4) or not all(n.isdigit() for n in nums):
        await message.answer("Использование: /tgstats номер_поста просмотры пересылки [реакции]\nНапример: /tgstats 12 1500 14")
        return
    post_id, views, forwards, *rest = (int(n) for n in nums)
    post = await db.get(post_id)
    if not post or post.status != db.PUBLISHED:
        await message.answer("Такого опубликованного поста нет.")
        return
    await db.set_tg_stats(post_id, views, forwards, rest[0] if rest else 0)
    await message.answer(f"Записал по посту #{post_id}: просмотров {views}, пересылок {forwards}. Таблица: /stats")


@router.message(F.document.file_name.func(lambda n: bool(n) and n.lower().endswith(".xlsx")))
async def on_linkedin_export(message: Message, bot: Bot) -> None:
    """Выгрузка «Post performance» из LinkedIn: разбираем, запоминаем, повторная загрузка обновляет цифры."""
    if await _feature_off(message, "linkedin"):
        return
    buf = await bot.download(message.document)
    rows = analytics.parse_linkedin_xlsx(buf.read())
    if not rows:
        await message.answer("Не похоже на выгрузку «Post performance» из LinkedIn: не нашёл нужных колонок.")
        return
    await db.upsert_linkedin_stats(rows)
    await db.set_state("li_export_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))   # для напоминания о выгрузке
    total = sum(r["impressions"] for r in rows)
    await message.answer(f"📥 Принял: постов {len(rows)}, показов {total}. Сводка придёт в воскресенье, "
                         "или посмотри сейчас: /week")


@router.message(Command("habr"))
async def cmd_habr(message: Message, bot: Bot) -> None:
    if await _feature_off(message, "habr"):
        return
    _spawn(send_habr(bot, message.chat.id))


HABR_MATERIAL_MIN = 500
HABR_MATERIAL_MAX_BYTES = 1_000_000


@router.message(Command("habr_from"))
async def cmd_habr_from(message: Message, state: FSMContext) -> None:
    if await _feature_off(message, "habr"):
        return
    await state.set_state(Flow.habr_material)
    await message.answer("📰 Пришли материал для статьи: текст сообщением или файл .txt/.md (заметки, разбор инцидента, "
                         "конспект). Статья построится только на его фактах. /cancel — отмена.")


@router.message(Flow.habr_material, F.document | F.text)
async def on_habr_material(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.document:
        name = (message.document.file_name or "").lower()
        if not name.endswith((".txt", ".md")) or (message.document.file_size or 0) > HABR_MATERIAL_MAX_BYTES:
            await message.answer("Нужен файл .txt или .md до 1 МБ, либо просто текст сообщением.")
            return
        buf = await bot.download(message.document)
        text = buf.read().decode("utf-8", errors="replace").strip()
    else:
        text = (message.text or "").strip()
    if len(text) < HABR_MATERIAL_MIN:
        await message.answer(f"Слишком мало для статьи ({len(text)} знаков, нужно от {HABR_MATERIAL_MIN}). Пришли больше.")
        return
    await state.update_data(material=text)
    await state.set_state(Flow.habr_opinion)
    await message.answer("✍️ Твоя позиция в одну фразу: она станет тезисом статьи. Если есть случай из работы, "
                         "добавь второй фразой. «-» — пропустить, будет разбор материала.")


@router.message(Flow.habr_opinion, F.text)
async def on_habr_opinion(message: Message, state: FSMContext, bot: Bot) -> None:
    material = (await state.get_data()).get("material", "")
    await state.clear()
    opinion = "" if message.text.strip() in {"-", "—"} else message.text.strip()[:600]
    _spawn(send_habr_from(bot, message.chat.id, material, opinion))


# ---------- /longvideo: длинный ролик (прототип app/explainer*) ----------

LONG_MATERIAL_MIN = 1500
TG_FILE_LIMIT = 48 * 1024 * 1024    # Bot API принимает файлы до 50 МБ; запас на заголовки
_long_render_lock = asyncio.Lock()   # одна сборка за раз: Chromium и ffmpeg на сервере делят 4 ГБ с чужими проектами


class LongCB(CallbackData, prefix="lv"):
    mode: str   # "fake" — проверка вёрстки без озвучки, "voice" — сборка с озвучкой (платная)


@router.message(Command("longvideo"))
async def cmd_longvideo(message: Message, state: FSMContext) -> None:
    if await _feature_off(message, "longvideo"):
        return
    await state.set_state(Flow.long_material)
    await message.answer(f"🎬 Пришли материал для ролика на 5–7 минут: текст или файл .txt/.md от {LONG_MATERIAL_MIN} знаков "
                         "(разбор инцидента, отчёт, статья). Сначала напишу сценарий и покажу; сборка с озвучкой платная и "
                         "запускается отдельной кнопкой. /cancel — отмена.")


@router.message(Flow.long_material, F.document | F.text)
async def on_long_material(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.document:
        name = (message.document.file_name or "").lower()
        if not name.endswith((".txt", ".md")) or (message.document.file_size or 0) > HABR_MATERIAL_MAX_BYTES:
            await message.answer("Нужен файл .txt или .md до 1 МБ, либо просто текст сообщением.")
            return
        text = (await bot.download(message.document)).read().decode("utf-8", errors="replace").strip()
    else:
        text = (message.text or "").strip()
    if len(text) < LONG_MATERIAL_MIN:
        await message.answer(f"Слишком мало для ролика ({len(text)} знаков, нужно от {LONG_MATERIAL_MIN}).")
        return
    await state.clear()
    _spawn(send_long_script(bot, message.chat.id, text))


async def send_long_script(bot: Bot, chat_id: int, material: str) -> None:
    from . import explainer_script
    status = await bot.send_message(chat_id, "⏳ Пишу сценарий длинного ролика: план глав, текст и сцены, сверка цифр. До 15 минут.")
    try:
        script = await explainer_script.write(material, 9)
    except ai.AIError as e:
        await status.edit_text(f"❌ Не вышло написать сценарий: {escape(str(e))}")
        return
    except Exception as e:  # noqa: BLE001 — прототип: любой сбой сообщаем автору, а не роняем задачу молча
        log.exception("long script failed")
        await status.edit_text(f"❌ Сбой сценария: {escape(type(e).__name__)}: {escape(str(e)[:200])}")
        return
    await db.set_state("long_script", json.dumps(script, ensure_ascii=False))
    words = sum(len(c["text"].split()) for c in script["chapters"])
    warn = ("\n⚠️ " + "\n⚠️ ".join(script["warnings"][:5]) + "\nЭти числа не найдены в материале: проверь до озвучки.") if script["warnings"] else ""
    kb = InlineKeyboardBuilder()
    kb.button(text="🧪 Проверить вёрстку (бесплатно, без голоса)", callback_data=LongCB(mode="fake").pack())
    kb.button(text="🎙 Собрать с озвучкой (платно)", callback_data=LongCB(mode="voice").pack())
    kb.adjust(1)
    await bot.send_document(
        chat_id, BufferedInputFile(json.dumps(script, ensure_ascii=False, indent=2).encode("utf-8"), "long_script.json"),
        caption=truncate(f"🎬 Сценарий «{escape(script['title'])}»: глав {len(script['chapters'])}, слов {words} "
                         f"(~{words / 2.4 / 60:.1f} мин).{warn}\nКак читать и править: сначала проверка вёрстки.", 1000),
        reply_markup=kb.as_markup())
    await status.delete()


async def _fit_telegram(path, seconds: float):
    """Bot API не принимает файлы больше 50 МБ: длинный ролик пережимаем под лимит по длительности."""
    if path.stat().st_size <= TG_FILE_LIMIT:
        return path
    out = path.with_name("explainer_tg.mp4")
    video_kbps = max(300, int(TG_FILE_LIMIT * 8 / 1000 / max(seconds, 1) * 0.9) - 96)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(path), "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{video_kbps}k",
        "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps * 2}k", "-c:a", "aac", "-b:a", "96k", str(out),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
    return out if proc.returncode == 0 and out.exists() and out.stat().st_size <= TG_FILE_LIMIT else None


@router.callback_query(LongCB.filter())
async def on_long_render(query: CallbackQuery, callback_data: LongCB, bot: Bot) -> None:
    raw = await db.get_state("long_script")
    if not raw:
        await query.answer("Сценария нет, начни с /longvideo", show_alert=True)
        return
    if _long_render_lock.locked():
        await query.answer("Сборка уже идёт, дождись её окончания", show_alert=True)
        return
    await query.answer("Запускаю")
    _spawn(render_long(bot, query.message.chat.id, json.loads(raw), fake=callback_data.mode == "fake"))


async def render_long(bot: Bot, chat_id: int, script: dict, fake: bool) -> None:
    from pathlib import Path

    from . import explainer
    async with _long_render_lock:
        status = await bot.send_message(
            chat_id, "⏳ Собираю ролик " + ("без озвучки, только вёрстка (60 с), " if fake else "с озвучкой, ") +
            "это долго: Chromium и ffmpeg. Пришлю сама.")
        workdir = settings.media_dir / "explainer"
        try:
            out = await explainer.render(script, workdir, fake=fake, seconds=60 if fake else None)
            probe = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            seconds = float((await probe.communicate())[0].decode().strip() or 60)
            send = await _fit_telegram(Path(out), seconds)
        except Exception as e:  # noqa: BLE001 — прототип: показываем причину автору
            log.exception("long render failed")
            await status.edit_text(f"❌ Сборка упала: {escape(type(e).__name__)}: {escape(str(e)[:300])}")
            return
        if not send:
            await status.edit_text(f"✅ Ролик собран, но не влезает в лимит Telegram. Файл на сервере: {escape(str(out))}")
            return
        await bot.send_document(chat_id, FSInputFile(send), caption=f"🎬 {escape(script['title'])} ({seconds / 60:.1f} мин)"
                                + (" · проверка вёрстки, без голоса" if fake else ""))
        await status.delete()


async def send_habr_from(bot: Bot, chat_id: int, material: str, opinion: str) -> None:
    status = await bot.send_message(chat_id, "⏳ Статья для Хабра из твоего материала: несколько запросов к модели, "
                                             "до часа. Пришлю сама, ничего нажимать не нужно.")

    async def progress(text: str) -> None:
        try:
            await status.edit_text(f"⏳ Хабр: {text}")
        except TelegramBadRequest:
            pass

    try:
        md, plan = await longform.write_habr_from_material(material, opinion, progress)
    except ai.AIError as e:
        await status.edit_text(f"❌ Не вышло собрать статью: {escape(str(e))}")
        return
    await _deliver_habr(bot, chat_id, md, plan, status, f"habr_material_{datetime.now().strftime('%Y-%m-%d_%H%M')}.md")


async def send_devto(bot: Bot, chat_id: int) -> None:
    """Английская версия последней статьи для Хабра. Публиковать на dev.to автор будет сам, canonical — ссылка на Хабр."""
    raw = await db.get_state("habr_last")
    if not raw:
        await bot.send_message(chat_id, "Сначала нужна статья для Хабра: /habr. Английскую версию делаю из последней.")
        return
    md = json.loads(raw)["md"]
    status = await bot.send_message(chat_id, "⏳ Перевожу статью на английский: несколько запросов к модели, до получаса.")

    async def progress(text: str) -> None:
        try:
            await status.edit_text(f"⏳ dev.to: {text}")
        except TelegramBadRequest:
            pass

    try:
        out, meta = await longform.write_devto(md, progress)
    except ai.AIError as e:
        await status.edit_text(f"❌ Не вышло перевести статью: {escape(str(e))}")
        return
    caption = (f"📰 <b>Черновик для dev.to</b>\n\n<b>Title:</b>\n<pre>{escape(meta.title)}</pre>\n"
               f"<b>Tags:</b> <code>{escape(', '.join(meta.tags))}</code>\n\n"
               "Публикуй после Хабра: в поле <code>canonical_url</code> вставь ссылку на статью на Хабре (так поиск "
               "не сочтёт её копией). Факты перечитай. Правила dev.to по самопиару и переводам проверь сам: я их не читал.")
    await bot.send_document(chat_id, BufferedInputFile(out.encode("utf-8"), "devto_" + json.loads(raw)["name"]),
                            caption=truncate(caption, 1000))
    await status.delete()


@router.message(Command("devto"))
async def cmd_devto(message: Message, bot: Bot) -> None:
    if await _feature_off(message, "devto"):
        return
    _spawn(send_devto(bot, message.chat.id))


class RedditCB(CallbackData, prefix="rd"):
    idx: int


_reddit_threads: list = []   # последняя подборка; после перезапуска бота пропадает — тогда нужен новый /reddit


@router.message(Command("reddit"))
async def cmd_reddit(message: Message, bot: Bot) -> None:
    if await _feature_off(message, "reddit"):
        return
    wait = await message.answer("🔎 Смотрю Reddit (по одному сообществу, с паузами)…")
    threads = await reddit.fetch_threads()
    await wait.delete()
    if not threads:
        await message.answer("Reddit не ответил или свежих обсуждений нет (он режет частые запросы, 429). Попробуй позже.")
        return
    _reddit_threads[:] = threads
    await message.answer("🧵 Обсуждения за сутки. Отвечай только по существу, без ссылок на себя. "
                         "Кнопка даёт заготовку: перепиши своими словами и добавь свой опыт.")
    for i, t in enumerate(threads):
        kb = InlineKeyboardBuilder()
        kb.button(text="✍️ Заготовка ответа", callback_data=RedditCB(idx=i).pack())
        await bot.send_message(message.chat.id, f"<b>{escape(t.source)}</b>: {escape(truncate(t.title, 200))}\n{escape(t.url)}",
                               reply_markup=kb.as_markup(), link_preview_options=LinkPreviewOptions(is_disabled=True))


@router.callback_query(RedditCB.filter())
async def on_reddit(query: CallbackQuery, callback_data: RedditCB, bot: Bot) -> None:
    if callback_data.idx >= len(_reddit_threads):
        await query.answer("Подборка устарела, нажми /reddit ещё раз", show_alert=True)
        return
    await query.answer("Пишу заготовку…")
    t = _reddit_threads[callback_data.idx]
    try:
        text = await reddit.draft_reply(t)
    except ai.AIError as e:
        await bot.send_message(query.message.chat.id, f"❌ Не вышло написать заготовку: {escape(str(e))}")
        return
    await bot.send_message(query.message.chat.id, f"<b>Заготовка для {escape(t.source)}</b> (копируй, правь, отправляй сам):\n<pre>{escape(text)}</pre>")


@router.message(Command("post"))
async def cmd_post(message: Message, command: CommandObject, bot: Bot) -> None:
    if not command.args or not command.args.strip().lstrip("#").isdigit():
        await message.answer("Использование: /post 12")
        return
    post_id = int(command.args.strip().lstrip("#"))
    if not await db.get(post_id):
        await message.answer("Нет такого поста")
        return
    await send_card(bot, message.chat.id, post_id)


# ---------- состояния ----------

@router.message(Flow.waiting_source_text, F.text | F.caption)
async def on_source_text(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    text = (message.text or message.caption).strip()
    if len(text) < MIN_POST_LEN:
        await message.answer("Слишком коротко для поста. Пришли полный текст.")
        return
    await _start_from_text(message, bot, text, data.get("url"), state)


@router.message(Flow.editing, F.text)
async def on_edit(message: Message, state: FSMContext, bot: Bot) -> None:
    post_id = (await state.get_data())["post_id"]
    await state.clear()
    raw = message.text.strip()
    if raw.lower().startswith(("ai:", "ии:")):
        wait = await message.answer("🧠 Правлю текст через Claude…")
        try:
            await pipeline.ai_edit_telegram(post_id, raw.split(":", 1)[1].strip())
        except ai.AIError as e:
            await wait.edit_text(f"❌ {escape(str(e))}")
            return
        await wait.delete()
    else:
        # html_text сохраняет форматирование, которое автор поставил прямо в Telegram
        await db.update(post_id, tg_html=sanitize(message.html_text))
    await send_card(bot, message.chat.id, post_id)


@router.message(Flow.regen_wishes, F.text)
async def on_regen_wishes(message: Message, state: FSMContext, bot: Bot) -> None:
    post_id = (await state.get_data())["post_id"]
    await state.clear()
    wishes = "" if message.text.strip() in {"-", "—"} else message.text.strip()
    _spawn(_run_generation(bot, message.chat.id, post_id, wishes))


@router.message(Flow.waiting_opinion, F.text)
async def on_opinion(message: Message, state: FSMContext, bot: Bot) -> None:
    post_id = (await state.get_data())["post_id"]
    await state.clear()
    opinion = "" if message.text.strip() in {"-", "—"} else message.text.strip()[:600]
    if opinion:
        await db.update(post_id, opinion=opinion)
    _spawn(_run_generation(bot, message.chat.id, post_id))


# ---------- входящий пост ----------

@router.message(F.text | F.caption)
async def on_incoming(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or message.caption or "").strip()
    url = linkedin.find_url(text)
    rest = text.replace(url, "").strip() if url else text

    if len(rest) >= MIN_POST_LEN:  # прислали текст (с ссылкой или без) — берём текст
        await _start_from_text(message, bot, rest, url, state)
        return
    if not url:
        await message.answer("Пришли ссылку на пост LinkedIn или его полный текст.\n\n" + HELP)
        return

    wait = await message.answer("🔎 Читаю пост из LinkedIn…")
    fetched = await linkedin.fetch_post_text(url)
    if not fetched:
        await state.set_state(Flow.waiting_source_text)
        await state.update_data(url=url)
        await wait.edit_text("LinkedIn не отдал текст (пост закрыт или нужен логин). Скопируй и пришли текст поста сюда.")
        return
    await wait.edit_text(f"📥 Забрал пост ({len(fetched)} символов):\n<blockquote expandable>{escape(truncate(fetched, 3000))}</blockquote>")
    await _start_from_text(message, bot, fetched, url, state)


# ---------- кнопки модерации ----------

@router.callback_query(PostCB.filter())
async def on_button(query: CallbackQuery, callback_data: PostCB, state: FSMContext, bot: Bot) -> None:
    post = await db.get(callback_data.post_id)
    chat_id = query.message.chat.id
    if not post:
        await query.answer("Пост не найден", show_alert=True)
        return
    if post.status in (db.GENERATING, db.PUBLISHING):
        await query.answer("Подожди, пост сейчас в работе", show_alert=True)
        return

    action = callback_data.action
    if action in ("all", "tg", "yt"):
        await query.answer("Публикую…")
        await query.message.edit_reply_markup(reply_markup=None)

        async def run() -> None:
            report = await pipeline.publish(bot, post.id, to_tg=action in ("all", "tg"), to_yt=action in ("all", "yt"))
            fresh = await db.get(post.id)
            await bot.send_message(chat_id, "\n".join(escape(r) for r in report), link_preview_options=LinkPreviewOptions(is_disabled=True))
            await bot.send_message(chat_id, _status_line(fresh), reply_markup=_keyboard(fresh))

        _spawn(run())
    elif action == "edit":
        await query.answer()
        await state.set_state(Flow.editing)
        await state.update_data(post_id=post.id)
        await bot.send_message(
            chat_id,
            f"✏️ Пост #{post.id}. Два варианта:\n"
            "• скопируй превью, поправь прямо в Telegram (жирный/код сохранятся) и пришли сюда — заменю как есть;\n"
            "• или напиши <code>ai: сделай короче и добавь пример с kubectl</code> — Claude перепишет по правкам.\n\n"
            "/cancel — отмена",
        )
    elif action == "regen":
        await query.answer()
        await state.set_state(Flow.regen_wishes)
        await state.update_data(post_id=post.id)
        await bot.send_message(chat_id, f"🔁 Пост #{post.id}: напиши пожелания к новой версии (тон, акценты, другой hook) или отправь «-», чтобы просто перегенерировать.")
    elif action == "cta":
        new_mode = cta.TELEGRAM if cta.normalize(post.cta_mode) == cta.SUBSCRIBE else cta.SUBSCRIBE
        await db.update(post.id, cta_mode=new_mode)
        await query.answer(f"Призыв: {cta.LABELS[new_mode]}")
        await query.message.edit_reply_markup(reply_markup=None)

        async def rebuild() -> None:
            # призыв звучит в озвучке и стоит в финальной сцене — ролик нужно пересобрать
            status = await bot.send_message(chat_id, f"⏳ Пост #{post.id}: пересобираю ролик под новый призыв…")

            async def progress(text: str) -> None:
                try:
                    await status.edit_text(f"⏳ Пост #{post.id}: {text}")
                except TelegramBadRequest:
                    pass

            await db.update(post.id, status=db.GENERATING)
            await pipeline.build_media(post.id, post.source_text, post.package, progress)
            await db.update(post.id, status=db.REVIEW)
            await send_card(bot, chat_id, post.id)

        _spawn(rebuild())
    elif action == "li":
        if not features.enabled("linkedin"):
            await query.answer("LinkedIn выключен в этом инстансе (FEATURES)", show_alert=True)
            return
        await query.answer()
        if post.linkedin_json:
            await _send_linkedin(bot, chat_id, post.id, json.loads(post.linkedin_json), [])
            return
        status = await bot.send_message(chat_id, f"⏳ Пост #{post.id}: пишу вариант для LinkedIn (несколько минут)…")

        async def write() -> None:
            try:
                li, issues = await longform.write_linkedin(post.source_text, opinion=post.opinion or "")
            except ai.AIError as e:
                await status.edit_text(f"❌ LinkedIn: {escape(str(e))}")
                return
            data = li.model_dump()
            await db.update(post.id, linkedin_json=json.dumps(data, ensure_ascii=False))
            await status.delete()
            await _send_linkedin(bot, chat_id, post.id, data, issues)

        _spawn(write())
    elif action == "li_regen":
        await query.answer()
        await db.update(post.id, linkedin_json=None)
        await query.message.edit_reply_markup(reply_markup=None)
        await bot.send_message(chat_id, "Ок, жми «💼 Пост для LinkedIn» в карточке ещё раз — напишу заново.")
    elif action in ("queue", "unqueue"):
        if action == "queue":
            if post.status != db.REVIEW:
                await query.answer("В очередь можно поставить только пост на проверке", show_alert=True)
                return
            try:
                await scheduler.enqueue(post.id)
            except RuntimeError as e:
                await query.answer(str(e), show_alert=True)
                return
            await query.answer("Поставил в очередь")
        else:
            await scheduler.dequeue(post.id)
            await query.answer("Убрал из очереди")
        fresh = await db.get(post.id)
        await query.message.edit_text(_status_line(fresh), reply_markup=_keyboard(fresh))
    elif action == "drop":
        await db.update(post.id, status=db.FAILED, error="отклонён автором")
        await query.answer("Отклонён")
        await query.message.edit_text(f"🗑 Пост #{post.id} отклонён")


# ---------- кнопки подборки тем ----------

@router.callback_query(TopicCB.filter())
async def on_topic(query: CallbackQuery, callback_data: TopicCB, state: FSMContext, bot: Bot) -> None:
    topic = await db.get_topic(callback_data.topic_id)
    if not topic:
        await query.answer("Тема не найдена", show_alert=True)
        return
    if topic.status != "offered":
        await query.answer("Эту тему уже разобрали", show_alert=True)
        return
    chat_id = query.message.chat.id
    if callback_data.action == "skip":
        await db.set_topic_status(topic.id, "skipped")
        await query.answer("Пропустил")
        await query.message.edit_reply_markup(reply_markup=None)
        return

    await db.set_topic_status(topic.id, "used")   # до загрузки: двойной тап не запустит две генерации
    await query.answer("Беру в работу")
    await query.message.edit_reply_markup(reply_markup=None)
    article = await topics.fetch_article(topic.url)
    text = f"{topic.title}\n\n{article or topic.summary}"
    if len(article or topic.summary) < MIN_POST_LEN:
        await bot.send_message(chat_id, f"Не смог прочитать статью: {escape(topic.url)}\nСкопируй её текст и пришли сюда.",
                               link_preview_options=LinkPreviewOptions(is_disabled=True))
        return
    post_id = await db.create(chat_id, text, topic.url)
    await db.update(post_id, credit=f"{topic.source}\n{topic.url}")   # «Источник» только для тем из лент, до генерации
    await _ask_opinion(bot, chat_id, post_id, state)
