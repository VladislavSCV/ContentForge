"""AI Engine: LinkedIn-пост ➔ ContentPackage (TG-пост + сценарий Shorts).

Провайдер выбирается через LLM_PROVIDER:
  yandex    — YandexGPT (Yandex AI Studio), JSON по схеме через jsonSchema
  openai    — любой OpenAI-совместимый API (DeepSeek, прокси) через LLM_BASE_URL
  anthropic — Claude, structured outputs
Ответ всегда валидируется Pydantic-схемой; если модель ошиблась — повторяем запрос,
показав ей ошибку (слабые модели так чинят JSON с 1–2 попытки).
"""

import contextvars
import copy
import json
import logging
import re
from typing import TypeVar

import httpx
from pydantic import BaseModel, Field, ValidationError

from . import profiles
from .config import settings
from .prompts import SCRIPT_PROMPT, SCRIPT_REVIEW_PROMPT, STORY_SCRIPT_PROMPT, SYSTEM_PROMPT, TG_EDIT_PROMPT, TG_REVIEW_PROMPT
from .schemas import FLOW_ICONS, ContentPackage, ScenePlan, TelegramStream
from .tg_format import build_post, visible_length

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
MAX_ATTEMPTS = 3


# Потолок токенов для коротких структурных ответов. Без него слабая модель иногда уходит в бесконечные
# переводы строк и генерирует до лимита 4000 токенов — это 5–6 минут впустую на одном запросе.
_token_cap: contextvars.ContextVar[int | None] = contextvars.ContextVar("token_cap", default=None)


class AIError(RuntimeError):
    pass


def yandex_hint(status: int) -> str:
    """Что чаще всего стоит за кодом ответа Яндекса: 403 выглядит как «нет прав», а причина часто в деньгах."""
    hints = {
        401: "ключ неверный или отозван: проверь YANDEX_API_KEY",
        402: "не хватает денег на платёжном аккаунте",
        403: "закончились деньги на платёжном аккаунте (billing.yandex.cloud) или у ключа нет роли на папку",
        429: "превышен лимит запросов, повтори позже",
    }
    return f"\n💡 Вероятная причина: {hints[status]}" if status in hints else ""


