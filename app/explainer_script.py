"""Сценарий длинного ролика (5–7 минут) из материала автора: план глав → текст и сцены каждой главы → проверка цифр.

Запуск: python -m app.explainer_script материал.txt сценарий.json [--chapters=9]
Дальше: python -m app.explainer сценарий.json папка
НЕ проверено на настоящей модели: промпты написаны по структуре референса и ждут первого живого прогона.
"""

import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from . import ai

log = logging.getLogger("explainer_script")

SYSTEM = """Ты сценарист научно-популярных роликов на YouTube по 5–7 минут. Зритель должен досмотреть и унести открытие, а не список фактов.

КАРКАС РОЛИКА
1. Хук: громкое утверждение или факт из материала, который сразу показывает, а не объявляет («украли шесть миллиардов», а не «сейчас расскажу про кражу»). Фразы «сегодня разберём», «в этом ролике» запрещены. Ответ не выдаёшь.
2. Что известно точно и что спорно: две-три короткие карточки, без воды.
3. Две-четыре истории с неожиданным поворотом: кто, когда, чего хотел, что вышло на деле. Конкретные имена, даты, суммы.
4. Одна аналогия из быта, которая объясняет механизм ИЗ МАТЕРИАЛА. Аналогию придумай под этот сюжет, а не бери готовую.
5. Смена взгляда: вывод, после которого тема выглядит иначе.

ПЕРЕЗАПУСК. Около середины ролика нужна глава-перезапуск: неожиданный факт, вопрос-поворот или «и вот тут начинается самое неприятное». Зритель к этому моменту устаёт, его надо вернуть.
КОНЦОВКА РЕЗКАЯ. Не сигналь, что ролик кончается: без «итак», «подведём итог», «главный урок», «в заключение». Последняя фраза — самая сильная мысль, и на ней всё обрывается.

ПРАВИЛА
- Только факты, цифры, имена и даты из материала. Нет в материале — не выдумывай и не «уточняй» по памяти.
- Речь для синтезатора голоса: фразы 4–12 слов, «ты» или безличные, без канцелярита, скобок, эмодзи, ссылок.
- Числа и сокращения пиши словами в text («сорок семь минут»). В цифрах (value, y) — арабскими.
- Эмоцию создают контраст и конкретика. Слов «удивительно», «шокирующе», «невероятно» нет.
- Штампы ИИ запрещены: «не X, а Y» чаще раза за ролик, перечни «без A, без B, без C».
- Английские названия и термины оставляй латиницей как в материале, остальное пиши по-русски."""


class _OutlineChapter(BaseModel):
    # значения по умолчанию — для проверки ответа слабой модели: пропущенное поле не должно ронять весь запрос;
    # в схему для YandexGPT поля всё равно попадают обязательными (ai._inline_schema)
    label: str = ""   # 1–3 слова: «Загадка», «Что спорно», «Бильярдные шары»
    beat: str = ""    # о чём глава: сюжет, факт или цифра ИЗ МАТЕРИАЛА
    twist: str = ""   # поворот, аналогия или смена взгляда этой главы


class _Outline(BaseModel):
    title: str = ""
    chapters: list[_OutlineChapter]


class Item(BaseModel):
    k: str       # verdict: заголовок карточки; timeline: год
    v: str       # verdict: текст; timeline: событие
    tone: str    # ok | warn | no (только для verdict, иначе пусто)


class Vis(BaseModel):
    type: Literal["date", "stat", "verdict", "analogy", "quote", "timeline", "flow", "bars", "poster", "icon", "terminal"]
    glitch: bool   # quote: помеха на фразе, ставь только на самых тревожных фразах («защита ничего не заметила»), не больше двух на ролик
    on: str       # первые 2–4 слова фразы ИЗ ТЕКСТА ГЛАВЫ, на которой появляется сцена (у первой сцены пусто)
    chip: str      # короткая плашка-термин в углу («Химики-аналитики», «Метод»), можно пусто
    text: str      # date: заголовок («Февраль 2025 года»); quote: фраза; poster: заголовок; icon: крупное слово
    sub: str       # date: подпись под заголовком
    value: str     # stat: число из материала
    unit: str      # stat: «%», «×», «лет»
    caption: str   # stat: что это за число
    icon: str      # icon: имя иконки (server, db, user, queue, cloud, code, clock, money, shield, bolt, box)
    a: str         # analogy: левая часть (слова, не имя иконки)
    a_sub: str
    b: str         # analogy: правая часть
    b_sub: str
    hl: list[str]  # quote: слова для акцента
    items: list[Item]  # verdict: карточки; timeline: k — год, v — событие; flow: k — иконка, v — подпись; bars: k — подпись, v — число


