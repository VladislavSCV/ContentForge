"""Best-effort извлечение текста публичного поста LinkedIn по ссылке.

LinkedIn отдаёт публичные посты без логина с JSON-LD (articleBody) и og:description.
Если пост не публичный или LinkedIn отдал authwall — возвращаем None, и бот просит
прислать текст вручную.
"""

import json
import logging
import re

import httpx
from bs4 import BeautifulSoup

from .config import settings

log = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://(?:[\w-]+\.)?linkedin\.com/\S+", re.I)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}


def find_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(").,>") if m else None


def _from_ld_json(soup: BeautifulSoup) -> str | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data, *data.get("@graph", [])] if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict):
                body = item.get("articleBody") or item.get("text")
                if isinstance(body, str) and len(body) > 80:
                    return body.strip()
    return None


async def _fetch_html(url: str) -> str | None:
    worker = settings.telegram_api_url.rstrip("/")
    try:
        async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=30) as client:
            if worker:  # linkedin.com заблокирован в РФ — качаем через Cloudflare Worker
                r = await client.get(f"{worker}/li", params={"url": url}, headers={"X-Proxy-Secret": settings.cf_proxy_secret})
                final_url = r.headers.get("x-final-url", "")
            else:
                r = await client.get(url)
                final_url = str(r.url)
    except httpx.HTTPError as e:
        log.warning("linkedin fetch failed: %s", e)
        return None
    if r.status_code != 200 or "authwall" in final_url:
        log.warning("linkedin returned %s (%s)", r.status_code, final_url)
        return None
    return r.text


async def fetch_post_text(url: str) -> str | None:
    html = await _fetch_html(url)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    text = _from_ld_json(soup)
    if not text:
        el = soup.select_one('[data-test-id="main-feed-activity-card__commentary"], .attributed-text-segment-list__content')
        if el:
            text = el.get_text("\n", strip=True)
    if not text:
        meta = soup.find("meta", property="og:description") or soup.find("meta", attrs={"name": "description"})
        text = meta.get("content") if meta else None
    return text.strip() if text and len(text.strip()) > 80 else None
