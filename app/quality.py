"""Проверка готового пакета без участия модели: годится ли он к публикации без глаз автора.

Слабая модель иногда пишет неверное — выдумывает цифры и инструменты, оставляет латиницу,
которую синтезатор читает по буквам, раздувает или съедает сценарий. Здесь правила, которые
ловят такое детерминированно. Любое замечание = «нужна ручная проверка»: автопубликация
разрешена только при пустом списке (`auto_ok`). Пока автопубликации нет, отчёт показывается
в карточке модерации и подсказывает, куда смотреть.
"""

import re
from dataclasses import dataclass
from html import unescape

from . import ai, profiles, pronounce, tells
from .config import settings
from .schemas import ContentPackage
from .tg_format import TG_TEXT_LIMIT, visible_length

BLOCK, WARN = "block", "warn"

MIN_WORDS, MAX_WORDS = 50, 80      # ролик 30–40 секунд; вне этих границ он коротковат или затянут
HARD_ENGLISH = re.compile(r"\b(concurrently|explain analyze|gitops|kubectl|autoscal\w*|observability|"
                          r"throughput|backpressure|idempotenc\w*)\b", re.I)
GREETING = re.compile(r"^\s*(привет|здравствуй|друзья|сегодня (расскажу|поговорим)|давай(те)? разберёмся)", re.I)
_VY = re.compile(r"\b(вы|вас|вам|вами|ваш\w*|узнайте|подписывайтесь|читайте|смотрите|скачайте|проверьте|используйте|"
                 r"представьте|зацените)\b", re.I)
NUMBER = re.compile(r"\d+(?:[  ]\d{3})*(?:[.,]\d+)?")


@dataclass
class Issue:
    level: str
    text: str


def _numbers(text: str) -> set[str]:
    """Числа из текста без пробелов-разделителей и с точкой вместо запятой: «1 400» и «1,4» сравнимы."""
    out = set()
    for m in NUMBER.findall(text):
        n = m.strip().replace(" ", "").replace(" ", "").replace(",", ".")
        if n:
            out.add(n)
    return out


