"""Озвучка сценария + таймкоды слов для субтитров.

TTS_PROVIDER=yandex (по умолчанию) — Yandex SpeechKit API v3, тот же API-ключ, что и для YandexGPT.
  SpeechKit не отдаёт таймкоды слов, поэтому синтезируем по предложениям: границы предложений
  известны точно, тишина по краям находится по амплитуде, а слова внутри раскладываются
  пропорционально длине. Рассинхрон — доли секунды, для субтитров незаметно.
TTS_PROVIDER=elevenlabs — ElevenLabs with-timestamps (точные таймкоды символов).
"""

import array
import asyncio
import base64
import json
import re
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import pronounce
from .config import settings


@dataclass
class Word:
    text: str
    start: float
    end: float


class TTSError(RuntimeError):
    pass


def _ipv4_client(timeout: float) -> httpx.AsyncClient:
    # у Яндекса есть AAAA-записи; в сетях без IPv6 (WSL, часть VPS) соединение иначе падает
    return httpx.AsyncClient(timeout=timeout, transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=2))


# ---------- Yandex SpeechKit ----------

SAMPLE_RATE = 48000
PAUSE_SEC = 0.12  # пауза между предложениями


MAX_CHUNK = 240  # SpeechKit v3 принимает ~250 символов за запрос


def _vlen(text: str) -> int:
    """Длина текста так, как он уйдёт в SpeechKit. Словарь произношений раздувает термины («mysql» → «майэскьюэль»,
    «+» перед ударением), и кусок в 226 знаков после подстановки бывал 392 при лимите ~250: «Too long text»."""
    return len(pronounce.apply(text))


def _split_long(sentence: str) -> list[str]:
    """Слишком длинное предложение режем по запятым/тире, в крайнем случае — по словам."""
    if _vlen(sentence) <= MAX_CHUNK:
        return [sentence]
    out, cur = [], ""
    for piece in re.split(r"(?<=[,;:—–])\s+", sentence):
        for word in (piece.split() if _vlen(piece) > MAX_CHUNK else [piece]):
            if cur and _vlen(cur) + _vlen(word) + 1 > MAX_CHUNK:
                out.append(cur)
                cur = word
            else:
                cur = f"{cur} {word}".strip()
    if cur:
        out.append(cur)
    return out


def _sentences(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"(?<=[.!?…])\s+", text) if p.strip()]


def _pack_chunks(text: str) -> list[list[str]]:
    """Набиваем предложения в запрос под завязку: чем меньше склеек, тем живее речь.

    Каждый запрос синтезируется со своей интонацией от нуля, поэтому отдельное предложение
    на запрос даёт «робота, читающего список». Внутри одного запроса SpeechKit связывает
    фразы сам. Возвращаем куски как списки предложений — они нужны для таймкодов.
    """
    chunks: list[list[str]] = []
    cur: list[str] = []
    cur_len = 0
    for sentence in _sentences(text):
        for piece in _split_long(sentence):
            n = _vlen(piece)
            if cur and cur_len + n + 1 > MAX_CHUNK:
                chunks.append(cur)
                cur, cur_len = [piece], n
            else:
                cur.append(piece)
                cur_len += n + 1
    if cur:
        chunks.append(cur)
    return chunks


def _speech_bounds(pcm: bytes, threshold: int = 600) -> tuple[float, float]:
    """Начало и конец речи (сек) внутри фрагмента — SpeechKit добавляет тишину по краям."""
    samples = array.array("h", pcm)
    n = len(samples)
    if not n:
        return 0.0, 0.0
    first = next((i for i in range(0, n, 48) if abs(samples[i]) > threshold), 0)
    last = next((i for i in range(n - 1, -1, -48) if abs(samples[i]) > threshold), n - 1)
    return first / SAMPLE_RATE, max(last, first) / SAMPLE_RATE


