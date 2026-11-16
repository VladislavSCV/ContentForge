"""Послушать варианты произношения слова и выбрать лучший — для словаря assets/pronunciation.txt.

    docker compose run --rm contentops python scripts/pronounce_test.py
        — стандартный набор: проверка знака «+» и частые технические слова
    docker compose run --rm contentops python scripts/pronounce_test.py "постгрес|постгр+эс|п+остгрес" "докер|д+окер"
        — свои группы: варианты одного слова через «|»

На каждую группу пишется data/media/pron/NN.wav: варианты по очереди, с паузой между ними, в том
порядке, в каком они перечислены. Голос и скорость — из настроек, то есть слышно ровно то, что
будет в роликах. Лучший вариант записывают в assets/pronunciation.txt: «слово = вариант».
"""

import asyncio
import os
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
for k in ("TELEGRAM_BOT_TOKEN", "ADMIN_IDS"):
    os.environ.setdefault(k, "1")

from app import tts  # noqa: E402
from app.config import settings  # noqa: E402

# Первая группа — контрольная: если «+» читается как «плюс» или ударение не меняется между
# вариантами, словарь с ударениями не поможет и надо менять только написание.
DEFAULT_GROUPS = [
    "замок|зам+ок|з+амок",
    "постгрес|постгр+эс|п+остгрес|постгр+ес",
    "кубернетес|куберн+етес|куберн+этис",
    "докер|д+окер|док+ер",
    "конкаррентли|конк+арэнтли|конкарр+ентли",
    "гитопс|гитоп+с|гит оп+с",
    "терраформ|терраф+орм|террафор+м",
    "прометеус|пром+етеус|промет+еус",
]
GAP_SEC = 0.9


async def main() -> None:
    args = sys.argv[1:]
    # --name=слово: писать в слово_1.wav, слово_2.wav…, не затирая 01.wav…, которые вы ещё не прослушали
    stem = next((a.split("=", 1)[1] for a in args if a.startswith("--name=")), "")
    groups = [a for a in args if not a.startswith("--name=")] or DEFAULT_GROUPS
    outdir = settings.media_dir / "pron"
    outdir.mkdir(parents=True, exist_ok=True)
    gap = b"\x00\x00" * int(tts.SAMPLE_RATE * GAP_SEC)
    async with tts._ipv4_client(90) as client:
        for n, group in enumerate(groups, 1):
            variants = [v.strip() for v in group.split("|") if v.strip()]
            pcm = bytearray()
            for v in variants:
                pcm += await tts._yandex_sentence(client, f"{v}.", settings.yandex_tts_speed) + gap
            out = outdir / (f"{stem}_{n}.wav" if stem else f"{n:02d}.wav")
            with wave.open(str(out), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(tts.SAMPLE_RATE)
                w.writeframes(bytes(pcm))
            print(f"{out.name}: " + "  →  ".join(f"({i}) {v}" for i, v in enumerate(variants, 1)), flush=True)


asyncio.run(main())
