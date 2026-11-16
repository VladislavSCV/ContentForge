"""Сравнение подбора тем: прежний общий топ-3 против квоты по столпам. Только чтение лент, без модели и базы.

    python scripts/topics_compare.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app import topics  # noqa: E402


async def main() -> None:
    urls = topics.load_sources()
    async with httpx.AsyncClient(headers=topics._HEADERS, follow_redirects=True, timeout=20) as client:
        batches = await asyncio.gather(*(topics._fetch_feed(client, u) for u in urls))
    cands = [c for b in batches for c in b]
    print(f"лент: {len(urls)}, записей: {len(cands)}")
    ok = sorted({c.source for c in cands})
    print("ответили:", ", ".join(ok))
    for title, by_pillar in (("ПРЕЖНИЙ топ-3 (общий)", False), ("НОВЫЙ топ-3 (по столпам)", True)):
        print(f"\n{title}")
        for c in topics.pick(cands, set(), 3, by_pillar=by_pillar):
            print(f"  [{topics.PILLARS.get(c.pillar, '?'):22}] {c.score:4.1f} {c.source}: {c.title[:90]}")
    counts: dict[str, int] = {}
    for c in cands:
        counts[topics.pillar_of(c)] = counts.get(topics.pillar_of(c), 0) + 1
    print("\nзаписей по столпам:", {topics.PILLARS.get(k, k): v for k, v in counts.items()})


asyncio.run(main())
