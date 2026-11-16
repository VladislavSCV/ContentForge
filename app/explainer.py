"""Длинный ролик 5–7 минут (16:9): главы, заставки с датой, цифры, карточки «факт / не доказано», аналогии.

ПРОТОТИП-срез: сценарий из JSON → озвучка → вёрстка explainer.html → ffmpeg с музыкой под голосом.
Сценарий пишет человек (или модель в следующем срезе); формат см. assets/explainer_demo.json.
Запуск: python -m app.explainer сценарий.json папка_вывода [--fake] [--seconds N]
  --fake      без озвучки: тишина с расчётными таймкодами (проверка вёрстки без ключей и без денег)
  --seconds   срезать ролик до N секунд (быстрый просмотр)
"""

import asyncio
import json
import logging
import re
import shutil
import sys
import wave
from pathlib import Path

from . import music, sfx, tts
from .config import settings
from .video import RenderError, _chunks, _font_data_uri, _render_frames

log = logging.getLogger("explainer")

W, H, FPS = 1280, 720, 25
_TEMPLATE = Path(__file__).parent / "render" / "explainer.html"
# сколько секунд слова не хватает в тишине-заменителе: 0.12 с на слово + 0.05 с на букву
_FAKE_WORD, _FAKE_LETTER = 0.12, 0.05


def _fake_audio(text: str, path: Path) -> tuple[Path, list[tts.Word]]:
    """Тишина с расчётными таймкодами слов: вёрстку и сводку можно проверить без озвучки."""
    words, t = [], 0.4
    for tok in text.split():
        d = _FAKE_WORD + _FAKE_LETTER * len(tok)
        words.append(tts.Word(tok, t, t + d))
        t += d + (0.35 if tok[-1:] in ".!?" else 0.0)
    total = t + 1.0
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00" * int(44100 * total))
    return path, words


async def _real_audio(text: str, workdir: Path, chapters: list[str] | None = None) -> tuple[Path, list[tts.Word]]:
    if not settings.tts_enabled:
        raise RenderError(f"озвучка не настроена (TTS_PROVIDER={settings.tts_provider}: нет ключа); для проверки вёрстки --fake")
    if settings.tts_provider == "elevenlabs":
        return await tts._synthesize_elevenlabs(text, workdir)
    # не tts.synthesize: он ускоряет голос под потолок Shorts, а здесь ролик длинный
    # подача по предложениям (падения, паузы перед тезисом) только у длинного ролика; Shorts остаются как были
    plan = tts.plan_delivery(chapters) if chapters and settings.tts_dynamic else None
    return await tts._synthesize_yandex(text, workdir, settings.yandex_tts_speed, plan)


def _norm(tok: str) -> str:
    return re.sub(r"[^\w]", "", tok.lower().replace("ё", "е"))


def _visual_starts(vis: list[dict], words: list[tts.Word], s: float, e: float) -> list[float]:
    """Моменты появления сцен главы. Графика должна выходить на той фразе, о которой говорит диктор.

    Поле `on` у сцены — первые слова фразы из текста главы, на которой сцена появляется. Для цитат без `on`
    ищется их собственное начало. Не нашлось, нарушился порядок или фраза слишком близко к соседней —
    сцена встаёт на равную долю главы, как раньше. Первая сцена всегда с начала главы.
    """
    n = len(vis)
    toks = [_norm(w.text) for w in words]
    even = [s + (k / n) * (e - s) for k in range(n)]
    out: list[float] = []
    for k, v in enumerate(vis):
        if k == 0:
            out.append(s)
            continue
        cue = v.get("on") or (v.get("text") if v.get("type") == "quote" else "")
        key = [t for t in (_norm(x) for x in cue.split()) if t][:3]
        t0 = None
        for i in range(len(toks) - len(key) + 1) if key else []:
            if toks[i:i + len(key)] == key:
                t0 = words[i].start - 0.1
                break
        # порядок сцен должен сохраняться, и между ними нужно хотя бы 1,6 с: быстрее зритель не успевает прочитать
        if t0 is None or t0 < out[-1] + 1.6 or t0 > e - 1.2:
            t0 = max(even[k], out[-1] + 1.6)
        out.append(t0)
    # если из-за запаса хвост вылез за главу, равномерно прижимаем его назад
    if out and out[-1] > e - 1.2 and n > 1:
        out = [s + (k / n) * (e - s) for k in range(n)]
    return out


