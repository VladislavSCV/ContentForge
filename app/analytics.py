"""Итоги недели: что вышло, что пошло на проверку и как отработал LinkedIn.

Что видно и чего нет:
- Своя кухня (сколько постов создано, опубликовано, отклонено, на чём чаще ловит автопроверка) — из базы.
- LinkedIn: у сервиса нет доступа к аналитике (она за логином), поэтому автор присылает боту выгрузку
  «Post performance» (.xlsx) — бот разбирает её и сравнивает постовые показатели.
- Telegram: Bot API не отдаёт просмотры постов канала, поэтому цифр по каналу здесь нет.
- YouTube: нужен отдельный токен с правом чтения статистики; текущий умеет только загружать.
"""

import io
import re
import statistics
import urllib.parse
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

from . import db, pipeline

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_HEADERS = {  # заголовок колонки в выгрузке LinkedIn → поле таблицы
    "Post URL": "url", "Post Publish Date": "published", "Impressions": "impressions", "Members reached": "reached",
    "Followers gained": "followers", "Profile viewers": "profile_views", "Reactions": "reactions",
    "Comments": "comments", "Reposts": "reposts", "Saves": "saves", "Sends": "sends",
}
MAX_XLSX = 5_000_000


def _cells(row: ET.Element, strings: list[str]) -> dict[str, str]:
    """Строка листа: буква колонки → значение."""
    out = {}
    for c in row:
        col = re.match(r"[A-Z]+", c.get("r", "")).group(0)
        v = c.find(_NS + "v")
        if c.get("t") == "s" and v is not None:
            out[col] = strings[int(v.text)]
        elif c.get("t") == "inlineStr":
            out[col] = "".join(t.text or "" for t in c.iter(_NS + "t"))
        else:
            out[col] = v.text if v is not None else ""
    return out


