"""JSON-схема ответа Claude (раздел 3, шаг 2 ТЗ)."""

from pydantic import BaseModel, Field


class TelegramStream(BaseModel):
    formatted_text: str = Field(description="Тело поста в Telegram-HTML: <b>, <i>, <code>, <pre>, <a>, <blockquote>. Без кода-артефакта, CTA и хештегов — они идут отдельными полями.")
    code_snippet: str = Field(description="Готовый код/конфиг/запрос (PromQL, Go, YAML, bash). Пустая строка, если кода нет.")
    code_language: str = Field(description="Язык code_snippet для подсветки: promql, go, yaml, bash, python… Пустая строка, если кода нет.")
    hashtags: list[str] = Field(description="3–6 хештегов, каждый начинается с #")
    cta_text: str = Field(description="Короткий призыв к действию в конце поста")


class ShortsStream(BaseModel):
    hook_0_3s: str
    problem_3_15s: str
    solution_15_40s: str
    cta_40_60s: str
    full_audio_script: str = Field(description="Чистый текст для диктора: hook+problem+solution+cta подряд, без скобок, ремарок, эмодзи, спецсимволов и кода; числа и аббревиатуры словами так, как их надо произнести.")
    youtube_title: str = Field(description="До 90 символов, включая #shorts")
    youtube_description: str
    youtube_tags: list[str] = Field(description="5–10 тегов без символа #")
    first_comment: str


# иконки схемы нарисованы в composition.html; модель выбирает из этого списка, чужие названия заменяются на box
FLOW_ICONS = ("server", "db", "user", "queue", "cloud", "code", "clock", "money", "shield", "bolt", "box")


class ScenePlan(BaseModel):
    """Тексты для анимированной инфографики Shorts — по одной карточке на часть ролика."""

    layout: str = Field(default="number", description="Раскладка хука: «claim» — пост спорит с общепринятым (на экране миф зачёркивается, появляется правда); «number» — в посте есть сильная цифра. Только одно из двух слов")
    myth: str = Field(default="", description="Для claim: что обычно думают, до 8 слов («Больше серверов — быстрее сервис»). Для number — пустая строка")
    truth: str = Field(default="", description="Для claim: как на самом деле, до 8 слов, это тезис поста («Лишний сервер роняет базу»). Для number — пустая строка")
    flow: list[str] = Field(default_factory=list, description="Цепочка причин и следствий для схемы, 3–4 узла по 1–3 слова: «30 подов», «600 соединений», «база падает». Последний узел — последствие. Пустой список, если цепочки в посте нет")
    flow_icons: list[str] = Field(default_factory=list, description="Иконка для каждого узла flow, в том же порядке, из списка: server, db, user, queue, cloud, code, clock, money, shield, bolt, box")
    hook_big: str = Field(description="Главная цифра или факт хука, 1–3 слова: «47 минут», «−35%», «$38 000»")
    hook_small: str = Field(description="Пояснение к цифре, до 6 слов: «простоя оплаты в 3 ночи»")
    problem_before: str = Field(description="Что пошло не так / как было, до 5 слов: «30 подов × 20 соединений»")
    problem_after: str = Field(description="Последствие / к чему привело, до 5 слов: «база упала целиком»")
    steps: list[str] = Field(description="2–3 шага решения, каждый 2–5 слов: «Найди узкое место»")
    code: str = Field(default="", description="3–7 строк настоящего кода, конфига или команды, показывающих суть решения (Dockerfile, YAML, bash, SQL, PromQL), строки до 38 символов. Пустая строка, если не уверен в корректности")
    code_language: str = Field(default="", description="Язык code: dockerfile, yaml, bash, sql, promql, go, python… Пустая строка, если кода нет")
    stock_queries: list[str] = Field(description="3 запроса на английском для стоковых видео по теме, 1–3 слова: «server room», «programmer night», «data center»")


class ContentPackage(BaseModel):
    telegram_stream: TelegramStream
    youtube_shorts_stream: ShortsStream
