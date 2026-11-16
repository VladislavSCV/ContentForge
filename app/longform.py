"""Пост для LinkedIn и статья для Хабра: те же материалы, другие форматы.

LinkedIn: формула поста; образцы стиля — до двух .txt в assets/linkedin_examples (положи свои удачные посты, без них образцов в промпте нет).
Хабр: раз в месяц из опубликованных постов собирается одна статья по плану, раздел за разделом
(слабая модель не выдерживает длинный текст за один запрос).
"""

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

from pydantic import BaseModel, Field

from . import ai, db, quality, tells
from .config import settings
from .prompts import HABR_PLAN_PROMPT, HABR_SECTION_PROMPT, LINKEDIN_PROMPT, LINKEDIN_PROMPT_EN

log = logging.getLogger(__name__)

EXAMPLES_DIR = Path("assets/linkedin_examples")
LI_MIN, LI_MAX = 1000, 2800      # лимит LinkedIn — 3000 знаков; ниже тысячи пост не набирает глубины

Progress = Callable[[str], Awaitable[None]]


class LinkedInPost(BaseModel):
    text: str = Field(description="Текст поста по формуле: тезис-спор, как думают все, 3 пункта, правила с порогами, вывод, вопрос коллегам. 1300–2400 знаков")
    first_comment: str = Field(description="Первый комментарий: что лежит в Telegram-канале и ссылка на него. 1–2 фразы")


class HabrPlan(BaseModel):
    title: str = Field(description="Заголовок статьи, конкретный, без кликбейта")
    lead: str = Field(description="Лид на 2–3 предложения: проблема и что получит читатель")
    headings: list[str] = Field(description="4–5 заголовков разделов")
    sources: list[str] = Field(description="Для каждого раздела в том же порядке: номера постов через запятую, например «1, 3»")
    tags: list[str] = Field(description="5 ключевых слов для тегов статьи на Хабре, строчными")
    hubs: list[str] = Field(description="2–3 подходящих хаба Хабра, например «DevOps», «Kubernetes», «Серверное администрирование»")


class HabrSection(BaseModel):
    body: str = Field(description="Текст раздела в Markdown, 1200–2000 знаков")


def _examples() -> str:
    parts = [f"<образец>\n{p.read_text(encoding='utf-8').strip()[:2200]}\n</образец>"
             for p in sorted(EXAMPLES_DIR.glob("*.txt"))[:2]]
    return "\n\n".join(parts)


# служебные метки, которые слабая модель переносит из промпта в текст («Closing line: …», «Question: …»)
_LABELS = re.compile(r"^(?:closing line|question|hook|thesis|first line|вывод|вопрос)\s*:\s*", re.I | re.M)
# замечания, которые имеет смысл исправлять повторным запросом (остальные автор видит в карточке)
_FIXABLE = re.compile(r"длина|процент|цифры, которых нет|кириллица|штамп|stock|ссылка в тексте")


def check_linkedin(source: str, li: LinkedInPost) -> list[str]:
    """Замечания к посту: длина, формула, выдуманные цифры и инструменты."""
    en = settings.linkedin_lang == "en"
    issues, text = [], li.text.strip()
    if not LI_MIN <= len(text) <= LI_MAX:
        issues.append(f"длина {len(text)} знаков (нужно {LI_MIN}–{LI_MAX})")
    first = text.split("\n", 1)[0]
    # LinkedIn обрезает пост «ещё» примерно после двух строк: тезис обязан поместиться в первую
    if len(first) > (130 if en else 160) or first.rstrip().endswith("?"):
        issues.append("первая строка не тезис: слишком длинная или вопрос")
    if not text.rstrip().endswith("?"):
        issues.append("нет вопроса коллегам в конце")
    if re.search(r"https?://", text):
        issues.append("ссылка в тексте: её место в первом комментарии")
    if en and re.search(r"[А-Яа-яЁё]{3,}", text):
        issues.append("в английском посте осталась кириллица")
    issues += tells.ai_tells_en(text) if en else tells.ai_tells(text)
    src_nums = quality._numbers(source)
    fresh = sorted(n for n in quality._numbers(text) if n not in src_nums and not re.fullmatch(r"\d{1,3}", n))
    if fresh:
        issues.append("цифры, которых нет в исходнике: " + ", ".join(fresh[:5]))
    # короткие числа допустимы как пороги в правилах, но процент «из воздуха» — выдумка: «на 30%» модель дописывает сама
    src_pct = set(re.findall(r"(\d+(?:[.,]\d+)?)\s?(?:%|процент|percent)", source, re.I))
    pct = sorted({m for m in re.findall(r"(\d+(?:[.,]\d+)?)\s?(?:%|процент|percent)", text, re.I) if m not in src_pct})
    if pct:
        issues.append("процент, которого нет в исходнике: " + ", ".join(p + "%" for p in pct[:3]))
    fake = [n for n in ai._invented_terms(source) if re.search(ai._INVENTED[n], text, re.I)]
    if fake:
        issues.append("инструменты, которых нет в исходнике: " + ", ".join(fake))
    return issues