def _plain(html: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", html))


def _balanced(code: str) -> bool:
    """Скобки и кавычки в коде закрыты. Грубо, но обрезанный посередине пример ловит."""
    stack, pairs, quote = [], {")": "(", "]": "[", "}": "{"}, ""
    for ch in code:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            stack.append(ch)
        elif ch in pairs and (not stack or stack.pop() != pairs[ch]):
            return False
    return not stack and not quote


def check(source: str, pkg: ContentPackage, voice_parts: list[str], has_video: bool = True) -> list[Issue]:
    """voice_parts — речь по частям так, как уйдёт в озвучку (после чистки и без призыва)."""
    issues: list[Issue] = []
    add = lambda level, text: issues.append(Issue(level, text))  # noqa: E731
    yt, tg = pkg.youtube_shorts_stream, pkg.telegram_stream
    voice = " ".join(voice_parts)

    # --- сценарий ---
    words = len(voice.split())
    if words < MIN_WORDS:
        add(BLOCK, f"сценарий короткий: {words} слов (нужно {MIN_WORDS}+)")
    elif words > MAX_WORDS:
        add(BLOCK, f"сценарий длинный: {words} слов (не больше {MAX_WORDS})")
    for name, part in zip(("хук", "проблема", "решение", "главная мысль"), voice_parts):
        if len(part.split()) < 4:
            add(BLOCK, f"пустая или обрубленная часть: {name}")
    if voice_parts and GREETING.search(voice_parts[0]):
        add(WARN, "хук начинается с приветствия, а не с сути")
    if voice_parts and not re.search(r"\d|минут|секунд|час|раз\b|процент|тысяч|миллион|миллиард|доллар|рубл|гигабайт|гига|мегабайт|дн[яей]",
                                     voice_parts[0], re.I):
        add(WARN, "в хуке нет конкретики (числа, суммы, времени)")

    # --- выдуманное ---
    known = set(ai._invented_terms(source))
    fake = [n for n in ai._INVENTED if n in known
            and re.search(ai._INVENTED[n], voice + " " + _plain(tg.formatted_text), re.I)]
    if fake:
        add(BLOCK, "инструменты, которых нет в исходнике: " + ", ".join(fake))
    src_nums = _numbers(source)
    fresh = sorted(n for n in _numbers(_plain(tg.formatted_text) + " " + yt.youtube_title)
                   if n not in src_nums and not re.fullmatch(r"\d", n) and n not in ("2024", "2025", "2026"))
    if fresh:
        add(BLOCK, "цифры, которых нет в исходнике: " + ", ".join(fresh[:5]))

    # --- озвучка ---
    # латиница, которую словарь произношений не переписал (docker → докер — это нормально)
    latin = {w for w in re.findall(r"[A-Za-z][A-Za-z+#\-]{2,}", voice)
             if pronounce.apply(w).lower() == w.lower()}
    if latin:
        add(WARN if len(latin) <= 1 else BLOCK, "латиница в речи, синтезатор прочтёт криво: " + ", ".join(sorted(latin)[:5]))
    hard = {m.group(0).lower() for m in HARD_ENGLISH.finditer(voice)}
    if hard:
        add(BLOCK, "трудные английские слова в речи: " + ", ".join(sorted(hard)))
    if re.search(r"\d", voice):
        add(WARN, "цифры в речи: синтезатор может прочитать их неверно, нужны слова")
    if len(voice_parts) >= 4 and re.search(r"подпиш|телеграм|telegram|канал", voice_parts[3], re.I):
        add(WARN, "модель дописала свой призыв в главную мысль")

    # --- Telegram ---
    length = visible_length(tg.formatted_text)
    if length < 400:
        add(WARN, f"пост для Telegram короткий: {length} знаков")
    if length > TG_TEXT_LIMIT:
        add(BLOCK, f"пост длиннее лимита Telegram: {length} > {TG_TEXT_LIMIT}")
    if tg.code_snippet and not _balanced(tg.code_snippet):
        add(BLOCK, "в коде не закрыты скобки или кавычки: пример, видимо, обрезан")
    from . import topics   # поздний импорт: topics тянет сеть и базу, а quality нужен и без них
    market = bool(topics._PILLAR_MARKET and re.search(topics._PILLAR_MARKET, source[:400].lower()))   # про найм кода не бывает
    if (profiles.current().code_warning and not tg.code_snippet and not market
            and not re.search(r"<code>|<pre>", tg.formatted_text)):
        add(WARN, "в посте нет кода или команды: канал ценят за практику")

    # обращение должно быть на «ты» везде: пост, описание и первый комментарий
    texts = {"пост": _plain(tg.formatted_text + " " + tg.cta_text), "описание YouTube": yt.youtube_description,
             "первый комментарий": yt.first_comment}
    vy = [f"{name} («{m.group(0)}»)" for name, t in texts.items() if (m := _VY.search(t))]
    if vy:
        add(WARN, "обращение на «вы» вместо «ты»: " + "; ".join(vy))
    for name, t in (("пост", texts["пост"]), ("сценарий", voice)):
        for tell in tells.ai_tells(t):
            add(WARN, f"{name}: {tell}")

    # --- YouTube ---
    if len(yt.youtube_title) > 100:
        add(BLOCK, f"заголовок YouTube длиннее 100 знаков: {len(yt.youtube_title)}")
    if "#shorts" not in yt.youtube_title.lower() + yt.youtube_description.lower():
        add(WARN, "нет #shorts в заголовке или описании")
    if settings.tg_channel_link not in yt.youtube_description:
        add(WARN, "в описании YouTube нет ссылки на Telegram")
    if not has_video:
        add(BLOCK, "видео не собралось")
    return issues


def auto_ok(issues: list[Issue]) -> bool:
    return not issues


def summary(issues: list[Issue]) -> str:
    """Короткий блок для карточки модерации."""
    if not issues:
        return "🟢 Автопроверка: замечаний нет"
    head = "🔴 Автопроверка: нужна ручная проверка" if any(i.level == BLOCK for i in issues) \
        else "🟡 Автопроверка: есть сомнения"
    return head + "\n" + "\n".join(f"{'🔴' if i.level == BLOCK else '🟡'} {i.text}" for i in issues)
