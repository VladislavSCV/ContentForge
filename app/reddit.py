"""Reddit: бот ничего не публикует. Он приносит свежие обсуждения профсообществ и пишет заготовку ответа,
а отправляет автор руками. Причина: Reddit банит автопостинг и самопиар, а полезные ответы по существу
дают узнаваемость в r/devops, r/ExperiencedDevs, r/kubernetes.

Ленты Reddit отдают 429 при частых запросах, поэтому ходим по одной, с паузой. Недоступная лента пропускается.
"""

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel, Field

from . import topics

log = logging.getLogger(__name__)

SUBS = ("devops", "ExperiencedDevs", "kubernetes")
PAUSE = 3.0           # секунд между запросами к Reddit
PER_SUB = 2
MAX_AGE_HOURS = 36    # старые обсуждения уже разобраны: ответ в хвосте никто не увидит


class RedditReply(BaseModel):
    text: str = Field(description="Reply for the thread: 3-6 sentences, concrete, in English")


REPLY_PROMPT = """You help an infrastructure engineer write a reply to a Reddit thread. The goal is to be useful to the thread, not to promote the author.
Rules:
- 3 to 6 sentences, plain English, conversational. No greeting, no sign-off, no links, no mention of any channel or brand.
- Add one concrete thing the thread lacks: the mechanism behind the problem, a trade-off, or a threshold ("past N it stops working"). If the question is ambiguous, ask one clarifying question.
- Do NOT claim personal experience, numbers or incidents. Use only what is in the thread and general engineering knowledge. If a claim is uncertain, say so.
- No stock phrases ("great question", "let's dive in", "game-changer"), no lists with more than three items.
Return the result strictly following the JSON schema."""


async def fetch_threads(subs: tuple[str, ...] = SUBS, now: datetime | None = None) -> list[topics.Candidate]:
    """Лучшие за сутки обсуждения подписанных сообществ: по PER_SUB с каждого, новые вперёд."""
    now = now or datetime.now(timezone.utc)
    out: list[topics.Candidate] = []
    async with httpx.AsyncClient(headers=topics._HEADERS, follow_redirects=True, timeout=20) as client:
        for i, sub in enumerate(subs):
            if i:
                await asyncio.sleep(PAUSE)
            try:
                r = await client.get(f"https://www.reddit.com/r/{sub}/top/.rss?t=day")
                r.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("reddit r/%s недоступен: %s", sub, e)
                continue
            items = topics.parse_feed(r.content[:topics.MAX_FEED_BYTES], f"r/{sub}")
            fresh = [c for c in items if not c.published or (now - c.published).total_seconds() < MAX_AGE_HOURS * 3600]
            out += fresh[:PER_SUB]
    return out


async def draft_reply(thread: topics.Candidate) -> str:
    from . import ai   # поздний импорт, как в topic_rank: тесты без тяжёлых клиентов
    ask = f"Subreddit: {thread.source}\nThread title: {thread.title}\nThread text:\n{thread.summary or '(no text, title only)'}"
    res = await ai._generate(ask, RedditReply, system=REPLY_PROMPT, max_tokens=500)
    return res.text.strip()