def build_config(script: dict, words: list[tts.Word], seconds: float | None = None) -> dict:
    """Главы привязываются к словам озвучки по числу слов в тексте главы; визуалы делят главу поровну."""
    chapters_in = script["chapters"]
    counts = [len(c["text"].split()) for c in chapters_in]
    total_tokens, n_words = sum(counts), len(words)
    if not n_words:
        raise RenderError("озвучка не дала слов")
    bounds, cum = [], 0
    for n in counts:
        # слов в озвучке может быть чуть иначе, чем в тексте (токенизация): тогда границы масштабируем пропорционально
        a = round(cum * n_words / total_tokens)
        cum += n
        b = round(cum * n_words / total_tokens)
        bounds.append((min(a, n_words - 1), max(min(a, n_words - 1) + 1, b)))
    duration = words[-1].end + 1.2
    chapters = []
    for i, (c, (a, b)) in enumerate(zip(chapters_in, bounds)):
        s = 0.0 if i == 0 else words[a].start - 0.15
        e = words[bounds[i + 1][0]].start - 0.15 if i + 1 < len(bounds) else duration
        vis = c.get("visuals") or []
        n = max(1, len(vis))
        starts = _visual_starts(vis, words[a:b], s, e)
        vs = []
        for k, v in enumerate(vis):
            end = starts[k + 1] if k + 1 < len(vis) else e
            vs.append({**v, "s": round(starts[k], 3), "e": round(end, 3)})
        chapters.append({"label": c.get("label", ""), "s": round(s, 3), "e": round(e, 3), "visuals": vs})
    config = {
        "duration": round(duration, 3), "title": script.get("title", ""), "brand": script.get("brand", ""),
        "chapters": chapters, "chunks": _chunks(words, max_words=6, max_chars=44, glue=True),
    }
    if seconds:
        config["duration"] = min(config["duration"], seconds)
    return config


def _sfx_track(config: dict, path: Path, voice_peak: float) -> Path:
    """Акценты по правилу «звук на значимые события, а не по таймеру» (гайды по саунд-дизайну для YouTube).

    Есть: взмах под шторку на смене главы, удар на раскрытии цифры и на плакате, тихие тики на карточках,
    узлах схемы и строках терминала. Нет: звука на каждой смене сцены, на иконках, столбцах и аналогиях.
    Вдобавок mix_cues каждый раз слегка меняет высоту и громкость, чтобы повтор не звучал копией.
    """
    cues: list[tuple] = []
    for ch in config["chapters"]:
        if ch["s"] > 0:
            cues.append((ch["s"] - 0.3, "whoosh"))   # пик взмаха (0,3 с от начала) попадает в середину шторки
        for v in ch["visuals"]:
            n = len(v.get("items", []))
            if v["type"] == "stat":
                cues.append((v["s"] + 1.4, "hit"))
            elif v["type"] == "poster":
                cues.append((v["s"] + 0.3, "hit", .6))
            elif v["type"] == "flow":
                cues += [(v["s"] + 0.25 + j * 0.55 + 0.1, "tick", .7) for j in range(n)]
            elif v["type"] in ("verdict", "timeline"):
                cues += [(v["s"] + 0.35 + j * 0.55 + 0.12, "tick", .7) for j in range(n)]
            elif v["type"] == "terminal":
                cues += [(v["s"] + 0.3 + j * 0.42, "tick", .45) for j in range(len(v.get("lines", [])))]
    return sfx.mix_cues(cues, config["duration"], voice_peak, path)