async def write_linkedin(source_text: str, extra: str = "", opinion: str = "") -> tuple[LinkedInPost, list[str]]:
    if settings.linkedin_lang == "en":
        # образцы лежат на русском и тянули бы текст в русский: для английского формула целиком в промпте
        system = LINKEDIN_PROMPT_EN
        content = f"Source material:\n<source>\n{source_text[:6000]}\n</source>"
        if opinion:
            content += f"\n\nAuthor position (build the thesis on it; the source is only material):\n{opinion}"
        if extra:
            content += f"\n\nAuthor requests:\n{extra}"
    else:
        system = f"{LINKEDIN_PROMPT}\n\n{_examples()}"
        content = f"Исходный материал:\n<source>\n{source_text[:6000]}\n</source>"
        if opinion:
            content += f"\n\nПозиция автора (тезис поста строй на ней, исходник служит материалом):\n{opinion}"
        if extra:
            content += f"\n\nПожелания автора:\n{extra}"
    li, issues = None, []
    for attempt in range(2):
        ask = content if not issues else (
            f"{content}\n\nThe previous draft had these problems, fix them and keep everything else:\n- " + "\n- ".join(issues)
            if settings.linkedin_lang == "en" else
            f"{content}\n\nВ прошлом черновике нашли замечания, исправь их, остальное сохрани:\n- " + "\n- ".join(issues))
        li = await ai._generate(ask, LinkedInPost, system=system, max_tokens=1600)
        li.text = _LABELS.sub(lambda m: "\0", li.text.strip())
        li.text = re.sub(r"\0(\s*)(\w)", lambda m: m.group(2).upper(), li.text).replace("\0", "")
        issues = [i for i in check_linkedin(source_text, li) if _FIXABLE.search(i)]
        if not issues:
            break
    if settings.tg_channel_link not in li.first_comment:
        li.first_comment = f"{li.first_comment.strip()} {settings.tg_channel_link}".strip()
    return li, check_linkedin(source_text, li)


# ---------- Хабр ----------

