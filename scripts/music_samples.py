"""Образцы фоновой музыки всех стилей для прослушивания: data/media/music_<стиль>.wav (30 секунд, громче рабочего уровня).

Запуск: docker compose run --rm -v "$PWD":/srv -w /srv contentops python scripts/music_samples.py
Выбранный стиль — MUSIC_STYLE в .env (calm, tense, drive); громкость — MUSIC_LEVEL (доля от пика голоса).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import music  # noqa: E402

out = Path("data/media")
out.mkdir(parents=True, exist_ok=True)
for style in music.STYLES:
    p = music.render(30, out / f"music_{style}.wav", 0.7, 0.5, style)
    print("готово:", p)