def _html(config: dict) -> str:
    font = settings.fonts_dir / "Inter.ttf"
    if not font.exists():
        raise RenderError(f"нет шрифта {font}")
    mono = settings.fonts_dir / "Mono.ttf"
    if not mono.exists():
        mono = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
    if not mono.exists():
        mono = font   # без моноширинного шрифта терминал выглядит хуже, но ролик собирается
    payload = json.dumps(config, ensure_ascii=False).replace("</", "<\\/")  # </script> в тексте не ломает страницу
    return (_TEMPLATE.read_text(encoding="utf-8").replace("__FONT_SANS__", _font_data_uri(font))
            .replace("__FONT_MONO__", _font_data_uri(mono)).replace("__CONFIG__", payload))


async def render(script: dict, workdir: Path, fake: bool = False, seconds: float | None = None) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    text = " ".join(c["text"] for c in script["chapters"])
    if fake:
        audio, words = _fake_audio(text, workdir / "voice.wav")
    else:
        audio, words = await _real_audio(text, workdir, [c["text"] for c in script["chapters"]])
    config = build_config(script, words, seconds)
    duration = config["duration"]
    (workdir / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8")

    frames = workdir / "frames"
    if frames.exists():
        shutil.rmtree(frames)
    frames.mkdir()
    log.info("кадры: %.0f с, %d шт", duration, int(duration * FPS))
    await _render_frames(_html(config), duration, frames, size=(W, H), fps=FPS, fmt="jpeg")

    cmd = [settings.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
           "-framerate", str(FPS), "-i", str(frames / "%06d.jpg"), "-i", str(audio.resolve())]
    # в режиме --fake голоса нет, и «пик» тишины сделал бы музыку неслышной: берём обычный уровень речи
    peak = 0.7 if fake else sfx.voice_peak(audio)
    fc_parts, labels, idx = ["[1:a]asplit=2[vo1][vo2]"], ["[vo1]"], 2
    if settings.music_enabled:
        mus = music.render(duration, workdir / "music.wav", peak, settings.music_level, settings.music_style)
        cmd += ["-i", str(mus.resolve())]
        # музыка приглушается на словах и поднимается на паузах
        fc_parts.append(f"[{idx}:a][vo2]sidechaincompress=threshold=0.03:ratio=5:attack=50:release=450[md]")
        labels.append("[md]")
        idx += 1
    if settings.sfx_enabled:
        accents = _sfx_track(config, workdir / "sfx.wav", peak)
        cmd += ["-i", str(accents.resolve())]
        labels.append(f"[{idx}:a]")
        idx += 1
    if len(labels) > 1:
        fc = (";".join(fc_parts) + ";" + "".join(labels) +
              f"amix=inputs={len(labels)}:duration=first:dropout_transition=0,volume={len(labels)},alimiter=limit=0.95,"
              # −15 LUFS: заметно тише, чем у роликов в ленте, и слушатель крутит громкость вверх; YouTube только занижает
              f"loudnorm=I=-15:TP=-1.5:LRA=9[a]")
        if not settings.music_enabled:  # [vo2] нужен только музыке
            fc = fc.replace("[1:a]asplit=2[vo1][vo2];", "[1:a]anull[vo1];", 1)
    else:
        fc = "[1:a]anull[a]"
    out = workdir / "explainer.mp4"
    cmd += ["-filter_complex", fc, "-map", "0:v", "-map", "[a]", "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    proc = await asyncio.create_subprocess_exec(*cmd, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RenderError(f"ffmpeg exit {proc.returncode}: {err.decode(errors='replace')[-700:]}")
    return out


def main(argv: list[str]) -> None:
    flags = [a for a in argv if a.startswith("--")]
    args = [a for a in argv if not a.startswith("--")]
    seconds = None
    for a in list(flags):
        if a.startswith("--seconds="):
            seconds = float(a.split("=", 1)[1])
    if len(args) < 2:
        raise SystemExit(__doc__)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    script = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    out = asyncio.run(render(script, Path(args[1]), fake="--fake" in flags, seconds=seconds))
    print("готово:", out)


if __name__ == "__main__":
    main(sys.argv[1:])