class _Chapter(BaseModel):
    text: str            # закадровый текст главы
    visuals: list[Vis]   # 4–6 сцен по порядку текста, каждая держится 3–6 секунд


_ICONS = ("server", "db", "user", "queue", "cloud", "code", "clock", "money", "shield", "bolt", "box")


def _digits(s: str) -> set[str]:
    # «6,3» в тексте ролика и «6.3» в английском материале — одно число
    return {x.replace(",", ".") for x in re.findall(r"\d+(?:[.,]\d+)?", s)}


_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


def _years_ok(text: str, source: str) -> bool:
    """Год в сцене должен встречаться в материале: слабая модель дописывает «2024», когда речь про другой год."""
    return all(y in source for y in _YEAR.findall(text))


def _clean_visual(v: Vis, source: str) -> dict | None:
    """Пустую или выдуманную сцену выкидываем: цифра на весь экран должна быть из материала."""
    d = {"type": v.type}
    if v.type == "date":
        if not v.text.strip() or not _years_ok(v.text + " " + v.sub, source):
            return None
        d.update(text=v.text.strip(), sub=v.sub.strip())
    elif v.type == "stat":
        val = v.value.strip().replace(" ", "")
        if not val or not _digits(val) <= _digits(source):
            return None
        d.update(value=val, unit=v.unit.strip(), caption=v.caption.strip(), max=100 if "%" in v.unit else None)
        if d["max"] is None:
            del d["max"]
    elif v.type == "verdict":
        items = [{"k": i.k.strip(), "v": i.v.strip(), "tone": i.tone if i.tone in ("ok", "warn", "no") else "ok"}
                 for i in v.items if i.k.strip() and i.v.strip()][:3]
        if len(items) < 2:
            return None
        d["items"] = items
    elif v.type == "analogy":
        if not (v.a.strip() and v.b.strip()):
            return None
        d.update(a=v.a.strip(), a_sub=v.a_sub.strip(), b=v.b.strip(), b_sub=v.b_sub.strip())
    elif v.type == "quote":
        # фраза на весь экран крупным кеглем: длиннее 14 слов она нечитаема
        if not v.text.strip() or len(v.text.split()) > 14:
            return None
        d.update(text=v.text.strip(), hl=[h.strip() for h in v.hl if h.strip()][:3])
        if v.glitch:
            d["glitch"] = True
    elif v.type == "terminal":
        # строки терминала выглядят как «настоящие», поэтому выдуманную строку пропустить нельзя:
        # оставляем только те, что дословно (без учёта пробелов и регистра) есть в материале
        flat = re.sub(r"\s+", "", source).lower()
        lines = [i.v.strip() for i in v.items if i.v.strip()]
        lines = [x for x in lines if len(x) <= 70 and re.sub(r"\s+", "", x).lower() in flat][:6]
        if len(lines) < 2:
            return None
        d.update(title=v.text.strip() or "log", lines=lines)
    elif v.type == "timeline":
        items = [{"y": i.k.strip(), "t": i.v.strip()} for i in v.items if i.k.strip() and i.v.strip()][:4]
        items = [i for i in items if _years_ok(i["y"] + " " + i["t"], source)]
        # «2024-06» из «июнь» тоже выдумка: числа в подписи года обязаны быть в материале целиком
        items = [i for i in items if not _digits(i["y"]) or _digits(i["y"]) <= _digits(source)]
        if len(items) < 2:
            return None
        d["items"] = items
    elif v.type == "flow":
        items = [{"icon": i.k.strip() if i.k.strip() in _ICONS else "box", "label": i.v.strip()}
                 for i in v.items if i.v.strip()][:4]
        if len(items) < 2:
            return None
        d["items"] = items
    elif v.type == "bars":
        items = [{"k": i.k.strip(), "v": i.v.strip(), "tone": i.tone if i.tone in ("warn", "no") else ""}
                 for i in v.items if i.k.strip() and i.v.strip()][:5]
        # числа столбцов, как и цифра на весь экран, должны быть из материала
        items = [i for i in items if _digits(i["v"]) and _digits(i["v"]) <= _digits(source)]
        if len(items) < 2:
            return None
        d.update(items=items, unit=v.unit.strip())
    elif v.type == "poster":
        if not v.text.strip():
            return None
        d.update(text=v.text.strip(), sub=v.sub.strip(), caption=v.caption.strip())
    elif v.type == "icon":
        if not v.text.strip():
            return None
        d.update(a=v.icon.strip() if v.icon.strip() in _ICONS else "box", text=v.text.strip(), sub=v.sub.strip())
    if v.on.strip():
        d["on"] = v.on.strip()
    if v.chip.strip():
        chip = v.chip.strip()
        d["chip"] = chip if len(chip) <= 34 else chip[:34].rsplit(" ", 1)[0]   # по границе слова, без «Северокорейс»
    return d


