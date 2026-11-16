"""Проверка стоковой выборки Pixabay отдельно от всего конвейера: что находит и что отсеивает фильтр.

    docker compose run --rm -v "$PWD":/srv -w /srv contentops python scripts/stock_check.py ["запрос 1" "запрос 2" …]
Без аргументов берёт запросы, типичные для абстрактных платформенных тем. Клипы скачиваются в data/media/stockcheck.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for k in ("TELEGRAM_BOT_TOKEN", "ADMIN_IDS"):
    os.environ.setdefault(k, "1")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "httpcore"):   # их INFO печатает адрес с ключом Pixabay
    logging.getLogger(noisy).setLevel(logging.WARNING)

from app import stock  # noqa: E402
from app.config import settings  # noqa: E402

queries = sys.argv[1:] or ["software platform dashboard", "metrics dashboard", "dark server room",
                           "code on screen", "data center night", "devops pipeline"]
out = settings.media_dir / "stockcheck"
out.mkdir(parents=True, exist_ok=True)
print("ключ Pixabay задан:", bool(settings.pixabay_api_key), "· через Worker:", bool(settings.telegram_api_url))
for q in queries:
    clips = asyncio.run(stock.fetch_clips([q], out))
    print(f"{q!r}: найдено клипов {len(clips)}")
