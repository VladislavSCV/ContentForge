"""Проверить генерацию без Telegram:

    python scripts/try_llm.py [файл_с_постом.txt]            # только тексты
    python scripts/try_llm.py [файл_с_постом.txt] --video    # + озвучка и ролик в data/media/try/
    docker compose run --rm contentops python scripts/try_llm.py --video   # то же в Docker (там есть ffmpeg)

Берёт настройки из .env, печатает итоговый Telegram-пост и сценарий Shorts.
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for k in ("TELEGRAM_BOT_TOKEN", "ADMIN_IDS"):
    os.environ.setdefault(k, "1")

from app import ai  # noqa: E402
from app.config import settings  # noqa: E402
from app.pipeline import clean_voice_script, make_video  # noqa: E402
from app.tg_format import build_post, visible_length  # noqa: E402

SAMPLE = """We cut our Kubernetes bill by 35% in two weeks. No magic, just data.
Most teams set CPU requests "just in case" - we found pods requesting 2 cores and using 120m.
What we did:
1. Pulled 14 days of usage from Prometheus: sum(rate(container_cpu_usage_seconds_total[5m])) by (pod)
2. Ran Robusta KRR to get recommendations per workload
3. Rolled out new requests via GitOps, 10% of services per day
4. Kept limits only for memory, removed CPU limits to avoid throttling
Result: 40 nodes -> 26 nodes, p99 latency unchanged. FinOps is an engineering problem, not a finance one."""


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
for noisy in ("httpx", "httpcore"):   # их INFO печатает адрес с ключом Pixabay
    logging.getLogger(noisy).setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    text = Path(args[0]).read_text(encoding="utf-8") if args else SAMPLE
    print(f"LLM: {settings.llm_provider} / {settings.llm_model}\n")
    t0 = time.monotonic()
    pkg = await ai.generate_package(text)
    html = build_post(pkg.telegram_stream)
    s = pkg.youtube_shorts_stream
    voice = clean_voice_script(pkg)
    print(f"=== Telegram ({visible_length(html)} символов) ===\n{html}\n")
    print(f"=== Shorts ===\nЗаголовок: {s.youtube_title}\nТеги: {s.youtube_tags}\nОписание: {s.youtube_description}")
    print(f"Комментарий: {s.first_comment}\nДиктор ({len(voice.split())} слов): {voice}")
    print(f"\n[текст: {time.monotonic() - t0:.0f} c]")

    if "--video" in sys.argv:
        workdir = settings.media_dir / "try"
        workdir.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        out = await make_video(text, pkg, workdir, progress=lambda m: asyncio.sleep(0, print(m)))
        print(f"[видео: {time.monotonic() - t0:.0f} c] → {out}")


asyncio.run(main())
