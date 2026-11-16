"""Контрольные файлы для прослушивания словаря произношений: быстро проверить на слух всё непроверенное.

    docker compose run --rm -v "$PWD":/srv -w /srv contentops python scripts/pronounce_sheet.py
Берёт строки из assets/pronunciation.txt ниже черты «не проверено на слух» и записывает их по 15 штук в
data/media/pron/sheet_1.wav, sheet_2.wav… Перед каждым словом диктор называет номер. Рядом лежит
sheet_N.txt с тем же списком. Слушаешь, записываешь номера, которые прозвучали неправильно, и присылаешь:
править словарь будем только по ним.
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

MARKER = "не проверено на слух"
PER_SHEET = 15
GAP = 1.1       # пауза между словами, секунды
NUMBERS = "ноль один два три четыре пять шесть семь восемь девять десять одиннадцать двенадцать тринадцать четырнадцать пятнадцать".split()


def entries() -> list[tuple[str, str]]:
    """Пары (как написано, как читать) ниже черты."""
    out, seen = [], False
    for raw in settings.pronunciation_file.read_text(encoding="utf-8").splitlines():
        if MARKER in raw:
            seen = True
            continue
        line = raw.split("#", 1)[0].strip()
        if seen and "=" in line:
            src, dst = (x.strip() for x in line.split("=", 1))
            if src and dst:
                out.append((src, dst.rstrip("*")))
    return out


async def main() -> None:
    items = entries()
    outdir = settings.media_dir / "pron"
    outdir.mkdir(parents=True, exist_ok=True)
    gap = b"\x00\x00" * int(tts.SAMPLE_RATE * GAP)
    async with tts._ipv4_client(90) as client:
        for sheet, start in enumerate(range(0, len(items), PER_SHEET), 1):
            chunk = items[start:start + PER_SHEET]
            pcm, lines = bytearray(), []
            for i, (src, dst) in enumerate(chunk, 1):
                pcm += await tts._yandex_sentence(client, f"{NUMBERS[i]}.", settings.yandex_tts_speed) + b"\x00\x00" * 6000
                pcm += await tts._yandex_sentence(client, f"{dst}.", settings.yandex_tts_speed) + gap
                lines.append(f"{i:2d}. {src}  →  {dst}")
            wav = outdir / f"sheet_{sheet}.wav"
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(tts.SAMPLE_RATE)
                w.writeframes(bytes(pcm))
            (outdir / f"sheet_{sheet}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
            print(f"{wav.name}: слова {start + 1}–{start + len(chunk)}", flush=True)
    print(f"Всего слов: {len(items)}. Списки: data/media/pron/sheet_N.txt")


asyncio.run(main())