def _inline_schema(model: type[BaseModel]) -> dict:
    """JSON Schema без $ref/$defs — не все провайдеры их понимают."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return resolve(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            out = {k: resolve(v) for k, v in node.items() if k != "title"}
            # YandexGPT отвергает схему, где есть необязательные поля: «all fields must be required»
            if isinstance(out.get("properties"), dict):
                out["required"] = list(out["properties"])
            return out
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    return resolve(schema)


def _extract_json(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    if start == -1:
        return text
    # YandexGPT иногда дописывает после JSON мусор (хвост из ``` и «{») — берём только первый цельный объект
    try:
        _, end = json.JSONDecoder(strict=False).raw_decode(text[start:])
        return text[start : start + end]
    except ValueError:
        end = text.rfind("}")
        return text[start : end + 1] if end > start else text


# ---------- провайдеры: (system, messages, schema) -> сырой текст ответа ----------

async def _yandex(system: str, messages: list[dict], schema: dict) -> str:
    model = settings.llm_model
    model_uri = model if model.startswith("gpt://") else f"gpt://{settings.yandex_folder_id}/{model}"
    body = {
        "modelUri": model_uri,
        "completionOptions": {"stream": False, "temperature": 0.4, "maxTokens": str(min(settings.llm_max_tokens, _token_cap.get() or settings.llm_max_tokens))},
        "jsonSchema": {"schema": schema},
        "messages": [{"role": "system", "text": system}] + [{"role": m["role"], "text": m["content"]} for m in messages],
    }
    # IPv4: у Яндекса есть AAAA-записи, а в сетях без IPv6 соединение иначе падает
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=2)
    async with httpx.AsyncClient(timeout=180, transport=transport) as client:
        r = await client.post(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
            headers={"Authorization": f"Api-Key {settings.yandex_api_key}", "x-folder-id": settings.yandex_folder_id},
            json=body,
        )
    if r.status_code != 200:
        raise AIError(f"YandexGPT {r.status_code}: {r.text[:300]}{yandex_hint(r.status_code)}")
    data = r.json()["result"]
    log.info("yandex usage: %s", data.get("usage"))
    alt = data["alternatives"][0]
    if alt.get("status") == "ALTERNATIVE_STATUS_CONTENT_FILTER":
        raise AIError("YandexGPT отказался отвечать (сработал контент-фильтр)")
    return alt["message"]["text"]


async def _openai(system: str, messages: list[dict], schema: dict) -> str:
    base = (settings.llm_base_url or "https://api.openai.com/v1").rstrip("/")
    sys_text = f"{system}\n\nJSON Schema ответа:\n{json.dumps(schema, ensure_ascii=False)}"
    body = {
        "model": settings.llm_model,
        "messages": [{"role": "system", "content": sys_text}, *messages],
        "response_format": {"type": "json_object"},
        "max_tokens": min(settings.llm_max_tokens, _token_cap.get() or settings.llm_max_tokens),
    }
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {settings.llm_api_key}"}, json=body)
    if r.status_code != 200:
        raise AIError(f"LLM {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]["content"]


async def _anthropic(system: str, messages: list[dict], schema: dict) -> str:
    import anthropic  # опциональная зависимость

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=3)
    try:
        response = await client.messages.create(
            model=settings.llm_model,
            max_tokens=16000,
            system=system,
            messages=messages,
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": {**schema, "additionalProperties": False}}},
            # при отказе классификатора API сам перезапустит запрос на рекомендованной модели
            extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
            extra_body={"fallbacks": "default"},
        )
    except anthropic.APIStatusError as e:
        raise AIError(f"Claude API {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise AIError("Claude: нет соединения с API") from e
    if response.stop_reason == "refusal":
        raise AIError("Claude отказался обрабатывать этот текст")
    return "".join(b.text for b in response.content if b.type == "text")


_PROVIDERS = {"yandex": _yandex, "openai": _openai, "deepseek": _openai, "anthropic": _anthropic}


async def _generate(user_content: str, output_format: type[T], system: str = SYSTEM_PROMPT,
                    max_tokens: int | None = None) -> T:
    call = _PROVIDERS.get(settings.llm_provider)
    if not call:
        raise AIError(f"Неизвестный LLM_PROVIDER={settings.llm_provider}")
    schema = _inline_schema(output_format)
    messages = [{"role": "user", "content": user_content}]
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            token = _token_cap.set(max_tokens)
            try:
                raw = await call(system, messages, schema)
            finally:
                _token_cap.reset(token)
        except httpx.HTTPError as e:
            last_error = f"сеть: {e}"
            continue
        try:
            return output_format.model_validate_json(_extract_json(raw))
        except ValidationError as e:
            last_error = str(e)[:1500]
            log.warning("attempt %d: invalid JSON from %s: %s", attempt, settings.llm_provider, last_error)
            messages = [
                *messages[:1],
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"Ответ не прошёл проверку схемы:\n{last_error}\n\nВерни исправленный JSON целиком, строго по схеме, без пояснений."},
            ]
    raise AIError(f"Модель {MAX_ATTEMPTS} раза вернула невалидный ответ: {last_error[:300]}")


_EMPTY = {"", "null", "none", "nil", "-", "n/a"}


def _normalize(pkg: ContentPackage) -> ContentPackage:
    tg, yt = pkg.telegram_stream, pkg.youtube_shorts_stream
    if tg.code_snippet.strip().lower() in _EMPTY:
        tg.code_snippet = ""
    if tg.code_language.strip().lower() in _EMPTY or not tg.code_snippet:
        tg.code_language = ""
    yt.youtube_tags = [t.lstrip("#").strip() for t in yt.youtube_tags if t.strip("# ").lower() not in ("shorts", "")]

    # модель часто дублирует CTA, код и хештеги внутри текста — они и так добавляются отдельными блоками
    text = "\n".join(line.rstrip() for line in tg.formatted_text.strip().splitlines())
    # Markdown вместо HTML: **жирный**, `код`
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!`)`([^`\n]+)`(?!`)", r"<code>\1</code>", text)
    # приветствие вместо заголовка — выкидываем первую строку, если за ней есть текст
    first_line, _, rest_text = text.partition("\n")
    if re.match(r"^(<b>)?\s*(привет|здравствуй|добрый|hello|hi)\b", first_line, re.I) and rest_text.strip():
        text = rest_text.strip()
    cta = tg.cta_text.strip()
    if len(cta) > 15 and cta in text:
        text = text.replace(cta, "")
    code = tg.code_snippet.strip()
    if len(code) > 15:
        text = re.sub(r"<pre>(<code[^>]*>)?\s*" + re.escape(code) + r"\s*(</code>)?</pre>", "", text)
    lines = text.rstrip().splitlines()
    while lines and (not lines[-1].strip() or re.fullmatch(r"(\s*#[\w\-]+)+\s*", lines[-1])):
        lines.pop()
    if lines:
        lines[-1] = re.sub(r"(\s+#[\w\-]+)+\s*$", "", lines[-1])
    text = "\n".join(lines).strip()
    # заголовок без <b> — делаем жирной первую строку
    first, _, rest = text.partition("\n")
    if not re.match(r"\s*<b>\s*\S", first):
        text = f"<b>{re.sub(r'</?b>', '', first).strip()}</b>\n{rest}"
    tg.formatted_text = re.sub(r"\n{3,}", "\n\n", text)
    link = settings.tg_channel_link
    if link not in yt.first_comment:
        yt.first_comment = f"{yt.first_comment.strip()}\n{link}".strip()
    if link not in yt.youtube_description:
        yt.youtube_description = f"{yt.youtube_description.strip()}\n\n{link}".strip()
    return pkg


class _VoiceParts(BaseModel):
    """Слабой модели проще заполнить 4 коротких поля нужной длины, чем выдержать общий объём текста."""

    hook: str = Field(description="Хук: конкретика из поста (число, сумма, время, неожиданный факт), 10–14 слов, без приветствия")
    problem: str = Field(description="Проблема коротко и конкретно, одна бытовая аналогия, 16–22 слова")
    solution: str = Field(description="Решение: 2–3 действия простыми словами, 26–34 слова")
    cta: str = Field(description="Запоминающаяся главная мысль одной фразой, 8–12 слов. Без призыва подписаться и без слова «телеграм»: призыв добавит бот")


# к этому тексту бот допишет шаблонный призыв (~13 слов), поэтому цель ниже итоговых 70–90
# Темп речи 2,0–2,3 слова/с (медленнее на английских терминах и числах). С призывом (~13 слов) это 31–40 с.
MIN_VOICE_WORDS, MAX_VOICE_WORDS = 50, 74
TG_POST_MAX = 3200  # видимых символов; лимит Telegram — 4096, оставляем запас на правки


def _voice_score(text: str) -> int:
    """Насколько текст далёк от нужной длины (0 — в диапазоне)."""
    n = len(text.split())
    return max(MIN_VOICE_WORDS - n, n - MAX_VOICE_WORDS, 0)


async def _fit_voice(source_text: str, pkg: ContentPackage) -> None:
    """Слабые модели пишут сценарий то на 15 секунд, то на 2 минуты — переписываем только его по частям."""
    yt = pkg.youtube_shorts_stream
    best = yt.full_audio_script
    best_parts = [yt.hook_0_3s, yt.problem_3_15s, yt.solution_15_40s, yt.cta_40_60s]
    for _ in range(3):
        # недобор до шести слов — это секунда речи; каждый запрос к Yandex идёт минуты, а слабая модель
        # при «допиши» раздувает текст вдвое, поэтому подгоняем только заметные промахи
        if _voice_score(best) <= 6:
            break
        words = len(best.split())
        task = "Текущий текст слишком короткий — раскрой каждую часть подробнее." if words < MIN_VOICE_WORDS else "Текущий текст слишком длинный — оставь только главное."
        prompt = (
            f"Исходный пост:\n<post>\n{source_text[:4000]}\n</post>\n\nТекущий текст диктора для Shorts ({words} слов):\n"
            f"<script>\n{best}\n</script>\n\n"
            f"Перепиши текст диктора, разбив его на 4 части строго указанной длины (всего 60–72 слова; призыв добавит бот). {task} "
            "Говори на «ты», короткими живыми фразами, как будто объясняешь другу. "
            "Только чистый текст для озвучки: без эмодзи, скобок, ссылок и кода."
        )
        try:
            parts = await _generate(prompt, _VoiceParts, max_tokens=700)
        except AIError as e:
            log.warning("voice fit failed: %s", e)
            break
        candidate = _parts_text(parts)
        log.info("voice fit: %d -> %d words", words, len(candidate.split()))
        if _voice_score(candidate) < _voice_score(best):
            best = candidate
            best_parts = [_sentence(p) for p in (parts.hook, parts.problem, parts.solution, parts.cta)]
    yt.full_audio_script = best
    yt.hook_0_3s, yt.problem_3_15s, yt.solution_15_40s, yt.cta_40_60s = best_parts
    words = yt.full_audio_script.split()
    if len(words) > MAX_VOICE_WORDS + 5:  # модель так и не сократила — режем по последнему предложению в лимите
        cut = " ".join(words[:MAX_VOICE_WORDS])
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        yt.full_audio_script = cut[: end + 1] if end > 0 else cut


def _sentence(text: str) -> str:
    """Часть без точки в конце склеивается со следующей в одно «предложение»: синтезатор читает без пауз,
    а монтаж не находит границ фраз. Поэтому каждую часть закрываем знаком препинания."""
    text = text.strip()
    return text if not text or re.search(r"[.!?…]$", text) else text.rstrip(",;:—- ") + "."


def _parts_text(p: "_VoiceParts") -> str:
    return " ".join(_sentence(x) for x in (p.hook, p.problem, p.solution, p.cta) if x.strip())


# Признаки материала-истории: у него есть герой, год и поворот. Обычный технический пост даёт этих признаков меньше двух.
_STORY_CUES = re.compile(
    r"(?:в|с|к)\s+(?:19|20)\d\d\s*(?:году|г\.|год)|впервые|основал|создал|придумал|изобрёл|изобрел|"
    r"история|случилось|случай|взлом|червь|червя|вирус|троян|ботнет|утечк|легенд|написал в одиночку|"
    r"почему\s+(?:у|он|она|они|первые)|назвали|появился|появилась|ненавидел|покинул|уволил",
    re.I)


def script_format(source_text: str) -> str:
    """«story» — история с героем, «argument» — спор с общепринятым (как раньше). Профиль general историй не получает."""
    if not settings.story_shorts or not profiles.current().story_shorts:
        return "argument"
    hits = {m.group(0).lower() for m in _STORY_CUES.finditer(source_text[:2500])}
    return "story" if len(hits) >= 2 else "argument"


async def write_script(source_text: str, pkg: ContentPackage, extra_instructions: str = "") -> None:
    """Отдельный сценарный этап: черновик → правка редактором. Модель не отвлекается на Telegram-пост.

    Если оба запроса упали, остаётся сценарий из общего запроса — ролик всё равно получится.
    """
    yt = pkg.youtube_shorts_stream
    fmt = script_format(source_text)
    system = STORY_SCRIPT_PROMPT if fmt == "story" else SCRIPT_PROMPT
    log.info("script format: %s", fmt)
    post = f"Исходный пост:\n<post>\n{source_text[:4000]}\n</post>"
    if extra_instructions:
        post += f"\n\nПожелания автора:\n{extra_instructions}"
    try:
        draft = await _generate(post, _VoiceParts, system, max_tokens=700)
    except AIError as e:
        log.warning("script draft failed, keeping the one-shot script: %s", e)
        return
    final = draft
    draft_json = json.dumps(draft.model_dump(), ensure_ascii=False, indent=2)
    review = f"{SCRIPT_REVIEW_PROMPT}\n\n{post}\n\nЧерновик:\n<draft>\n{draft_json}\n</draft>"
    try:
        edited = await _generate(review, _VoiceParts, system, max_tokens=700)
        # слабая модель на правке иногда раздувает или обрезает текст — берём правку, только если она не хуже по длине
        if _voice_score(_parts_text(edited)) <= _voice_score(_parts_text(draft)):
            final = edited
    except AIError as e:
        log.warning("script review failed, using the draft: %s", e)
    log.info("script: %d words (draft %d)", len(_parts_text(final).split()), len(_parts_text(draft).split()))
    yt.hook_0_3s, yt.problem_3_15s = _sentence(final.hook), _sentence(final.problem)
    yt.solution_15_40s, yt.cta_40_60s = _sentence(final.solution), _sentence(final.cta)
    yt.full_audio_script = _parts_text(final)


# Инструменты, которые слабая модель любит «додумывать» от себя. Если термина нет в исходнике,
# а в сценарии он есть — фраза с ним выдумана. Ключ — как термин пишут в посте, значение — как его
# пишут в сценарии после транслитерации «для диктора».
_INVENTED = {
    "kubernetes": r"kubernetes|k8s|кубернетес|кубер\b",
    "docker": r"docker|докер",
    "terraform": r"terraform|терраформ",
    "ansible": r"ansible|ансибл",
    "prometheus": r"prometheus|прометеус",
    "grafana": r"grafana|графана",
    "jenkins": r"jenkins|дженкинс",
    "gitlab": r"gitlab|гитлаб",
    "github": r"github|гитхаб",
    "helm": r"\bhelm\b|хелм",
    "nginx": r"nginx|энджинкс|нгинкс",
    "redis": r"redis|редис",
    "kafka": r"kafka|кафка",
    "postgres": r"postgres|постгрес",
    "mysql": r"mysql|майэскюэль",
    "aws": r"\baws\b|амазон",
    "linux": r"linux|линукс",
}


def _invented_terms(source: str) -> list[str]:
    """Термины-инструменты, которые есть в сценарии, но не упомянуты в исходном посте."""
    return [name for name, pat in _INVENTED.items() if not re.search(pat, source, re.I)]


def strip_invented(source: str, pkg: ContentPackage) -> list[str]:
    """Вырезаем из сценария фразы с выдуманными инструментами. Возвращает, что нашли (для лога).

    Если после вырезания часть осталась бы пустой, не трогаем её: пустой хук хуже лишнего слова.
    """
    yt = pkg.youtube_shorts_stream
    bad = [pat for name, pat in _INVENTED.items() if name in _invented_terms(source)]
    if not bad:
        return []
    rx = re.compile("|".join(bad), re.I)
    found: list[str] = []

    def clean(part: str) -> str:
        sentences = re.findall(r"[^.!?…]+[.!?…]?\s*", part)
        kept = [x for x in sentences if not rx.search(x)]
        found.extend(m.group(0).lower() for m in rx.finditer(part))
        return " ".join(x.strip() for x in kept) if kept else part

    parts = [yt.hook_0_3s, yt.problem_3_15s, yt.solution_15_40s, yt.cta_40_60s]
    new = [clean(p) for p in parts]
    if new != parts:
        yt.hook_0_3s, yt.problem_3_15s, yt.solution_15_40s, yt.cta_40_60s = new
        yt.full_audio_script = " ".join(_sentence(p) for p in new if p.strip())
    return found


def _hard_trim(tg: TelegramStream, limit: int) -> None:
    """Последний рубеж: выкидываем абзацы с конца formatted_text, пока пост не влезет."""
    while visible_length(build_post(tg)) > limit:
        paragraphs = tg.formatted_text.split("\n\n")
        if len(paragraphs) <= 2:
            tg.code_snippet = ""  # дальше резать текст некуда — жертвуем кодом
            if visible_length(build_post(tg)) > limit:
                tg.formatted_text = tg.formatted_text[: max(200, limit - 600)].rsplit(" ", 1)[0] + "…"
            return
        tg.formatted_text = "\n\n".join(paragraphs[:-1])


async def _fit_telegram(pkg: ContentPackage) -> None:
    """Длинный исходник — длинный пост: просим модель ужать, если не вышло — режем по абзацам."""
    for _ in range(2):
        length = visible_length(build_post(pkg.telegram_stream))
        if length <= TG_POST_MAX:
            return
        log.info("telegram post too long (%d), asking to shorten", length)
        try:
            pkg.telegram_stream = await edit_telegram(
                pkg.telegram_stream,
                f"Пост слишком длинный: {length} символов. Сократи formatted_text примерно до 1800 символов: "
                "оставь заголовок, суть проблемы, 3–5 главных шагов решения и вывод. Убери повторы и второстепенные детали.",
            )
            _normalize(pkg)
        except AIError as e:
            log.warning("telegram shorten failed: %s", e)
            break
    _hard_trim(pkg.telegram_stream, TG_POST_MAX)


_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _accept_review(old: TelegramStream, new: TelegramStream, source: str) -> bool:
    """Правку редактора берём, только если она не потеряла суть и не выдумала фактов."""
    o, n = visible_length(old.formatted_text), visible_length(new.formatted_text)
    if not (0.6 * o <= n <= 1.3 * o) or not new.formatted_text.strip():
        return False
    known = set(_NUM.findall(source)) | set(_NUM.findall(old.formatted_text + " " + old.cta_text))
    if set(_NUM.findall(new.formatted_text + " " + new.cta_text)) - known:
        return False                         # редактор добавил цифру, которой не было
    return not _invented_terms_in(source, old.formatted_text, new.formatted_text + " " + new.cta_text)


def _invented_terms_in(source: str, old_text: str, new_text: str) -> list[str]:
    """Инструменты, появившиеся в правке, но отсутствовавшие и в исходнике, и в старом тексте."""
    return [name for name, pat in _INVENTED.items()
            if re.search(pat, new_text, re.I) and not re.search(pat, source + " " + old_text, re.I)]


async def review_telegram(source_text: str, pkg: ContentPackage) -> bool:
    """Редакторский проход по Telegram-посту: грамматика, «ты», повторы вывода и призыва.

    Слабая модель в первом проходе ошибается в согласовании («ценная инсайты») и дублирует вывод призывом,
    а автопроверка грамматику не видит. Правка принимается только при выполнении _accept_review.
    """
    tg = pkg.telegram_stream
    content = (f"Исходный материал:\n<source>\n{source_text[:4000]}\n</source>\n\n"
               f"<post>\n{json.dumps(tg.model_dump(), ensure_ascii=False, indent=2)}\n</post>")
    try:
        edited = await _generate(content, TelegramStream, system=TG_REVIEW_PROMPT, max_tokens=1800)
    except AIError as e:
        log.warning("редактор Telegram-поста не отработал: %s", e)
        return False
    if not _accept_review(tg, edited, source_text):
        log.warning("правка редактора отклонена: слишком короткая, длинная или с новыми фактами")
        return False
    tg.formatted_text, tg.cta_text = edited.formatted_text, edited.cta_text   # код и хештеги оставляем как были
    return True


async def generate_package(source_text: str, extra_instructions: str = "") -> ContentPackage:
    content = f"Исходный пост из LinkedIn:\n<post>\n{source_text}\n</post>"
    if extra_instructions:
        content += f"\n\nДополнительные пожелания автора:\n{extra_instructions}"
    pkg = _normalize(await _generate(content, ContentPackage))
    await write_script(source_text, pkg, extra_instructions)
    invented = strip_invented(source_text, pkg)
    if invented:
        log.warning("из сценария вырезаны выдуманные инструменты: %s", sorted(set(invented)))
    await _fit_voice(source_text, pkg)
    await review_telegram(source_text, pkg)
    await _fit_telegram(pkg)
    return pkg


async def edit_telegram(current: TelegramStream, instructions: str) -> TelegramStream:
    content = (
        f"{TG_EDIT_PROMPT}\n\n<current>\n{json.dumps(current.model_dump(), ensure_ascii=False, indent=2)}\n</current>"
        f"\n\n<edits>\n{instructions}\n</edits>"
    )
    return await _generate(content, TelegramStream)


SCENE_PROMPT = profiles.current().prompt("scene")


def _clip_words(text: str, n: int) -> str:
    words = re.sub(r"\s+", " ", text).strip().split(" ")
    return " ".join(words[:n]).rstrip(",.;:—-")


async def scene_plan(source_text: str, pkg: ContentPackage) -> ScenePlan:
    """Отдельный короткий запрос: слабой модели проще выдать маленький JSON, чем всё сразу."""
    s = pkg.youtube_shorts_stream
    prompt = f"{SCENE_PROMPT}\n\nПост:\n<post>\n{source_text[:3000]}\n</post>\n\nСценарий диктора:\n<script>\n{s.full_audio_script}\n</script>"
    try:
        plan = await _generate(prompt, ScenePlan, max_tokens=1200)
    except AIError as e:
        log.warning("scene plan failed, using fallback: %s", e)
        title = re.sub(r"#\S+", "", s.youtube_title).strip()
        plan = ScenePlan(hook_big=_clip_words(title, 3), hook_small="", problem_before="", problem_after="",
                         steps=[], stock_queries=[])
    # цифра на весь экран должна быть из материала: слабая модель подставляет «50%» вместо слова «большинство»
    invented = set(_NUM.findall(plan.hook_big)) - set(_NUM.findall(source_text + " " + s.full_audio_script))
    if invented:
        log.warning("hook_big с выдуманной цифрой %s, заменяю заголовком", sorted(invented))
        plan.hook_big = _clip_words(re.sub(r"#\S+", "", s.youtube_title).strip(), 3)
        plan.hook_small = ""
    plan.hook_big = _clip_words(plan.hook_big, 4)
    plan.hook_small = _clip_words(plan.hook_small, 7)
    # «claim» нужен оба текста и ни одной выдуманной цифры; иначе остаётся привычная цифра на весь экран
    plan.myth, plan.truth = _clip_words(plan.myth, 9), _clip_words(plan.truth, 9)
    known_nums = set(_NUM.findall(source_text + " " + s.full_audio_script))
    if (plan.layout.strip().lower() != "claim" or not profiles.current().claim_layout
            or not (plan.myth and plan.truth) or set(_NUM.findall(plan.myth + " " + plan.truth)) - known_nums):
        plan.layout, plan.myth, plan.truth = "number", "", ""
    else:
        plan.layout = "claim"
    # схема: 3–4 узла, иконки из известного набора, ни одной выдуманной цифры; иначе остаются карточки «ошибка → итог»
    labels = [_clip_words(x, 3) for x in plan.flow if x.strip()][:4]
    icons = [i.strip().lower() for i in plan.flow_icons][:len(labels)]
    icons += ["box"] * (len(labels) - len(icons))
    plan.flow_icons = [i if i in FLOW_ICONS else "box" for i in icons]
    plan.flow = labels
    if (len(labels) < 3 or not profiles.current().flow_layout
            or set(_NUM.findall(" ".join(labels))) - known_nums):
        plan.flow, plan.flow_icons = [], []
    plan.problem_before = _clip_words(plan.problem_before, 6)
    plan.problem_after = _clip_words(plan.problem_after, 6)
    # призыв подписаться — не шаг решения
    plan.steps = [_clip_words(x, 6) for x in plan.steps if x.strip() and not re.search(r"telegram|телеграм|подпис|канал", x, re.I)][:3]
    plan.steps = [s[:1].upper() + s[1:] for s in plan.steps]
    queries = [q.strip() for q in plan.stock_queries if q.strip() and q.isascii()][:3]
    plan.stock_queries = queries or list(profiles.current().stock_fallback)
    # модель любит оборачивать код в ```: на экране эти строки не нужны
    plan.code = re.sub(r"^```\w*\s*|\s*```$", "", plan.code.strip()).strip()
    if plan.code.lower() in _EMPTY:
        plan.code = ""
    plan.code_language = plan.code_language.strip().lower() if plan.code else ""
    if not profiles.current().code_scene:   # в этих темах кода нет: терминал не показываем, шаги занимают сцену
        plan.code, plan.code_language = "", ""
    return plan