def _segment_bounds(pcm: bytes, parts: int, threshold: int = 600) -> list[tuple[float, float]]:
    """Границы речевых отрезков внутри фрагмента — ищем паузы, чтобы субтитры не уплывали.

    В один запрос уходит несколько предложений, и растягивать слова на весь фрагмент
    пропорционально нельзя: паузы между фразами сдвигают всё. Поэтому находим самые
    длинные паузы и режем по ним.
    """
    samples = array.array("h", pcm)
    step = SAMPLE_RATE // 100  # кадр 10 мс
    frames = [max(abs(samples[i]), abs(samples[min(i + step - 1, len(samples) - 1)]))
              for i in range(0, len(samples) - step, step)]
    if not frames:
        return [(0.0, 0.0)] * parts

    loud = [i for i, v in enumerate(frames) if v > threshold]
    if not loud:
        return [(0.0, len(samples) / SAMPLE_RATE)] * parts
    first, last = loud[0], loud[-1]
    if parts <= 1:
        return [(first / 100, (last + 1) / 100)]

    gaps: list[tuple[int, int, int]] = []  # (длина, начало, конец) пауз внутри речи
    run = 0
    for i in range(first, last + 1):
        if frames[i] <= threshold:
            run += 1
        elif run:
            gaps.append((run, i - run, i))
            run = 0
    gaps = [g for g in gaps if g[0] >= 12]  # пауза от 120 мс — граница фразы
    gaps.sort(reverse=True)
    cuts = sorted(((g[1] + g[2]) // 2) for g in gaps[: parts - 1])

    bounds, start = [], first
    for cut in cuts:
        bounds.append((start / 100, cut / 100))
        start = cut
    bounds.append((start / 100, (last + 1) / 100))
    while len(bounds) < parts:  # пауз нашлось меньше, чем предложений — добиваем нулевыми
        bounds.append((bounds[-1][1], bounds[-1][1]))
    return bounds


def _spread_words(sentence: str, start: float, end: float) -> list[Word]:
    tokens = sentence.split()
    weights = [len(re.sub(r"\W", "", t)) + 2 for t in tokens]  # +2: даже короткие слова звучат какое-то время
    total = sum(weights) or 1
    words, t = [], start
    for tok, w in zip(tokens, weights):
        d = (end - start) * w / total
        words.append(Word(tok, t, t + d))
        t += d
    return words


async def _yandex_sentence(client: httpx.AsyncClient, text: str, speed: float, pitch: int = 0) -> bytes:
    # SpeechKit API v3: новые голоса (anton, kirill, alexander…) заметно живее голосов v1
    hints: list[dict] = [{"voice": settings.yandex_tts_voice}, {"speed": f"{speed:.2f}"}]
    if pitch:
        hints.append({"pitchShift": str(pitch)})  # Гц; минус — ниже и весомее, плюс — выше, вопросительнее
    if settings.yandex_tts_role:
        hints.append({"role": settings.yandex_tts_role})
    r = await client.post(
        "https://tts.api.cloud.yandex.net/tts/v3/utteranceSynthesis",
        headers={"Authorization": f"Api-Key {settings.yandex_api_key}", "x-folder-id": settings.yandex_folder_id},
        json={
            # ударения и «как читать» подставляем только сюда: слова в таймкодах и субтитрах остаются исходными
            "text": pronounce.apply(text),
            "hints": hints,
            "outputAudioSpec": {"rawAudio": {"audioEncoding": "LINEAR16_PCM", "sampleRateHertz": str(SAMPLE_RATE)}},
            "loudnessNormalizationType": "LUFS",
        },
    )
    if r.status_code != 200:
        from .ai import yandex_hint  # здесь, чтобы озвучка не тянула модуль модели при импорте
        raise TTSError(f"SpeechKit {r.status_code}: {r.text[:300]}{yandex_hint(r.status_code)}")
    # ответ — JSON-строки с кусками аудио
    return b"".join(
        base64.b64decode(json.loads(line)["result"]["audioChunk"]["data"]) for line in r.text.splitlines() if line.strip()
    )


def plan_delivery(chapters: list[str]) -> list[tuple[str, dict]]:
    """Подача по предложениям: где чуть медленнее и ниже, где выше, где пауза перед ударом.

    Монотонность синтеза — это одинаковые скорость и высота на всём тексте. Живой диктор замедляется на
    коротких тезисах, понижает голос в конце мысли, повышает на вопросе и держит паузу перед поворотом.
    Правила грубые, на слух не подобраны: цифры стартовые, правятся по отзыву.
    """
    plan: list[tuple[str, dict]] = []
    for ci, text in enumerate(chapters):
        sents = _sentences(text)
        for i, s in enumerate(sents):
            n = len(s.split())
            p = {"speed": 1.04, "pitch": 0, "pause": 0.14}
            if i == 0 and ci == 0:  # только хук ролика; начало остальных глав сливается со следующими фразами: меньше склеек
                p.update(speed=0.92, pitch=-40, pause=0.35)
            if s.rstrip().endswith("?"):
                p.update(pitch=70, pause=0.35)
            elif n <= 6:
                p.update(speed=0.88, pitch=-50, pause=0.4)  # короткий тезис: вес
            if i == len(sents) - 1:  # конец мысли: падение и вдох перед следующей главой
                p.update(speed=0.9, pitch=-70, pause=0.75 if ci < len(chapters) - 1 else 1.0)
            plan.append((s, p))
    return plan


def _pack_styled(plan: list[tuple[str, dict]]) -> list[tuple[list[str], dict]]:
    """Соседние предложения с одинаковой подачей идут одним запросом (связная речь), разные — отдельными."""
    groups: list[tuple[list[str], dict]] = []
    for sentence, p in plan:
        for piece in _split_long(sentence):
            key = (p["speed"], p["pitch"])
            if groups and (groups[-1][1]["speed"], groups[-1][1]["pitch"]) == key \
                    and sum(_vlen(x) + 1 for x in groups[-1][0]) + _vlen(piece) <= MAX_CHUNK:
                groups[-1][0].append(piece)
                groups[-1][1]["pause"] = p["pause"]
            else:
                groups.append(([piece], dict(p)))
    return groups


async def _synthesize_yandex(text: str, workdir: Path, speed: float, plan: list[tuple[str, dict]] | None = None) -> tuple[Path, list[Word]]:
    # без плана — прежнее поведение (Shorts): одна подача на весь текст
    styled = _pack_styled(plan) if plan else [(g, {"speed": 1.0, "pitch": 0, "pause": PAUSE_SEC}) for g in _pack_chunks(text)]
    if not styled:
        raise TTSError("пустой текст для озвучки")
    groups = [g for g, _ in styled]
    sem = asyncio.Semaphore(4)
    async with _ipv4_client(90) as client:

        async def one(sentences: list[str], p: dict) -> bytes:
            async with sem:
                # связь с Яндексом из этой сети рвётся урывками: два повтора дешевле, чем потерять весь ролик
                for attempt in range(3):
                    try:
                        return await _yandex_sentence(client, " ".join(sentences), min(3.0, speed * p["speed"]), p["pitch"])
                    except httpx.HTTPError:
                        if attempt == 2:
                            raise
                        await asyncio.sleep(2 * (attempt + 1))

        try:
            chunks = await asyncio.gather(*(one(g, p) for g, p in styled))
        except httpx.HTTPError as e:
            raise TTSError(f"SpeechKit: сеть: {e}") from e

    pcm, words, offset = bytearray(), [], 0.0
    for (sentences, p), chunk in zip(styled, chunks):
        pause = b"\x00\x00" * int(SAMPLE_RATE * p["pause"])
        for sentence, (s0, s1) in zip(sentences, _segment_bounds(chunk, len(sentences))):
            words += _spread_words(sentence, offset + s0, offset + s1)
        pcm += chunk + pause
        offset += (len(chunk) + len(pause)) / 2 / SAMPLE_RATE

    out = workdir / "voice.wav"
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(pcm))
    return out, words


# ---------- ElevenLabs ----------

def _words_from_alignment(al: dict) -> list[Word]:
    chars = al["characters"]
    starts = al["character_start_times_seconds"]
    ends = al["character_end_times_seconds"]
    words: list[Word] = []
    buf, w_start, w_end = "", 0.0, 0.0
    for ch, s, e in zip(chars, starts, ends):
        if ch.isspace():
            if buf:
                words.append(Word(buf, w_start, w_end))
                buf = ""
            continue
        if not buf:
            w_start = s
        buf += ch
        w_end = e
    if buf:
        words.append(Word(buf, w_start, w_end))
    return words


async def _synthesize_elevenlabs(text: str, workdir: Path) -> tuple[Path, list[Word]]:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{settings.elevenlabs_voice_id}/with-timestamps"
    payload = {
        "text": text,
        "model_id": settings.elevenlabs_model,
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.3, "use_speaker_boost": True},
    }
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(
            url,
            params={"output_format": "mp3_44100_128"},
            headers={"xi-api-key": settings.elevenlabs_api_key},
            json=payload,
        )
    if r.status_code != 200:
        raise TTSError(f"ElevenLabs {r.status_code}: {r.text[:300]}")
    data = r.json()
    out = workdir / "voice.mp3"
    out.write_bytes(base64.b64decode(data["audio_base64"]))
    # alignment — по исходному тексту (для субтитров это то, что нужно), normalized — запасной вариант
    alignment = data.get("alignment") or data.get("normalized_alignment")
    if not alignment:
        raise TTSError("ElevenLabs не вернул alignment")
    return out, _words_from_alignment(alignment)


MAX_SHORTS_SEC = 40.0
MAX_SPEED = 1.4  # быстрее звучит уже скороговоркой


async def synthesize(text: str, workdir: Path) -> tuple[Path, list[Word]]:
    """Озвучивает текст в workdir, возвращает путь к аудио и слова с таймкодами."""
    if not settings.tts_enabled:
        raise TTSError(f"TTS не настроен (TTS_PROVIDER={settings.tts_provider}: нет ключа)")
    if settings.tts_provider == "elevenlabs":
        return await _synthesize_elevenlabs(text, workdir)
    speed = settings.yandex_tts_speed
    audio, words = await _synthesize_yandex(text, workdir, speed)
    duration = words[-1].end if words else 0.0
    if duration > MAX_SHORTS_SEC and speed < MAX_SPEED:
        # текст всё же длинноват — перезаписываем чуть быстрее, чтобы уложиться в Shorts на 30–40 секунд
        faster = min(MAX_SPEED, speed * duration / (MAX_SHORTS_SEC - 2))
        audio, words = await _synthesize_yandex(text, workdir, faster)
    return audio, words
