"""Озвучить один и тот же текст разными голосами и прислать образцы в Telegram.

    docker compose run --rm contentops python scripts/voice_samples.py
Голоса синтезируются тем же кодом, что и ролики, — слышно ровно то, что будет в Shorts.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for k in ("TELEGRAM_BOT_TOKEN", "ADMIN_IDS"):
    os.environ.setdefault(k, "1")

import httpx  # noqa: E402

from app import tts  # noqa: E402
from app.config import settings  # noqa: E402

SCRIPT = (
    "Представь, что ты снимаешь квартиру на десять комнат, а живёшь только в одной. "
    "Так большинство команд платят за Kubernetes каждый месяц. "
    "Разработчики ставят requests с запасом, на всякий случай. "
    "Сначала сними реальное потребление за две недели, потом урежь requests по фактическим цифрам. "
    "Забирай готовые конфиги в нашем Telegram."
)

# мужские голоса SpeechKit v3 + несколько ролей; женский — для сравнения
VARIANTS = [
    ("anton", "good"), ("anton", "neutral"),
    ("alexander", "good"), ("alexander", "neutral"),
    ("kirill", "good"), ("kirill", "strict"),
    ("ermil", "good"), ("zahar", "good"), ("filipp", ""),
    ("lera", "friendly"),
]


async def main() -> None:
    outdir = settings.media_dir / "voices"
    outdir.mkdir(parents=True, exist_ok=True)
    base = f"{settings.telegram_api_url.rstrip('/')}/bot{settings.telegram_bot_token}"
    chat = settings.admin_ids.split(",")[0].strip()

    async with httpx.AsyncClient(timeout=120) as client:
        await client.post(f"{base}/sendMessage", data={
            "chat_id": chat,
            "text": "🎙 Образцы голосов на НОВОМ синтезе (меньше склеек, скорость 1.0).\n"
                    "Напиши, какой ставить — пропишу в настройки.",
        })
        for voice, role in VARIANTS:
            settings.yandex_tts_voice = voice
            settings.yandex_tts_role = role
            name = f"{voice}{'-' + role if role else ''}"
            try:
                wav, words = await tts._synthesize_yandex(SCRIPT, outdir, 1.0)
            except tts.TTSError as e:
                print(f"{name}: пропуск — {e}")
                continue
            ogg = outdir / f"{name}.ogg"
            try:
                subprocess.run([settings.ffmpeg_bin, "-y", "-loglevel", "error", "-i", str(wav),
                                "-c:a", "libopus", "-b:a", "64k", str(ogg)], check=True)
            except subprocess.CalledProcessError as e:  # один сбойный голос не должен ронять остальные
                print(f"{name}: не сконвертировался — {e}", flush=True)
                continue
            dur = words[-1].end if words else 0
            with ogg.open("rb") as f:
                r = await client.post(f"{base}/sendVoice", data={"chat_id": chat, "caption": name},
                                      files={"voice": (ogg.name, f, "audio/ogg")})
            print(f"{name}: {dur:.1f} c, отправка {r.status_code}", flush=True)


asyncio.run(main())
