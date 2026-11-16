"""Напоминания автору: бот сам приходит и говорит, что сделать руками.

Бот ведёт всё, что можно автоматизировать, но три вещи делает только человек: публикует в LinkedIn, отвечает на Reddit
и присылает выгрузку LinkedIn. Без напоминаний они забываются первыми, а личный бренд держится на регулярности
Расписание — таблица RULES ниже: правь время и тексты здесь. Отключить всё: REMINDERS=false.

Правило срабатывает один раз в сутки в своё окно (WINDOW_H часов после времени). Если бот был выключен дольше окна,
напоминание пропускается, а не приходит среди ночи или запоздавшим. Условие cond возвращает текст или None
(None — напоминать не о чем, молчим).
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import db, features

log = logging.getLogger(__name__)

WINDOW_H = 3
REVIEW_STALE_H = 6

Cond = Callable[[], Awaitable[str | None]]


@dataclass
class Rule:
    key: str
    at: str                          # «HH:MM» местного времени
    text: str
    weekday: int | None = None       # 0 — понедельник … 6 — воскресенье
    monthday: int | None = None
    cond: Cond | None = None         # динамический текст или None: не о чем напоминать


def _age_h(iso: str) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 3600


async def _review_waiting() -> str | None:
    n = sum(1 for p in await db.latest(100) if p.status == db.REVIEW and p.updated_at and _age_h(p.updated_at) >= REVIEW_STALE_H)
    return f"🔔 Карточек ждёт твоей проверки: {n}. /list — открой и поставь в очередь или поправь." if n else None


async def _linkedin_missing() -> str | None:
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    n = sum(1 for p in await db.latest(100)
            if p.status == db.PUBLISHED and p.updated_at >= week_ago and not p.linkedin_json)
    return (f"💼 На этой неделе вышло постов без версии для LinkedIn: {n}. Открой карточку (/list) и нажми «Пост для LinkedIn», "
            "перечитай позицию и опубликуй руками. LinkedIn даёт тебе офферы, Shorts этого не делают.") if n else None


async def _export_stale() -> str | None:
    raw = await db.get_state("li_export_at")
    if raw and _age_h(raw) < 6 * 24:
        return None
    return ("📥 Пришли выгрузку LinkedIn: Analytics → Post performance → Export (.xlsx), просто файлом сюда. "
            "Через полчаса итоги недели: без цифр бот не узнает, что у тебя заходит.")


async def _habr_ready() -> str | None:
    return ("📰 Статья для Хабра собрана в начале месяца. Перечитай факты, опубликуй, потом /devto: "
            "английская версия для dev.to выходит после Хабра, canonical на него.") if await db.get_state("habr_last") else None


RULES = [
    Rule("plan", "10:00", "🗓 Неделя началась. 1) /topics: возьми по одной теме на столп (инфраструктура, AI, рынок и найм). "
         "2) Когда бот спросит позицию, ответь одной фразой, это главное, что делает пост твоим.", weekday=0),
    Rule("review", "12:00", "", cond=_review_waiting),
    Rule("reddit", "19:30", "🧵 15 минут на Reddit: /reddit, ответь по существу на 1–2 обсуждения. Без ссылок на себя: "
         "узнаваемость там растёт от пользы.", weekday=2),
    Rule("linkedin", "17:00", "", weekday=4, cond=_linkedin_missing),
    Rule("export", "17:30", "", weekday=6, cond=_export_stale),
    Rule("habr", "10:30", "", monthday=2, cond=_habr_ready),
    Rule("talk", "11:00", "🎤 Внутренний доклад на работе: выбери тему и дату на этот месяц. Запись разберём на посты и ролики, "
         "и на работе тебя начнут узнавать раньше, чем на митапах.", monthday=15),
]


# правило → функция из FEATURES; правила вне словаря (plan, review) работают всегда
_RULE_FEATURE = {"reddit": "reddit", "linkedin": "linkedin", "export": "week", "habr": "habr", "talk": "talk"}


def is_due(rule: Rule, local: datetime, last_sent: str | None) -> bool:
    """Подходит ли правило по календарю и окну, и не слали ли сегодня."""
    if rule.weekday is not None and local.weekday() != rule.weekday:
        return False
    if rule.monthday is not None and local.day != rule.monthday:
        return False
    h, m = map(int, rule.at.split(":"))
    start = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if not start <= local < start + timedelta(hours=WINDOW_H):
        return False
    return last_sent != local.strftime("%Y-%m-%d")


async def due_messages(local: datetime, rules: list[Rule] | None = None) -> list[tuple[str, str]]:
    """[(ключ правила, текст)] для всего, что пора отправить сейчас. Отметку «сегодня слали» ставит вызывающий."""
    out = []
    for rule in rules or RULES:
        if rule.key in _RULE_FEATURE and not features.enabled(_RULE_FEATURE[rule.key]):
            continue
        if not is_due(rule, local, await db.get_state(f"remind:{rule.key}")):
            continue
        text = (await rule.cond()) if rule.cond else rule.text
        out.append((rule.key, text or ""))
    return out
