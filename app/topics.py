"""Поиск тем: свежие материалы из RSS/Atom-лент, отсев воды, оценка по полезности для канала.

Оценка эвристическая, без модели: ответы YandexGPT идут минутами, а ленты нужно просматривать
каждый день. Эвристика не заменяет автора: она сужает сотни записей до трёх, а выбирает человек.
Список лент задаёт профиль (sources_file в profile.toml) или SOURCES_FILE в .env. Недоступная лента просто пропускается.
"""

import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape

import httpx
from bs4 import BeautifulSoup

from . import db, profiles, topic_rank
from .config import settings

log = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ContentOpsBot/1.0)"}
MAX_FEED_BYTES = 2_000_000
MAX_AGE_DAYS = 45
MIN_SCORE = 3.0

# Словарь тем, столпы и ленты задаёт профиль (profiles/<имя>/profile.toml): (вес, шаблон), индекс i = ключ темы t{i}
_PROFILE = profiles.current()
_THEMES = list(_PROFILE.themes)
# Столпы бренда: за раз берём по одной теме на столп, иначе корпоративные блоги забивают подборку.
# Профиль без столпов получает пустой словарь, и подбор идёт просто по оценке.
PILLARS = dict(_PROFILE.pillars)
_PILLAR_MARKET = _PROFILE.market_pattern
_PILLAR_AI = _PROFILE.ai_pattern

# истории «как мы сделали и что получилось» ложатся в наш формат: есть цифра и вывод
_STORY = r"\bhow we\b|\bwhy we\b|\bwe (cut|reduced|migrated|saved|moved|replaced|rebuilt|lost)\b|\d+ ?%|\blessons?\b|\bmistakes?\b"
# спор с общепринятым: так начинались посты автора с лучшими охватами («не нужна Kafka», «это не A, а B»)
_CONTRARIAN = r"don.t need|you (probably )?don.t|\bmyth|stop using|considered harmful|paradox|overrated|is dead|isn.t (a|the)|not (a|the) \w+ problem|\bwhy we (stopped|left|moved off)"
_JUNK = (r"webinar|webcast|register now|we.?re hiring|join us|sponsor|press release|\bawards?\b|"
         r"announces? (a )?(partnership|funding|acquisition)|\bevent\b|\bpodcast\b|newsletter")
_VERSION = r"release notes|\bv?\d+\.\d+\.\d+\b|changelog"


@dataclass
class Candidate:
    title: str
    url: str
    summary: str
    source: str
    published: datetime | None
    score: float = 0.0
    pillar: str = ""


def pillar_of(c: "Candidate") -> str:
    """Столп по заголовку: рынок и AI — по словарю, всё остальное считаем инфраструктурой."""
    if not _PILLAR_MARKET:
        return ""
    title = c.title.lower()
    if re.search(_PILLAR_MARKET, title):
        return "market"
    if _PILLAR_AI and re.search(_PILLAR_AI, title):
        return "ai"
    return "infra"


def _hn_signal(summary: str) -> float:
    """hnrss кладёт в описание «Points: N … # Comments: M»: обсуждаемость говорит об охвате лучше словаря."""
    pts = re.search(r"points:\s*(\d+)", summary, re.I)
    com = re.search(r"comments:\s*(\d+)", summary, re.I)
    p, k = int(pts.group(1)) if pts else 0, int(com.group(1)) if com else 0
    return (2 if p >= 300 else 1 if p >= 150 else 0) + (1 if k >= 100 else 0)


def load_sources() -> list[str]:
    path = settings.sources_file or _PROFILE.sources_file
    if not path.exists():
        return []
    lines = (ln.split("#", 1)[0].strip() for ln in path.read_text(encoding="utf-8").splitlines())
    return [ln for ln in lines if ln.startswith("http")]


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html or ""))).strip()


