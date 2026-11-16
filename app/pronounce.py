"""Словарь произношений: правим, как синтезатор читает слова, не трогая субтитры.

Файл assets/pronunciation.txt, одна строка — одно слово:

    замок = зам+ок            # ударение: знак + перед ударной гласной (SpeechKit его понимает)
    кубернетес = куберн+етес
    кластер* = кл+астер*      # со звёздочкой: любое окончание (кластеры, кластеров…)
    постгрес = постгр+эс      # можно менять и написание, если так звучит лучше

Подстановка идёт только в тексте, который уходит на синтез: слова в таймкодах и субтитрах
остаются исходными. Поэтому «замок» на экране остаётся «замок», а голос читает правильно.
Слово не найдено — текст не меняется, так что ошибка в словаре портит только это слово.
"""

import re
from functools import lru_cache
from pathlib import Path

from .config import settings


def _parse(text: str) -> list[tuple[re.Pattern, str]]:
    rules: list[tuple[re.Pattern, str]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        src, dst = (x.strip() for x in line.split("=", 1))
        if not src or not dst:
            continue
        if src.endswith("*"):
            # основа + любое окончание; окончание переносим в результат как есть
            pattern = re.compile(rf"(?<!\w){re.escape(src[:-1])}(\w*)", re.I)
            rules.append((pattern, dst.rstrip("*") + r"\1"))
        else:
            rules.append((re.compile(rf"(?<!\w){re.escape(src)}(?!\w)", re.I), dst))
    return rules


@lru_cache(maxsize=1)
def _rules(path: str, mtime: float) -> list[tuple[re.Pattern, str]]:
    return _parse(Path(path).read_text(encoding="utf-8"))


def apply(text: str, path: Path | None = None) -> str:
    """Подставляет произношения из словаря. Нет файла — возвращает текст как есть."""
    p = path or settings.pronunciation_file
    if not p.is_file():
        return text
    for pattern, repl in _rules(str(p), p.stat().st_mtime):
        text = pattern.sub(lambda m, r=repl: m.expand(r), text)
    return text
