"""Стоковые видео с Pixabay под тему ролика (фон под инфографикой).

Нужен бесплатный ключ: pixabay.com/api/docs (виден после входа). Без ключа — пустой список,
и фоном будет анимированный градиент. По условиям Pixabay видео можно использовать бесплатно,
в том числе в коммерческих роликах, без указания авторства.
"""

import logging
import re
from pathlib import Path

import httpx

from .config import settings

log = logging.getLogger(__name__)

MAX_BYTES = 60 * 1024 * 1024
_API = "https://pixabay.com/api/"
_CDN = "https://cdn.pixabay.com/"


def _via_worker(url: str) -> tuple[str, dict]:
    """С сервера в РФ ходим через Cloudflare Worker (cloudflare/worker.js), как к Telegram и YouTube."""
    base = settings.telegram_api_url.rstrip("/")
    if not base:
        return url, {}
    for prefix, route in ((_API, "/px/api/"), (_CDN, "/px/cdn/")):
        if url.startswith(prefix):
            return base + route + url[len(prefix):], {"X-Proxy-Secret": settings.cf_proxy_secret}
    return url, {}


# шаблоны концовок и интро («thank you for watching», «subscribe») Pixabay отдаёт по любому запросу
# про экраны и анимацию, а на фоне технического ролика они выглядят как чужая реклама
_JUNK_TAGS = re.compile(
    r"subscri|thank|watching|youtube|outro|intro|template|countdown|lower.?third|logo|social|"
    r"like|follow|bell|title|text|banner|overlay|green.?screen|transition", re.I)


def _is_junk(hit: dict) -> bool:
    return bool(_JUNK_TAGS.search(hit.get("tags", "")))


_STOP = {"on", "in", "at", "of", "the", "a", "with", "and"}
# теги, по которым видно, что клип про технику, а не про костёр или природу
_TECH_TAGS = re.compile(r"server|code|data|network|digital|technology|computer|cyber|matrix|"
                        r"programming|circuit|screen|binary|cloud|hardware", re.I)


def _relevance(hit: dict, query: str) -> int:
    """Pixabay ищет по слову где-нибудь в описании: на «code on screen» отдаёт костёр.
    Требуем, чтобы хоть одно слово запроса было среди тегов; технические теги дают бонус."""
    tags = hit.get("tags", "").lower()
    words = [w for w in re.findall(r"[a-z]+", query.lower()) if w not in _STOP]
    hits = sum(1 for w in words if w in tags)
    tech = len(_TECH_TAGS.findall(tags))
    # одно совпавшее слово без техники — случайность: «programmer at night» цепляет лунное небо по слову night
    return hits * 3 + tech if hits and tech else -1


def _pick(hits: list[dict], used: set[int], query: str = "") -> dict | None:
    """Предпочитаем вертикальные ролики, затем — подлиннее (чтобы хватило на сцену)."""
    best, best_score = None, -1.0
    for h in hits:
        if h["id"] in used or h.get("duration", 0) < 4 or _is_junk(h):
            continue
        rel = _relevance(h, query) if query else 0
        if rel < 0:
            continue
        v = h["videos"].get("medium") or h["videos"].get("small")
        if not v or not v.get("url"):
            continue
        vertical = v.get("height", 0) > v.get("width", 1)
        score = (10 if vertical else 0) + min(h.get("duration", 0), 20) / 2 + h.get("likes", 0) / 1000 + rel
        if score > best_score:
            best, best_score = h, score
    return best


async def fetch_clips(queries: list[str], workdir: Path) -> list[Path]:
    if not settings.pixabay_api_key:
        return []
    clips: list[Path] = []
    used: set[int] = set()
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=120), follow_redirects=True) as client:
        for i, query in enumerate(queries):
            try:
                api_url, headers = _via_worker(_API + "videos/")
                r = await client.get(api_url, headers=headers, params={
                    "key": settings.pixabay_api_key, "q": query, "per_page": 20, "safesearch": "true",
                })
                if r.status_code != 200:
                    log.warning("pixabay %s: %s %s", query, r.status_code, r.text[:200])
                    continue
                hit = _pick(r.json().get("hits", []), used, query)
                if not hit:
                    log.info("pixabay: nothing for %r", query)
                    continue
                used.add(hit["id"])
                log.info("pixabay %r -> %s tags=%s", query, hit["id"], hit.get("tags"))
                v = hit["videos"].get("medium") or hit["videos"]["small"]
                path = workdir / f"stock_{i}.mp4"
                file_url, headers = _via_worker(v["url"])
                async with client.stream("GET", file_url, headers=headers) as resp:
                    resp.raise_for_status()
                    size = 0
                    with path.open("wb") as f:
                        async for chunk in resp.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_BYTES:
                                raise ValueError("clip too large")
                            f.write(chunk)
                clips.append(path)
            except (httpx.HTTPError, ValueError, KeyError) as e:
                log.warning("pixabay clip %r failed: %s", query, e)
    return clips
