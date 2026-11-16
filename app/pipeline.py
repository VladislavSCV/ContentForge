"""Оркестратор: AI-трансформация ➔ медиа-пакет ➔ публикация."""

import asyncio
import html
import re
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiogram import Bot
from aiogram.types import LinkPreviewOptions

from . import ai, cta, db, profiles, quality, stock, topics, tts, video, youtube
from .config import settings
from .schemas import ContentPackage
from .tg_format import TG_TEXT_LIMIT, build_post, visible_length

log = logging.getLogger(__name__)

Progress = Callable[[str], Awaitable[None]]

# ffmpeg грузит CPU целиком — рендерим по одному ролику за раз
_render_lock = asyncio.Semaphore(1)


async def _noop(_: str) -> None:
    pass


def _scrub(text: str) -> str:
    """Убираем из речи всё, что синтезатор прочитает криво."""
    # \S* с требованием буквы/слэша на конце: иначе URL «съедает» финальный знак препинания
    text = re.sub(r"https?://\S*[\w/]|t\.me/\S*[\w/]|@[\w_]+", "", text)
    # название канала вслух не произносим — синтезатор читает «name2go8» по буквам
    text = re.sub(rf"«?{re.escape(settings.brand_name)}»?", "", text, flags=re.I)
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]|<[^>]+>", "", text)
    text = re.sub(r"[^\w\s.,!?:;\-—–«»\"'%$€₽]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    # после вырезанных скобок остаётся обрубок вроде «индекс по .»: висящий предлог перед точкой убираем
    text = re.sub(r"\s+(?:по|в|на|с|со|к|для|из|от|и)\s*(?=[.!?]|$)", "", text)
    # слабая модель коверкает привычные термины; на экране в субтитрах опечатка заметнее, чем на слух
    return re.sub(r"\b([Пп])острес", r"\1остгрес", text)


def _drop_model_cta(text: str) -> str:
    """Модель иногда всё равно дописывает свой призыв — убираем последнюю фразу про подписку/канал."""
    trimmed = re.sub(r"[^.!?]*\b(подпиш|подписывай|телеграм|telegram|канал)\w*[^.!?]*[.!?]?\s*$",
                     "", text, flags=re.I).strip()
    # если призывом было почти всё — оставляем как есть
    return trimmed if trimmed and len(trimmed) >= 0.3 * len(text) else text


def voice_segments(pkg: ContentPackage, cta_mode: str | None = None) -> tuple[list[str], bool]:
    """Речь диктора по смысловым частям: хук, проблема, решение, главная мысль, призыв.

    Второй элемент — признак, что части совпадают с full_audio_script. Тогда монтаж режет сцены
    ровно по границам смысла, а не по эвристике. Если автор или подгонка длины правили
    full_audio_script отдельно от частей, возвращаем один кусок и монтаж откатывается на эвристику.
    """
    s = pkg.youtube_shorts_stream
    call = cta.voice_line(cta_mode)
    parts = [_scrub(p) for p in (s.hook_0_3s, s.problem_3_15s, s.solution_15_40s, s.cta_40_60s)]
    whole = _scrub(s.full_audio_script)
    if all(parts) and " ".join(parts) == whole:
        parts[3] = _drop_model_cta(parts[3]) or parts[3]
        return [*parts, call], True
    text = whole or " ".join(p for p in parts if p)
    return [f"{_drop_model_cta(text)} {call}".strip()], False


def clean_voice_script(pkg: ContentPackage, cta_mode: str | None = None) -> str:
    """Текст диктора целиком: чистим то, что модель могла оставить, и дописываем шаблонный призыв."""
    return " ".join(voice_segments(pkg, cta_mode)[0])


def credit_parts(post: db.Post) -> tuple[str, str] | None:
    """(сайт, ссылка) источника, если тема взята из ленты; для своих постов и пересланных текстов — None."""
    if not post or not post.credit or "\n" not in post.credit:
        return None
    host, url = post.credit.split("\n", 1)
    return host, url


def with_credit(tg_html: str, post: db.Post) -> str:
    """Дописывает в конец Telegram-поста строку «Источник: …», если её там ещё нет."""
    c = credit_parts(post)
    if not c:
        return tg_html
    href = html.escape(c[1], quote=True)    # в HTML-тексте адрес стоит экранированным: & → &amp;
    if href in tg_html or c[1] in tg_html:
        return tg_html
    return f'{tg_html}\n\nИсточник: <a href="{href}">{html.escape(c[0], quote=False)}</a>'


_RUBRICS = profiles.current().rubrics_map   # столп → рубрика над постом (profile.toml, [rubrics])


def with_rubric(tg_html: str, post: db.Post) -> str:
    """Первая строка поста — постоянная рубрика (столп бренда): подписчик узнаёт её с одного взгляда и знает, чего ждать.

    Столп определяем по началу материала тем же словарём, что и подбор тем (topics.pillar_of).
    """
    if not settings.tg_rubrics or not profiles.current().rubrics:
        return tg_html
    pillar = topics.pillar_of(topics.Candidate(title=post.source_text[:400], url="", summary="", source="", published=None))
    label = _RUBRICS.get(pillar)
    if not label or label in tg_html:
        return tg_html
    return f"<i>{label}</i>\n\n{tg_html}"


def quality_report(post: db.Post) -> list[quality.Issue]:
    """Автопроверка пакета: то, что слышит и видит зритель, а не сырой ответ модели."""
    segments, aligned = voice_segments(post.package, post.cta_mode)
    parts = segments[:-1] if aligned else segments   # шаблонный призыв проверять незачем
    return quality.check(post.source_text, post.package, parts, has_video=bool(post.video))


async def make_video(source_text: str, pkg: ContentPackage, workdir: Path,
                     progress: Progress = _noop, cta_mode: str | None = None) -> Path:
    """Озвучка + инфографика по сценам + стоковый фон → shorts_render.mp4."""
    shorts = pkg.youtube_shorts_stream
    name = "ElevenLabs" if settings.tts_provider == "elevenlabs" else "Yandex SpeechKit"
    await progress(f"🎙 Озвучка в {name} и сценарий инфографики…")
    segments, aligned = voice_segments(pkg, cta_mode)
    (audio, words), plan = await asyncio.gather(
        tts.synthesize(" ".join(segments), workdir),
        ai.scene_plan(source_text, pkg),
    )
    # сколько слов в каждой смысловой части — по ним монтаж находит границы сцен в таймкодах
    seg_words = [len(x.split()) for x in segments] if aligned else None
    if not settings.pixabay_api_key:
        log.warning("сток: PIXABAY_API_KEY не задан, фон будет без видео")
    clips = await stock.fetch_clips(plan.stock_queries, workdir) if settings.pixabay_api_key else []
    # всегда пишем итог: когда ролик вышел с чёрным фоном, по журналу должно быть видно почему
    log.info("сток: запросы %s → клипов %s", plan.stock_queries, len(clips))
    await progress(f"🎬 Рендер Shorts ({'сток + ' if clips else ''}инфографика + субтитры)…")
    title = shorts.youtube_title.split("#")[0].strip()
    # код из Telegram-поста точнее, но если его нет, на экране всё равно нужен пример: терминал держит внимание
    code = pkg.telegram_stream.code_snippet or plan.code
    code_lang = pkg.telegram_stream.code_language if pkg.telegram_stream.code_snippet else plan.code_language
    async with _render_lock:
        return await video.render(
            workdir, audio, words, title, code, plan, clips,
            code_lang=code_lang, cta_mode=cta_mode,
            segment_words=seg_words, takeaway=segments[3] if aligned else "",
        )


async def build_media(post_id: int, source_text: str, pkg: ContentPackage, progress: Progress = _noop) -> None:
    """Stream B: TTS ➔ ffmpeg. Ошибка здесь не блокирует публикацию в Telegram."""
    if not settings.tts_enabled:
        await db.update(post_id, video_path=None, error=f"Видео не сгенерировано: нет ключа для TTS_PROVIDER={settings.tts_provider}")
        return
    workdir = settings.media_dir / str(post_id)
    workdir.mkdir(parents=True, exist_ok=True)
    saved = await db.get(post_id)
    try:
        out = await make_video(source_text, pkg, workdir, progress,
                               cta_mode=saved.cta_mode if saved else None)
        await db.update(post_id, video_path=str(out), error=None)
    except (tts.TTSError, video.RenderError) as e:
        log.exception("media failed for post %s", post_id)
        await db.update(post_id, video_path=None, error=f"Видео не сгенерировано: {e}")


async def generate(post_id: int, extra_instructions: str = "", progress: Progress = _noop) -> db.Post:
    post = await db.get(post_id)
    await db.update(post_id, status=db.GENERATING, error=None)
    try:
        await progress("🧠 Нейросеть переупаковывает пост…")
        pkg = await ai.generate_package(post.source_text, extra_instructions)
        await db.update(post_id, package=pkg, tg_html=with_rubric(with_credit(build_post(pkg.telegram_stream), post), post))
        await build_media(post_id, post.source_text, pkg, progress)
        await db.update(post_id, status=db.REVIEW)
    except ai.AIError as e:
        await db.update(post_id, status=db.FAILED, error=str(e))
    except Exception as e:
        log.exception("generate failed for post %s", post_id)
        await db.update(post_id, status=db.FAILED, error=f"{type(e).__name__}: {e}")
    return await db.get(post_id)


async def ai_edit_telegram(post_id: int, instructions: str) -> db.Post:
    post = await db.get(post_id)
    pkg = post.package
    pkg.telegram_stream = await ai.edit_telegram(pkg.telegram_stream, instructions)
    await db.update(post_id, package=pkg, tg_html=with_rubric(with_credit(build_post(pkg.telegram_stream), post), post))
    return await db.get(post_id)


def channel_post_url(message_id: int) -> str | None:
    ch = settings.tg_channel_id
    if ch.startswith("@"):
        return f"https://t.me/{ch[1:]}/{message_id}"
    if ch.startswith("-100"):
        return f"https://t.me/c/{ch[4:]}/{message_id}"
    return None


async def publish_telegram(bot: Bot, post: db.Post) -> str:
    if post.tg_message_id:
        return f"Telegram: уже опубликован ({channel_post_url(post.tg_message_id) or post.tg_message_id})"
    if visible_length(post.tg_html) > TG_TEXT_LIMIT:
        raise ValueError(f"пост длиннее {TG_TEXT_LIMIT} символов — сократи через ✏️")
    msg = await bot.send_message(
        settings.tg_channel_id, post.tg_html, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True)
    )
    await db.update(post.id, tg_message_id=msg.message_id)
    return f"Telegram: ✅ {channel_post_url(msg.message_id) or msg.message_id}"