async def _outline(source: str, n: int) -> _Outline:
    prompt = (f"МАТЕРИАЛ:\n{source}\n\nСоставь план ролика из {n} глав по каркасу. Первая глава — хук, последняя — смена взгляда. "
              f"В каждой главе beat — конкретный сюжет, факт или цифра ИЗ МАТЕРИАЛА, twist — поворот, аналогия или вывод. "
              f"Название ролика — громкое утверждение или вопрос из материала.")
    return await ai._generate(prompt, _Outline, SYSTEM, max_tokens=1800)


async def _chapter(source: str, outline: _Outline, i: int) -> _Chapter:
    ch = outline.chapters[i]
    plan = "\n".join(f"{k + 1}. {c.label}: {c.beat}" for k, c in enumerate(outline.chapters))
    prompt = (
        f"МАТЕРИАЛ:\n{source}\n\nПЛАН РОЛИКА «{outline.title}»:\n{plan}\n\n"
        f"Напиши главу {i + 1} из {len(outline.chapters)}: «{ch.label}». О чём: {ch.beat}. Поворот или вывод: {ch.twist}.\n"
        f"text: не меньше 90 слов закадрового текста (7–10 предложений). visuals: 4–6 сцен по порядку текста, каждая на 3–6 секунд речи.\n"
        f"Какой тип сцены когда: date — дата или эпоха («Середина XIX века»); stat — ОДНО число из материала; "
        f"verdict — 2–3 карточки «факт / спорно / нет» (tone ok, warn, no); analogy — «А против Б» с подписями; "
        f"quote — одна острая фраза (hl — 1–2 слова для акцента); timeline — 3–4 события с годами (k — год, v — событие); "
        f"flow — схема из 2–4 шагов с иконками (k — имя иконки из: server, db, user, queue, cloud, code, clock, money, shield, bolt, box; v — подпись); "
        f"bars — 2–5 столбцов для сравнения чисел ИЗ МАТЕРИАЛА (k — подпись, v — число, unit — единица); "
        f"poster — «архивная афиша»: text — заголовок, sub — суть одной фразой, caption — источник и год; нужна, когда в сюжете есть документ, объявление или публикация; "
        f"icon — крупная иконка со словом (icon — имя иконки, text — слово, sub — пояснение); "
        f"terminal — окно терминала, только если в материале есть строки лога, команды или кода: в items.v положи 2–5 таких строк "
        f"ДОСЛОВНО из материала (до 70 знаков каждая), text — имя файла или команды; не нашёл дословных строк — не используй этот тип. "
        f"glitch=true — только у одной-двух самых тревожных цитат ролика, у остальных false. "
        f"analogy — две части словами («Делится» / «Не делится»), имена иконок туда не пиши. "
        f"Название главы (label) — 1–3 слова по сути («Фальшивое интервью»), без слов «Глава», «Хук», «История». "
        f"chip — короткая плашка-термин в углу сцены (до трёх слов), ставь на сценах, где вводится новое понятие. "
        f"on — первые 2–4 слова фразы из text, на которой сцена должна появиться на экране, ТОЧНО как они написаны в text "
        f"(графика выходит ровно тогда, когда диктор произносит эту фразу); у первой сцены главы on пустое. "
        f"Не повторяй один тип подряд. Ненужные поля оставь пустыми. "
        + ("Это первая глава: открой хуком и честной оговоркой, ответ не выдавай. " if i == 0 else "")
        + ("Это последняя глава: закончи сменой взгляда и фразой, которую хочется пересказать." if i == len(outline.chapters) - 1 else "")
    )
    return await ai._generate(prompt, _Chapter, SYSTEM, max_tokens=2500)