def _plain(html: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", unescape(re.sub(r"<[^>]+>", "", html or ""))).strip()


async def month_posts(days: int = 30) -> list[db.Post]:
    border = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    return [p for p in await db.latest(200) if p.status == db.PUBLISHED and p.package and p.updated_at >= border]


def digest(posts: list[db.Post]) -> str:
    out = []
    for i, p in enumerate(posts, 1):
        tg = p.package.telegram_stream
        code = f"\nКод ({tg.code_language}):\n{tg.code_snippet}" if tg.code_snippet else ""
        out.append(f"[{i}] {_plain(tg.formatted_text)[:1500]}{code}")
    return "\n\n".join(out)


async def _skip(_: str) -> None:
    pass


async def write_habr(posts: list[db.Post], progress: Progress = _skip) -> tuple[str, HabrPlan]:
    """Статья в Markdown и её план (заголовок, теги, хабы) из опубликованных постов месяца (не меньше двух)."""
    return await _habr_article(digest(posts), "Посты автора:\n", progress)


async def write_habr_from_material(material: str, opinion: str = "", progress: Progress = _skip) -> tuple[str, HabrPlan]:
    """Статья по одному материалу автора (текст, заметки, разбор инцидента), а не по постам бота.

    Материал подаётся как пост [1]: планировщик и авторы разделов работают с ним теми же промптами.
    """
    src = f"[1] {material.strip()[:9000]}"
    intro = ("ЗАДАЧА: статья строится вокруг ОДНОГО материала автора ниже (он под номером [1], в sources пиши «1»), "
             "это не подборка постов. Опирайся только на его факты.\n")
    if opinion:
        intro += f"ПОЗИЦИЯ АВТОРА (тезис статьи строй на ней): {opinion}\n"
    return await _habr_article(src, intro + "Материал автора:\n", progress)


async def _habr_article(src: str, intro: str, progress: Progress) -> tuple[str, HabrPlan]:
    await progress("🧭 Составляю план статьи…")
    plan = await ai._generate(f"{intro}{src}", HabrPlan, system=HABR_PLAN_PROMPT, max_tokens=900)
    heads = [h for h in plan.headings if h.strip()][:5]
    if len(heads) < 3:
        raise ai.AIError("модель дала слишком короткий план статьи")
    md = [f"# {plan.title.strip()}", "", plan.lead.strip(), ""]
    for i, head in enumerate(heads):
        await progress(f"✍️ Раздел {i + 1} из {len(heads)}: {head}")
        refs = plan.sources[i] if i < len(plan.sources) else ""
        ask = (f"План статьи: {plan.title}\nЛид: {plan.lead}\nВсе разделы: {'; '.join(heads)}\n\n"
               f"Пиши раздел «{head}». Опирайся на посты: {refs or 'любые подходящие'}.\n\nПосты автора:\n{src}")
        sec = await ai._generate(ask, HabrSection, system=HABR_SECTION_PROMPT, max_tokens=1600)
        md += [f"## {head.strip()}", "", sec.body.strip(), ""]
    md += ["---", f"Больше разборов и конфигов — в Telegram: {settings.tg_channel_link}"]
    return "\n".join(md), plan


# ---------- dev.to: английская версия статьи с Хабра ----------

DEVTO_PROMPT = """You are an editor translating a Russian technical article into natural English for dev.to. Write it the way an engineer would write it originally, not word for word.
Keep Markdown, code blocks, tool names and every number exactly as in the source. Add no facts, figures or tools that the source does not contain.
Plain, direct sentences. Avoid stock phrases ("let's dive in", "game-changer", "in today's fast-paced world", "it's worth noting"). The 'is not X, it is Y' move at most once.
Return the result strictly following the JSON schema."""


class DevtoMeta(BaseModel):
    title: str = Field(description="English article title: a concrete claim with a term or number, no clickbait")
    tags: list[str] = Field(description="Up to 4 dev.to tags, lowercase, letters and digits only, e.g. kubernetes, devops, sre, career")


class DevtoPart(BaseModel):
    text: str = Field(description="The translated Markdown text")


def split_article(md: str) -> tuple[str, str, list[tuple[str, str]], str]:
    """(заголовок, лид, [(заголовок раздела, текст)], хвост со ссылкой). Хвост — всё после последней «---»."""
    body, _, tail = md.rpartition("\n---\n")
    if not body:
        body, tail = md, ""
    chunks = re.split(r"\n## ", "\n" + body.strip(), flags=re.M)
    head = chunks[0].strip().split("\n", 1)
    title = head[0].lstrip("# ").strip()
    lead = head[1].strip() if len(head) > 1 else ""
    sections = []
    for ch in chunks[1:]:
        h, _, text = ch.partition("\n")
        sections.append((h.strip(), text.strip()))
    return title, lead, sections, tail.strip()


def clean_tags(tags: list[str]) -> list[str]:
    out = []
    for t in tags:
        t = re.sub(r"[^a-z0-9]", "", t.lower())
        if t and t not in out:
            out.append(t)
    return out[:4]


async def write_devto(md: str, progress: Progress = _skip) -> tuple[str, DevtoMeta]:
    """Английская версия статьи с Хабра: метаданные одним запросом, текст по разделам (слабая модель длинное не держит)."""
    title, lead, sections, _tail = split_article(md)
    if len(sections) < 2:
        raise ai.AIError("в статье не нашёл разделов для перевода")
    await progress("🧭 Заголовок и теги…")
    meta = await ai._generate(f"Title: {title}\nLead: {lead}\nSections: {'; '.join(h for h, _ in sections)}",
                              DevtoMeta, system=DEVTO_PROMPT, max_tokens=300)
    meta.tags = clean_tags(meta.tags) or ["devops"]
    lead_en = (await ai._generate(lead, DevtoPart, system=DEVTO_PROMPT, max_tokens=500)).text.strip()
    out = ["---", f"title: {meta.title.strip()}", "published: false", f"tags: {', '.join(meta.tags)}",
           "canonical_url: ", "---", "", lead_en, ""]
    for i, (head, text) in enumerate(sections):
        await progress(f"✍️ Раздел {i + 1} из {len(sections)}")
        part = await ai._generate(f"Translate this article section.\nHeading: {head}\n\n{text}", DevtoSection, system=DEVTO_PROMPT, max_tokens=1800)
        out += [f"## {part.heading.strip() or head}", "", part.text.strip(), ""]
    out += ["---", f"More write-ups (in Russian): {settings.tg_channel_link}"]
    return "\n".join(out), meta


class DevtoSection(BaseModel):
    heading: str = Field(description="Translated section heading")
    text: str = Field(description="Translated section body in Markdown")