def parse_linkedin_xlsx(data: bytes) -> list[dict]:
    """Выгрузка «Post performance». Пустой список, если файл не похож на неё."""
    if len(data) > MAX_XLSX:
        return []
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            strings = ["".join(t.text or "" for t in si.iter(_NS + "t"))
                       for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(_NS + "si")]
        rows = list(ET.fromstring(z.read("xl/worksheets/sheet1.xml")).iter(_NS + "row"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        return []
    if not rows:
        return []
    head = _cells(rows[0], strings)
    cols = {col: _HEADERS[name] for col, name in head.items() if name in _HEADERS}
    if "url" not in cols.values() or "impressions" not in cols.values():
        return []
    out = []
    for row in rows[1:]:
        cells = _cells(row, strings)
        r = {field: cells.get(col, "") for col, field in cols.items()}
        try:
            r["published"] = datetime.strptime(r["published"], "%m/%d/%Y").strftime("%Y-%m-%d")
            for k in ("impressions", "reached", "followers", "profile_views", "reactions", "comments",
                      "reposts", "saves", "sends"):
                r[k] = int(float(r.get(k) or 0))
        except (KeyError, ValueError):
            continue
        out.append(r)
    return out


def slug_of(url: str) -> str:
    """Начало поста из адреса: LinkedIn кладёт его в слаг («…_парадокс-2026-года-команды-пишут-код-в-4-share-…»)."""
    m = re.search(r"/posts/[^_]+_(.+?)-share-\d+", urllib.parse.unquote(url))
    return m.group(1).replace("-", " ") if m else ""


def title_of(url: str) -> str:
    return (slug_of(url) or url)[:70]


def _rate(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "0%"


async def youtube_rows(posts: list[db.Post], days: int = 30) -> tuple[list[dict], str]:
    """Статистика роликов за последние дни: просмотры, лайки, комментарии, удержание. Вторым — причина, если не вышло."""
    from . import youtube  # здесь, чтобы анализ LinkedIn не зависел от настроек YouTube

    border = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    vids = [p for p in posts if p.youtube_video_id and p.updated_at >= border]
    if not vids:
        return [], ""
    try:
        stats = await youtube.video_stats([p.youtube_video_id for p in vids])
    except youtube.YouTubeError as e:
        return [], str(e)
    start = min(p.created_at for p in vids)[:10]
    ret = await youtube.retention([p.youtube_video_id for p in vids], start, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    rows = []
    for p in vids:
        s = stats.get(p.youtube_video_id)
        if s:
            title = p.package.youtube_shorts_stream.youtube_title.split("#")[0].strip() if p.package else p.youtube_video_id
            rows.append({"id": p.youtube_video_id, "title": title[:60], **s, **ret.get(p.youtube_video_id, {})})
    return rows, ""


def _per_mille(part: int, whole: int) -> str:
    return f"{1000 * part / whole:.0f}" if whole else "–"


def build_post_table(posts: list[db.Post], li: list[dict], yt: list[dict], tg: dict[int, dict], limit: int = 15) -> str:
    """Один пост — одна строка: что вышло и сколько получило на каждой площадке.

    Главное для роста: пересылки (Telegram), сохранения и отправки (LinkedIn), досмотр (YouTube). Лайки не считаем целью.
    Позиция автора — единственная «формула», которую база помнит про пост; по ней видно, помогает ли своя позиция охвату.
    """
    from . import learning  # здесь, чтобы не тянуть тематику в импорт аналитики

    published = [p for p in posts if p.status == db.PUBLISHED][:limit]
    if not published:
        return "Опубликованных постов пока нет."
    linked = learning.match_linkedin(published, li)
    yt_by_id = {r["id"]: r for r in yt if "id" in r}
    lines = ["📈 Пост → охват, пересылки, сохранения", ""]
    for p in published:
        title = (p.package.youtube_shorts_stream.youtube_title.split("#")[0].strip() if p.package else p.source_text[:50])[:45]
        parts = [f"#{p.id} {title}", "  позиция: " + ("да" if p.opinion else "нет")]
        t = tg.get(p.id)
        parts.append(f"  Telegram: просм. {t['views']}, пересл. {t['forwards']} ({_per_mille(t['forwards'], t['views'])}‰)"
                     if t else "  Telegram: нет цифр (/tgstats)")
        row = linked.get(p.id)
        parts.append(f"  LinkedIn: показов {row['impressions']}, сохр. {row['saves']}, отпр. {row['sends']}, "
                     f"коммент. {row['comments']}" if row else "  LinkedIn: нет в выгрузке")
        y = yt_by_id.get(p.youtube_video_id)
        parts.append(f"  YouTube: просм. {y['views']}" + (f", досмотр {y['avg_pct']:.0f}%" if "avg_pct" in y else "")
                     if y else "  YouTube: нет данных")
        lines += parts + [""]
    lines.append("Пересылки на 1000 просмотров (‰) показывают, делятся ли постом. Выводы — после 20 постов с цифрами.")
    return "\n".join(lines)


def build_digest(posts: list[db.Post], li: list[dict], now: datetime | None = None, days: int = 7,
                 yt: list[dict] | None = None, yt_error: str = "", learned: str = "") -> str:
    now = now or datetime.now(timezone.utc)
    border = (now - timedelta(days=days)).isoformat(timespec="seconds")
    week = [p for p in posts if p.created_at >= border]
    lines = [f"📊 Итоги недели ({(now - timedelta(days=days)).strftime('%d.%m')}–{now.strftime('%d.%m')})", ""]

    # --- своя кухня ---
    published = [p for p in week if p.status == db.PUBLISHED]
    queued = [p for p in posts if p.status == db.SCHEDULED]
    dropped = [p for p in week if p.status == db.FAILED]
    waiting = [p for p in week if p.status == db.REVIEW]
    shorts = [p for p in published if p.youtube_video_id]
    lines += ["Выпуск:",
              f"• создано постов: {len(week)}, опубликовано: {len(published)} (YouTube: {len(shorts)}), "
              f"отклонено или упало: {len(dropped)}",
              f"• ждёт вашей проверки: {len(waiting)}, в очереди: {len(queued)}"]
    problems = Counter()
    for p in week:
        if p.package and p.status in (db.REVIEW, db.SCHEDULED, db.PUBLISHED):
            try:
                problems.update(re.sub(r"[:\d].*", "", i.text).strip() for i in pipeline.quality_report(p))
            except Exception:  # noqa: BLE001 — отчёт важнее одной сбойной записи
                continue
    if problems:
        lines.append("• чаще всего ловила автопроверка: " + "; ".join(f"{t} ({n})" for t, n in problems.most_common(3)))
    month_border = (now - timedelta(days=30)).isoformat(timespec="seconds")
    month = [p for p in posts if p.status == db.PUBLISHED and p.package and p.updated_at >= month_border]
    habr = "хватает для статьи" if len(month) >= 2 else "для статьи нужно ещё " + str(2 - len(month))
    lines.append(f"• Хабр: статья соберётся сама 1-го числа в 09:00; опубликованных постов за 30 дней: {len(month)} ({habr})")
    lines.append("")

    # --- LinkedIn ---
    if not li:
        lines += ["LinkedIn: данных нет. Открой Analytics → Post performance → Export и пришли файл .xlsx сюда.", ""]
    else:
        last_import = max((r.get("imported_at") or "" for r in li), default="")
        cut = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        recent = [r for r in li if r["published"] >= cut]
        lines.append(f"LinkedIn (постов в базе: {len(li)}):")
        if recent:
            imp = sum(r["impressions"] for r in recent)
            lines.append(f"• за неделю постов: {len(recent)}, показов: {imp}, реакций: "
                         f"{sum(r['reactions'] for r in recent)} ({_rate(sum(r['reactions'] for r in recent), imp)}), "
                         f"подписчиков: +{sum(r['followers'] for r in recent)}")
        else:
            lines.append("• за неделю постов в выгрузке нет (или выгрузка старая)")
        med = statistics.median(r["impressions"] for r in li)
        top = sorted(li, key=lambda r: r["impressions"], reverse=True)[:3]
        lines.append(f"• медиана показов за всё время: {int(med)}. Лучшие:")
        lines += [f"   {r['impressions']} · реакций {_rate(r['reactions'], r['impressions'])} · {title_of(r['url'])}" for r in top]
        worst = min(li, key=lambda r: r["impressions"])
        lines.append(f"• слабее всех: {worst['impressions']} · {title_of(worst['url'])}")
        if last_import and last_import < border:
            lines.append("• выгрузка старше недели — пришли свежую, цифры могли измениться")
        lines.append("")

    # --- YouTube ---
    if yt:
        lines.append(f"YouTube (ролики за 30 дней: {len(yt)}):")
        lines.append(f"• просмотров: {sum(r['views'] for r in yt)}, лайков: {sum(r['likes'] for r in yt)}, "
                     f"комментариев: {sum(r['comments'] for r in yt)}")
        with_ret = [r for r in yt if "avg_pct" in r]
        if with_ret:
            lines.append(f"• средний процент досмотра: {sum(r['avg_pct'] for r in with_ret) / len(with_ret):.0f}%, "
                         f"новых подписчиков: +{sum(r['subs'] for r in with_ret)}")
        for r in sorted(yt, key=lambda r: r["views"], reverse=True)[:3]:
            extra = f" · досмотр {r['avg_pct']:.0f}%" if "avg_pct" in r else ""
            lines.append(f"   {r['views']} просм. · лайков {r['likes']}{extra} · {r['title']}")
        lines.append("")
    elif yt_error:
        lines += [f"YouTube: статистику получить не удалось ({yt_error[:150]})", ""]
    elif yt is None:
        lines += ["YouTube: статистика не подключена (нужен YOUTUBE_STATS_REFRESH_TOKEN).", ""]

    if learned:
        lines += [learned, ""]
    lines += ["Нет данных: просмотры Telegram-канала (Bot API их не отдаёт)."]
    return "\n".join(lines)
