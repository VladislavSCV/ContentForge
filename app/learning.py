"""Обратная связь: какие посты бота что набрали и как это меняет подбор тем.

1. Связь с LinkedIn. Бот не знает адрес вашего поста в LinkedIn, но знает его текст (черновик, который сам
   написал). LinkedIn строит адрес из первых слов поста, так что пост из выгрузки сопоставляется с черновиком
   по началу текста. Ничего вводить вручную не нужно.
2. Подстройка тем. Когда накопится MIN_POSTS постов с цифрами, для каждой темы считается, во сколько раз
   такие посты набирают больше или меньше остальных; поправка к весу темы (не больше CAP в обе стороны)
   сохраняется и учитывается при следующем подборе. До этого вес остаётся таким, как задан вручную.

Выводы статистически слабые: тема, день и первые часы показа влияют на охват не меньше темы. Поэтому поправка
ограничена, а в итогах недели видно, на чём она основана.
"""

import json
import math
import re
import statistics

from . import db, profiles, topics
from .analytics import slug_of

MIN_POSTS = 20      # столько постов с цифрами нужно, чтобы доверять сравнению
MIN_GROUP = 3       # и столько в каждой из двух сравниваемых групп
CAP = 1.5           # поправка к весу темы по модулю
STATE_KEY = "topic_adj"

LABELS = {f"t{i}": label for i, label in enumerate(profiles.current().theme_labels)}
LABELS.update({"story": "истории с цифрами", "contrarian": "споры с общепринятым"})


def _words(text: str) -> list[str]:
    return re.findall(r"[a-zа-яё0-9]+", text.lower())


def match_linkedin(posts: list[db.Post], li_rows: list[dict]) -> dict[int, dict]:
    """post.id → строка выгрузки LinkedIn. Совпадение по началу текста черновика."""
    drafts = {}
    for p in posts:
        if p.linkedin_json:
            try:
                drafts[p.id] = _words(json.loads(p.linkedin_json)["text"])
            except (ValueError, KeyError):
                continue
    out: dict[int, dict] = {}
    for row in li_rows:
        slug = _words(slug_of(row["url"]))
        core = slug[:-1] if len(slug) > 5 else slug     # последнее слово в адресе может быть обрезано
        if len(core) < 4:
            continue
        for pid, words in drafts.items():
            if pid not in out and words[:len(core)] == core:
                out[pid] = row
                break
    return out


def performance(posts: list[db.Post], li_rows: list[dict], yt_rows: list[dict]) -> list[tuple[db.Post, float, dict]]:
    """(пост, отнесённый к медиане результат, детали). Результат — среднее по доступным каналам: 1,0 — типичный пост."""
    linked = match_linkedin(posts, li_rows)
    li_med = statistics.median(r["impressions"] for r in li_rows) if li_rows else 0
    yt_by_id = {r["id"]: r for r in yt_rows if "id" in r}
    yt_med = statistics.median(r["views"] for r in yt_by_id.values()) if yt_by_id else 0
    out = []
    for p in posts:
        parts, detail = [], {}
        if p.id in linked and li_med:
            detail["li"] = linked[p.id]["impressions"]
            parts.append(detail["li"] / li_med)
        yt = yt_by_id.get(p.youtube_video_id)
        if yt and yt_med:
            detail["yt"] = yt["views"]
            parts.append(yt["views"] / yt_med)
        if parts:
            out.append((p, sum(parts) / len(parts), detail))
    return out


def _groups() -> dict[str, str]:
    g = {f"t{i}": rx for i, (_, rx) in enumerate(topics._THEMES)}
    g["story"], g["contrarian"] = topics._STORY, topics._CONTRARIAN
    return g


def compute(perf: list[tuple[db.Post, float, dict]]) -> dict[str, float]:
    """Поправки к весам тем. Пусто, пока постов с цифрами меньше MIN_POSTS."""
    if len(perf) < MIN_POSTS:
        return {}
    adj = {}
    for key, rx in _groups().items():
        inside = [v for p, v, _ in perf if re.search(rx, p.source_text[:800].lower())]
        outside = [v for p, v, _ in perf if not re.search(rx, p.source_text[:800].lower())]
        if len(inside) < MIN_GROUP or len(outside) < MIN_GROUP:
            continue
        a, b = statistics.mean(inside), statistics.mean(outside)
        if a <= 0 or b <= 0:
            continue
        adj[key] = round(max(-CAP, min(CAP, math.log2(a / b))), 1)
    return adj


async def load_adj() -> dict[str, float]:
    raw = await db.get_state(STATE_KEY)
    try:
        return {k: float(v) for k, v in json.loads(raw).items()} if raw else {}
    except (ValueError, AttributeError):
        return {}


async def update(posts: list[db.Post], li_rows: list[dict], yt_rows: list[dict]) -> tuple[list, dict[str, float]]:
    """Пересчитывает и сохраняет поправки; возвращает (результаты постов, поправки)."""
    perf = performance(posts, li_rows, yt_rows)
    adj = compute(perf)
    await db.set_state(STATE_KEY, json.dumps(adj))
    return perf, adj


def summary(perf: list[tuple[db.Post, float, dict]], adj: dict[str, float]) -> str:
    """Блок для итогов недели: что связали и подстроен ли подбор тем."""
    if not perf:
        return ("Связь с постами бота: пока не нашёл. Для сопоставления нужны посты LinkedIn, написанные кнопкой "
                "«💼 Пост для LinkedIn», или ролики, загруженные ботом на YouTube.")
    lines = [f"Что набрали посты бота (в сравнении с типичным постом, 1,0 = типичный; найдено: {len(perf)}):"]
    for p, v, d in sorted(perf, key=lambda x: x[1], reverse=True)[:3]:
        src = " · ".join(f"{name} {num}" for name, num in (("LinkedIn", d.get("li")), ("YouTube", d.get("yt"))) if num)
        title = p.package.youtube_shorts_stream.youtube_title.split("#")[0].strip() if p.package else p.source_text[:50]
        lines.append(f"   ×{v:.1f} · {title[:55]} ({src})")
    if len(perf) < MIN_POSTS:
        lines.append(f"Подбор тем пока ручной: накоплено {len(perf)} из {MIN_POSTS} постов с цифрами.")
    elif adj:
        lines.append("Подбор тем подстроен по цифрам: " + ", ".join(
            f"{LABELS.get(k, k)} {v:+.1f}" for k, v in sorted(adj.items(), key=lambda kv: -abs(kv[1])) if v))
    else:
        lines.append("Подбор тем: постов хватает, но различий между темами пока не видно.")
    return "\n".join(lines)
