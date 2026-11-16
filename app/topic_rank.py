"""Второй этап отбора тем: один запрос модели на несколько финалистов эвристики.

Эвристика (topics.py) видит слова, но не смысл: не отличает «пересказ релиза» от темы, где можно занять
позицию. Позиция с цифрой даёт охват в LinkedIn (см. CLAUDE.md), поэтому финалистов оценивает модель.
Запрос один на всю подборку, а не по записи: ответы YandexGPT идут минутами. Сбой модели не ломает
подбор тем: остаётся оценка эвристики.
"""

import asyncio
import logging

from pydantic import BaseModel

from .config import settings

log = logging.getLogger(__name__)

TIMEOUT = 240          # секунд; дольше ждать подборку тем нет смысла
MAX_BONUS = 4.0        # максимум, на сколько модель может сдвинуть оценку эвристики


class Verdict(BaseModel):
    n: int
    position: int      # 0..3: можно ли занять жёсткую позицию против общепринятого
    evidence: int      # 0..3: есть ли в описании конкретная цифра или факт
    retelling: int     # 0..3: насколько это пересказ релиза, анонса или общих советов


class Verdicts(BaseModel):
    items: list[Verdict]


_SYSTEM = (
    "Ты помогаешь инженеру-автору выбирать темы для постов. Его посты работают, когда в первой строке есть "
    "спор с общепринятым, дальше конкретная механика и цифры. Не работают пересказ новостей и релизов без позиции, "
    "общие советы и обзоры. Оцени каждую запись только по заголовку и описанию, ничего не додумывай. "
    "Для каждой записи верни n (её номер), position, evidence и retelling числами от 0 до 3."
)


def bonus(v: Verdict) -> float:
    """Поправка к оценке эвристики из ответа модели; значения вне 0..3 обрезаются."""
    clamp = lambda x: max(0, min(3, x))  # noqa: E731
    return round(min(MAX_BONUS, max(-MAX_BONUS, 1.5 * clamp(v.position) + 0.5 * clamp(v.evidence) - 1.5 * clamp(v.retelling) + 1.5)), 1)


async def rank(cands: list) -> dict[int, float]:
    """номер финалиста (индекс в cands) → поправка. Пусто, если модель выключена, не ответила или ошиблась."""
    if not cands or not settings.topics_llm:
        return {}
    from . import ai   # поздний импорт: ai тянет тяжёлые клиенты, а тесты topics работают без них
    listing = "\n\n".join(f"{i}. {c.title}\n{c.summary[:300]}" for i, c in enumerate(cands))
    try:
        res = await asyncio.wait_for(ai._generate(f"Записи:\n\n{listing}", Verdicts, system=_SYSTEM, max_tokens=800), TIMEOUT)
    except Exception as e:  # noqa: BLE001  любая ошибка модели не должна ронять подбор тем
        log.warning("оценка тем моделью не удалась, остаётся эвристика: %s", e)
        return {}
    return {v.n: bonus(v) for v in res.items if 0 <= v.n < len(cands)}