def _date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)               # RSS: RFC 822
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))   # Atom: ISO 8601
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_feed(xml_bytes: bytes, source: str) -> list[Candidate]:
    """RSS 2.0 и Atom. Чужой XML: размер ограничен выше, внешние сущности ElementTree не раскрывает."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    out: list[Candidate] = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] not in ("item", "entry"):
            continue
        fields: dict[str, str] = {}
        link = ""
        for ch in node:
            name = ch.tag.rsplit("}", 1)[-1]
            if name == "link":
                link = link or ch.get("href") or (ch.text or "").strip()
            elif name in ("title", "description", "summary", "content", "pubDate", "published", "updated"):
                fields.setdefault(name, ch.text or "")
        title = _text(fields.get("title", ""))
        if not title or not link.startswith("http"):
            continue
        body = fields.get("description") or fields.get("summary") or fields.get("content") or ""
        out.append(Candidate(title, link, _text(body)[:700], source,
                             _date(fields.get("pubDate") or fields.get("published") or fields.get("updated"))))
    return out


def score(c: Candidate, now: datetime | None = None, adj: dict[str, float] | None = None) -> float:
    """adj — поправки к весам тем по реальным цифрам (app/learning.py): ключи t0…t5, story, contrarian."""
    now = now or datetime.now(timezone.utc)
    adj = adj or {}
    blob = f"{c.title} {c.summary}".lower()
    s = sum(w + adj.get(f"t{i}", 0) for i, (w, rx) in enumerate(_THEMES) if re.search(rx, blob))
    s += 2 + adj.get("story", 0) if re.search(_STORY, blob) else 0
    s += 1 + adj.get("contrarian", 0) if re.search(_CONTRARIAN, blob) else 0
    # конкретная цифра в заголовке даёт ролику хук с числом; абстрактные обзоры дают скучные шаги без кода и цифр
    s += 1 if re.search(r"\d", c.title) else 0
    s -= 1 if re.search(r"best practices|introduction to|overview|getting started|guide to|what is\b", blob) else 0
    s -= 4 if re.search(_JUNK, blob) else 0
    s -= 2 if re.search(_VERSION, c.title.lower()) else 0
    if c.published:
        age = (now - c.published).total_seconds() / 86400
        s += 2 if age <= 1 else 1 if age <= 3 else -2 if age > 14 else 0
        if age > MAX_AGE_DAYS:
            return -99.0
    if len(c.summary) < 80:          # без описания не понять, о чём статья
        s -= 1
    return s + _hn_signal(c.summary)


def _words(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-zа-я0-9]{4,}", title.lower())}


def pick(cands: list[Candidate], seen_urls: set[str], n: int, now: datetime | None = None,
         adj: dict[str, float] | None = None, by_pillar: bool = True, rescored: bool = False) -> list[Candidate]:
    """Лучшие n новых тем. Не больше одной с источника, без почти одинаковых заголовков;
    сначала по одной теме на столп (если у столпа есть кандидат), остальное добирается по оценке.
    rescored=True — оценки уже посчитаны (второй этап добавил поправки модели), заново не считать."""
    for c in cands:
        if not rescored:
            c.score = score(c, now, adj)
        c.pillar = pillar_of(c)
    ranked = sorted((c for c in cands if c.url not in seen_urls and c.score >= MIN_SCORE),
                    key=lambda c: c.score, reverse=True)
    chosen: list[Candidate] = []

    def take(c: Candidate) -> bool:
        if any(c.source == x.source for x in chosen):
            return False
        w = _words(c.title)
        if any(w and len(w & _words(x.title)) / len(w | _words(x.title)) > 0.5 for x in chosen):
            return False
        chosen.append(c)
        return True

    if by_pillar:
        for pillar in PILLARS:
            for c in ranked:
                if c.pillar == pillar and take(c):
                    break
            if len(chosen) == n:
                break
    for c in ranked:
        if len(chosen) == n:
            break
        if c not in chosen:
            take(c)
    return sorted(chosen, key=lambda c: c.score, reverse=True)


async def _fetch_feed(client: httpx.AsyncClient, url: str) -> list[Candidate]:
    try:
        r = await client.get(url)
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("лента недоступна %s: %s", url, e)
        return []
    host = re.sub(r"^https?://(www\.)?", "", url).split("/", 1)[0]
    return parse_feed(r.content[:MAX_FEED_BYTES], host)


async def find(n: int | None = None) -> list[db.Topic]:
    """Обходит ленты, выбирает лучшие новые темы и запоминает их как предложенные."""
    urls = load_sources()
    if not urls:
        return []
    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=20) as client:
        batches = await asyncio.gather(*(_fetch_feed(client, u) for u in urls))
    cands = [c for b in batches for c in b]
    log.info("темы: %s лент, %s записей", len(urls), len(cands))
    topics: list[db.Topic] = []
    # поправки по итогам прошлых недель; пусто, пока постов с цифрами мало (см. learning.py)
    try:
        adj = {k: float(v) for k, v in json.loads(await db.get_state("topic_adj") or "{}").items()}
    except (ValueError, AttributeError):
        adj = {}
    seen, want = await db.topic_urls(), n or settings.topics_count
    # финалисты эвристики (по 3 на столп — запас для второго этапа) → один запрос модели → итоговый выбор
    finalists = pick(cands, seen, want * 4, adj=adj)
    bonuses = await topic_rank.rank(finalists)
    for i, c in enumerate(finalists):
        c.score += bonuses.get(i, 0.0)
    for c in pick(finalists, seen, want, adj=None, rescored=True):
        label = PILLARS.get(c.pillar, "")
        t = await db.add_topic(c.url, c.title, c.summary, f"{c.source} · {label}" if label else c.source, c.score)
        if t:
            topics.append(t)
    return topics


async def fetch_article(url: str) -> str:
    """Текст статьи для генерации: абзацы из <article>/<main>. Пустая строка, если не вышло."""
    try:
        async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=30) as client:
            r = await client.get(url)
            r.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("статья недоступна %s: %s", url, e)
        return ""
    soup = BeautifulSoup(r.text[:3_000_000], "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    paras = [p.get_text(" ", strip=True) for p in root.find_all(["p", "li", "pre"])]
    text = "\n\n".join(p for p in paras if len(p) >= 40)
    return text[:6000]