async def write(source: str, n_chapters: int = 9) -> dict:
    outline = await _outline(source, n_chapters)
    outline.chapters = [c for c in outline.chapters if c.beat.strip() or c.label.strip()][:n_chapters + 2]
    if not outline.chapters:
        raise ai.AIError("модель не вернула ни одной главы")
    if not outline.title.strip():
        outline.title = outline.chapters[0].beat.strip()[:90] or outline.chapters[0].label
    log.info("план: %d глав, название: %s", len(outline.chapters), outline.title)
    # главы пишутся параллельно: ответы модели идут минутами, последовательно ждать нечего
    chapters = await asyncio.gather(*(_chapter(source, outline, i) for i in range(len(outline.chapters))))
    out = []
    for c_out, c in zip(outline.chapters, chapters):
        vis = [x for x in (_clean_visual(v, source) for v in c.visuals) if x]
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", " ".join(c.text.split())) if s]
        if len(vis) < 3:
            # слабая модель даёт 1–2 сцены на главу, и ролик «залипает»: добираем короткими фразами самой главы
            have = {v.get("text") for v in vis}
            extra = [s for s in sentences if len(s.split()) <= 12 and s not in have]
            vis += [{"type": "quote", "text": s, "hl": []} for s in extra[:3 - len(vis)]]
        if not vis:
            vis = [{"type": "quote", "text": " ".join(sentences[0].split()[:12]) if sentences else c_out.label, "hl": []}]
        label = re.sub(r"^\s*(глава\s*\d+\s*[:.\-–—]?\s*)", "", c_out.label, flags=re.I).strip()
        if len(label) < 3 or len(label.split()) > 4 or label.lower() in ("хук", "история", "вывод"):
            label = ""   # модель дала «Глава 1», «Хук» или обрывок фразы; ниже подставим нейтральное по позиции
        out.append({"label": label, "text": " ".join(c.text.split()), "visuals": vis})
    for i, c in enumerate(out):
        if not c["label"]:
            # «Итог» не используем: название главы тоже сигналит о конце ролика
            c["label"] = "Загадка" if i == 0 else f"Часть {i + 1}"
    # числа в закадровом тексте, которых нет в материале, — кандидаты в выдумку: автор проверяет их до озвучки
    src_digits = _digits(source)
    warnings = []
    for i, c in enumerate(out, 1):
        bad = sorted(_digits(c["text"]) - src_digits)
        if bad:
            warnings.append(f"глава {i}: числа не из материала: {', '.join(bad)}")
    for w in warnings:
        log.warning(w)
    words = sum(len(c["text"].split()) for c in out)
    log.info("слов: %d (~%.1f мин при 2,4 слова/с)", words, words / 2.4 / 60)
    return {"title": outline.title, "chapters": out, "warnings": warnings}


def main(argv: list[str]) -> None:
    args = [a for a in argv if not a.startswith("--")]
    n = next((int(a.split("=")[1]) for a in argv if a.startswith("--chapters=")), 9)
    if len(args) < 2:
        raise SystemExit(__doc__)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    script = asyncio.run(write(Path(args[0]).read_text(encoding="utf-8"), n))
    Path(args[1]).write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print("готово:", args[1])


if __name__ == "__main__":
    main(sys.argv[1:])