async def publish_youtube(post: db.Post) -> str:
    if post.youtube_video_id:
        return f"YouTube: уже загружен (https://youtube.com/shorts/{post.youtube_video_id})"
    if not post.video:
        raise ValueError("нет готового видео")
    s = post.package.youtube_shorts_stream
    description = s.youtube_description
    if settings.tg_channel_link not in description:
        description += f"\n\n{settings.tg_channel_link}"
    c = credit_parts(post)
    if c and c[1] not in description:
        description += f"\n\nИсточник: {c[1]}"   # в Shorts ссылка некликабельна, но авторство указано честно
    video_id = await youtube.upload_short(post.video, s.youtube_title, description, s.youtube_tags)
    await db.update(post.id, youtube_video_id=video_id)
    return (
        f"YouTube: ✅ https://youtube.com/shorts/{video_id}\n"
        f"📌 Вставь и закрепи комментарий (Studio → Комментарии):\n{s.first_comment}"
    )


async def publish(bot: Bot, post_id: int, to_tg: bool, to_yt: bool) -> list[str]:
    post = await db.get(post_id)
    await db.update(post_id, status=db.PUBLISHING)
    report, failed = [], False
    for enabled, name, fn in ((to_tg, "Telegram", lambda: publish_telegram(bot, post)), (to_yt, "YouTube", lambda: publish_youtube(post))):
        if not enabled:
            continue
        try:
            report.append(await fn())
        except Exception as e:
            log.exception("%s publish failed for post %s", name, post_id)
            report.append(f"{name}: ❌ {e}")
            failed = True
    await db.update(post_id, status=db.REVIEW if failed else db.PUBLISHED)
    return report
