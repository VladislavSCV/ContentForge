"""YouTube Data API v3: загрузка Shorts (только право youtube.upload).

Если задан TELEGRAM_API_URL (Cloudflare Worker, cloudflare/worker.js), запросы к Google идут через него —
с сервера в РФ googleapis.com может быть недоступен. Иначе — напрямую.
"""

import json
import logging
from pathlib import Path

import httpx

from .config import settings

log = logging.getLogger(__name__)

# Только загрузка: с этим токеном нельзя удалить или изменить существующие видео, даже если он утечёт.
# Комментарий со ссылкой бот присылает автору в Telegram — вставить и закрепить его нужно вручную.
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

_OAUTH = "https://oauth2.googleapis.com/"
_API = "https://www.googleapis.com/"
_YA = "https://youtubeanalytics.googleapis.com/"


class YouTubeError(RuntimeError):
    pass


def _via_worker(url: str) -> str:
    base = settings.telegram_api_url.rstrip("/")
    if not base:
        return url
    if url.startswith(_OAUTH):
        return f"{base}/g/oauth2/{url[len(_OAUTH):]}"
    if url.startswith(_API):
        return f"{base}/g/api/{url[len(_API):]}"
    if url.startswith(_YA):
        return f"{base}/g/ya/{url[len(_YA):]}"
    return url


def _headers(extra: dict | None = None) -> dict:
    h = dict(extra or {})
    if settings.telegram_api_url:
        h["X-Proxy-Secret"] = settings.cf_proxy_secret
    return h


def _title(title: str) -> str:
    title = title.replace("<", "").replace(">", "").strip()
    if "#shorts" not in title.lower():
        title = f"{title} #shorts"
    return title[:100]


async def _access_token(client: httpx.AsyncClient, stats: bool = False) -> str:
    r = await client.post(
        _via_worker(_OAUTH + "token"),
        headers=_headers(),
        data={
            "client_id": settings.youtube_client_id,
            "client_secret": settings.youtube_client_secret,
            "refresh_token": settings.youtube_stats_refresh_token if stats else settings.youtube_refresh_token,
            "grant_type": "refresh_token",
        },
    )
    if r.status_code != 200:
        hint = (" — токен протух/отозван, запусти scripts/youtube_auth.py" + (" --stats" if stats else "") + " заново"
                if "invalid_grant" in r.text else "")
        raise YouTubeError(f"OAuth {r.status_code}: {r.text[:200]}{hint}")
    return r.json()["access_token"]


async def upload_short(video: Path, title: str, description: str, tags: list[str]) -> str:
    if not settings.youtube_enabled:
        raise YouTubeError("YouTube не настроен: нет YOUTUBE_CLIENT_ID / SECRET / REFRESH_TOKEN")
    data = video.read_bytes()
    meta = {
        "snippet": {
            "title": _title(title),
            "description": description.replace("<", "").replace(">", "")[:5000],
            "tags": [t.lstrip("#") for t in tags][:15],
            "categoryId": settings.youtube_category_id,
        },
        "status": {"privacyStatus": settings.youtube_privacy, "selfDeclaredMadeForKids": False},
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, write=600, read=600)) as client:
            token = await _access_token(client)
            # 1. resumable-сессия с метаданными
            r = await client.post(
                _via_worker(_API + "upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"),
                headers=_headers({
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Upload-Content-Type": "video/mp4",
                    "X-Upload-Content-Length": str(len(data)),
                }),
                content=json.dumps(meta).encode(),
            )
            location = r.headers.get("location")
            if r.status_code != 200 or not location:
                raise YouTubeError(f"YouTube init {r.status_code}: {r.text[:300]}")
            # 2. сам файл (Shorts — десятки МБ)
            r = await client.put(
                _via_worker(location),
                headers=_headers({"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"}),
                content=data,
            )
            if r.status_code not in (200, 201):
                raise YouTubeError(f"YouTube upload {r.status_code}: {r.text[:300]}")
            return r.json()["id"]
    except httpx.HTTPError as e:
        raise YouTubeError(f"сеть: {type(e).__name__}: {e}") from e


async def video_stats(ids: list[str]) -> dict[str, dict]:
    """Просмотры, лайки и комментарии по видео (Data API, право youtube.readonly). Пачками по 50."""
    out: dict[str, dict] = {}
    if not ids:
        return out
    if not settings.youtube_stats_enabled:
        raise YouTubeError("нет YOUTUBE_STATS_REFRESH_TOKEN: запусти scripts/youtube_auth.py --stats")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            token = await _access_token(client, stats=True)
            for i in range(0, len(ids), 50):
                r = await client.get(_via_worker(_API + "youtube/v3/videos"), headers=_headers({"Authorization": f"Bearer {token}"}),
                                     params={"part": "statistics", "id": ",".join(ids[i:i + 50])})
                if r.status_code != 200:
                    raise YouTubeError(f"videos {r.status_code}: {r.text[:200]}")
                for item in r.json().get("items", []):
                    st = item.get("statistics", {})
                    out[item["id"]] = {"views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
                                       "comments": int(st.get("commentCount", 0))}
    except httpx.HTTPError as e:
        raise YouTubeError(f"сеть: {type(e).__name__}: {e}") from e
    return out


async def retention(ids: list[str], start: str, end: str) -> dict[str, dict]:
    """Удержание: средний процент просмотра и новые подписчики по видео (Analytics API, yt-analytics.readonly).

    Данные приходят с задержкой 2–3 дня, у свежих роликов могут быть пустыми. Любой сбой — пустой ответ:
    это приятное дополнение к просмотрам, а не повод ронять итоги недели.
    """
    if not ids or not settings.youtube_stats_enabled:
        return {}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            token = await _access_token(client, stats=True)
            r = await client.get(_via_worker("https://youtubeanalytics.googleapis.com/v2/reports"),
                                 headers=_headers({"Authorization": f"Bearer {token}"}),
                                 params={"ids": "channel==MINE", "startDate": start, "endDate": end,
                                         "metrics": "views,averageViewPercentage,subscribersGained",
                                         "dimensions": "video", "filters": "video==" + ",".join(ids[:200]),
                                         "sort": "-views", "maxResults": 200})
    except (httpx.HTTPError, YouTubeError) as e:
        log.warning("YouTube Analytics недоступен: %s", e)
        return {}
    if r.status_code != 200:
        log.warning("YouTube Analytics %s: %s", r.status_code, r.text[:200])
        return {}
    return {row[0]: {"avg_pct": float(row[2]), "subs": int(row[3])} for row in r.json().get("rows", [])}


async def check(stats: bool = False) -> str:
    """Проверяет, что токен YouTube жив; возвращает его права (scope)."""
    if not (settings.youtube_stats_enabled if stats else settings.youtube_enabled):
        raise YouTubeError("YouTube не настроен")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            token = await _access_token(client, stats=stats)
            r = await client.get(_via_worker(_OAUTH + "tokeninfo"), headers=_headers(), params={"access_token": token})
    except httpx.HTTPError as e:
        raise YouTubeError(f"сеть: {type(e).__name__}: {e}") from e
    if r.status_code != 200:
        raise YouTubeError(f"tokeninfo {r.status_code}: {r.text[:200]}")
    return r.json().get("scope", "")
