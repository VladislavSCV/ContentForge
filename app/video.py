"""Shorts Factory: кадры вёрстки (headless Chromium) + фон + озвучка ➔ shorts_render.mp4 (1080x1920).

Вместо ASS-субтитров сцены рисует настоящий браузер: композиция render/composition.html
получает JSON со сценарием и функцию seek(t), Playwright снимает каждый кадр с прозрачным
фоном, ffmpeg подкладывает стоковое видео и сводит со звуком. Это даёт нормальную
типографику, тени, пружинные анимации и «печатающийся» терминал — чего ASS не умеет.
"""

import asyncio
import base64
import json
import logging
import random
import shutil
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import cta, mascot, music, sfx
from .config import settings
from .schemas import ScenePlan
from .tts import Word

log = logging.getLogger(__name__)

W, H, FPS = 1080, 1920, 30
_VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm"}
_COMPOSITION = Path(__file__).parent / "render" / "composition.html"
_MONO_FALLBACK = Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")


class RenderError(RuntimeError):
    pass


@dataclass
class Scenes:
    hook: tuple[float, float]
    problem: tuple[float, float]
    solution: tuple[float, float]
    takeaway: tuple[float, float]
    cta: tuple[float, float]


def split_scenes(words: list[Word], seg_words: list[int] | None = None) -> Scenes:
    """Границы сцен.

    Если известно, сколько слов в каждой смысловой части (хук, проблема, решение, главная мысль,
    призыв), режем ровно по ним: сцена меняется в момент, когда диктор переходит к следующей мысли.
    Иначе — эвристика: хук — первое предложение, призыв — последнее, середина делится 40/60.
    """
    end = words[-1].end + 0.5
    if seg_words and len(seg_words) == 5 and sum(seg_words) == len(words) and all(seg_words):
        starts, i = [0.0], 0
        for n in seg_words[:-1]:
            i += n
            starts.append(words[i].start)
        b = starts + [end]
        return Scenes((b[0], b[1]), (b[1], b[2]), (b[2], b[3]), (b[3], b[4]), (b[4], b[5]))
    if seg_words:
        log.warning("seg_words %s не сходятся с %d словами озвучки — монтаж по эвристике", seg_words, len(words))
    sentence_ends = [i for i, w in enumerate(words) if re.search(r"[.!?…]$", w.text)]
    hook_end = words[sentence_ends[0]].end if sentence_ends else end * 0.12
    hook_end = min(max(hook_end, 2.5), end * 0.3)
    cta_start = words[sentence_ends[-2] + 1].start if len(sentence_ends) >= 2 else end * 0.82
    if not end * 0.6 <= cta_start <= end - 2.0:
        cta_start = end * 0.82
    mid = hook_end + (cta_start - hook_end) * 0.4
    return Scenes((0.0, hook_end), (hook_end, mid), (mid, cta_start), (cta_start, cta_start), (cta_start, end))


def _step_times(words: list[Word], window: tuple[float, float], n_steps: int) -> list[float]:
    """Когда показывать шаги: в момент, когда диктор начинает очередное предложение решения.

    Равномерная раскладка по времени расходилась с голосом: шаг мог появиться раньше, чем о нём
    заговорили, или позже, чем перешли к следующему. Пустой список — пусть вёрстка раскладывает сама.
    """
    beats, new_sentence = [], True
    for w in words:
        if window[0] - 0.01 <= w.start < window[1] and new_sentence:
            beats.append(w.start)
        new_sentence = bool(re.search(r"[.!?…]$", w.text))
    if n_steps < 1 or len(beats) < n_steps:
        return []
    pick = [beats[round(i * (len(beats) - 1) / (n_steps - 1))] for i in range(n_steps)] if n_steps > 1 else [beats[0]]
    return [round(t, 3) for t in pick] if pick == sorted(set(pick)) else []


# слова, которые в субтитрах красятся акцентом: цифры, единицы измерения, латиница (термины), крайности
_KEY = re.compile(r"\d|[A-Za-z]{3}|процент|минут|секунд|тысяч|миллион|миллиард|долл|рубл|гигабайт|мегабайт|"
                  r"^(?:никогда|всегда|нельзя|ни)\b", re.I)


_DANGLING = {"в", "и", "с", "к", "о", "у", "а", "но", "на", "по", "за", "из", "от", "до", "для", "что", "это", "как", "не", "же", "ли", "то"}


def _chunks(words: list[Word], max_words: int = 3, max_chars: int = 22, glue: bool = False) -> list[dict]:
    """Субтитры по 1–3 слова: короткие строки читаются с телефона на скорости.

    glue: строка не заканчивается предлогом или союзом («…компании, в»): он уходит в начало следующей.
    """
    out: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur and (len(cur) >= max_words or sum(len(x.text) + 1 for x in cur) + len(w.text) > max_chars):
            tail = []
            if glue and len(cur) > 1 and re.sub(r"\W", "", cur[-1].text).lower() in _DANGLING:
                tail = [cur.pop()]
            out.append(cur)
            cur = tail
        cur.append(w)
        if re.search(r"[.!?…:;]$", w.text):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)

    chunks = []
    for i, group in enumerate(out):
        next_start = out[i + 1][0].start if i + 1 < len(out) else group[-1].end + 0.5
        chunks.append({
            "s": round(group[0].start, 3),
            "e": round(min(next_start, group[-1].end + 0.55), 3),
            "w": [{"t": x.text, "s": round(x.start, 3), "e": round(x.end, 3), "k": int(bool(_KEY.search(x.text)))}
                  for x in group],
        })
    return chunks


def _font_data_uri(path: Path) -> str:
    return "data:font/ttf;base64," + base64.b64encode(path.read_bytes()).decode()


def _fonts() -> tuple[str, str]:
    sans = settings.fonts_dir / "Inter.ttf"
    if not sans.exists():
        raise RenderError(f"нет шрифта {sans} — положи Inter.ttf в assets/fonts")
    mono = settings.fonts_dir / "Mono.ttf"
    if not mono.exists():
        mono = _MONO_FALLBACK
    if not mono.exists():
        mono = sans
    return _font_data_uri(sans), _font_data_uri(mono)


def _code_title(lang: str) -> str:
    lang = (lang or "").lower()
    return {
        "promql": "prometheus", "dockerfile": "Dockerfile", "bash": "bash", "sh": "bash", "shell": "bash",
        "yaml": "values.yaml", "yml": "values.yaml", "go": "main.go",
        "python": "main.py", "py": "main.py", "sql": "query.sql", "json": "config.json",
    }.get(lang, lang or "terminal")


def _wrap_code(code: str, max_cols: int = 34, max_lines: int = 9) -> str:
    """Переносим длинные строки сами, с отступом продолжения.

    Браузер переносит код по ширине блока и рвёт выражение в случайном месте, а ужать кегль
    до читаемого нельзя — строки в PromQL/YAML слишком длинные. Поэтому ломаем по пробелам.
    """
    out: list[str] = []
    for raw in (code or "").strip().expandtabs(2).splitlines():
        line = raw.rstrip()
        if len(line) <= max_cols:
            out.append(line)
            continue
        indent = " " * (len(line) - len(line.lstrip()) + 2)
        cur = ""
        for word in line.split(" "):
            candidate = f"{cur} {word}".strip() if cur else word
            if cur and len(candidate) > max_cols:
                out.append(cur)
                cur = indent + word
            else:
                cur = candidate
        if cur:
            out.append(cur)
    if len(out) > max_lines:
        out = out[:max_lines - 1] + ["…"]
    return "\n".join(out)


def _mascot_config(words: list[Word], sc: Scenes) -> dict | None:
    poses = mascot.load()
    if not poses:
        return None
    scenes = {"hook": sc.hook, "problem": sc.problem, "solution": sc.solution,
              "takeaway": sc.takeaway, "cta": sc.cta}
    return {"poses": poses, "timeline": mascot.timeline(words, scenes, set(poses))}


def build_config(words: list[Word], plan: ScenePlan, title: str, code: str, code_lang: str,
                 cta_mode: str | None = None, segment_words: list[int] | None = None,
                 takeaway: str = "") -> dict:
    sc = split_scenes(words, segment_words)
    if not takeaway or sc.takeaway[1] - sc.takeaway[0] < 0.5:
        # без главной мысли отдельный экран не нужен — его время отдаём решению
        takeaway = ""
        sc.solution = (sc.solution[0], sc.takeaway[1] if sc.takeaway[1] > sc.solution[1] else sc.solution[1])
        sc.takeaway = (sc.cta[0], sc.cta[0])
    code = _wrap_code(code)

    steps = list(plan.steps)
    if not steps and not code:  # сцене решения нечего показать — пусть держит заголовок темы
        steps = [title] if title else []
    flow = [{"label": l, "icon": i} for l, i in zip(plan.flow, plan.flow_icons)] if 3 <= len(plan.flow) <= 4 else []
    if not (plan.problem_before or plan.problem_after or flow):
        sc.problem = (sc.problem[0], sc.problem[0])  # пустую сцену проблемы отдаём решению
        sc.solution = (sc.problem[0], sc.solution[1])

    return {
        "duration": round(sc.cta[1], 3),
        "brand": {"name": settings.brand_name},
        "scenes": {
            "hook": [round(sc.hook[0], 3), round(sc.hook[1], 3)],
            "problem": [round(sc.problem[0], 3), round(sc.problem[1], 3)],
            "solution": [round(sc.solution[0], 3), round(sc.solution[1], 3)],
            "takeaway": [round(sc.takeaway[0], 3), round(sc.takeaway[1], 3)],
            "cta": [round(sc.cta[0], 3), round(sc.cta[1], 3)],
        },
        "takeaway": takeaway,
        "mascot": _mascot_config(words, sc),
        "stepTimes": _step_times(words, sc.solution, len(steps)),
        "hook": {"big": plan.hook_big or title, "small": plan.hook_small,
                 "layout": "claim" if plan.layout == "claim" and plan.myth and plan.truth else "number",
                 "myth": plan.myth, "truth": plan.truth},
        "problem": {"before": plan.problem_before, "after": plan.problem_after},
        "flow": flow,
        "steps": steps,
        "code": {"text": code, "title": _code_title(code_lang)},
        "cta": cta.screen(cta_mode),
        "chunks": _chunks(words),
    }


def _html(config: dict) -> str:
    sans, mono = _fonts()
    return (
        _COMPOSITION.read_text(encoding="utf-8")
        .replace("__FONT_SANS__", sans)
        .replace("__FONT_MONO__", mono)
        .replace("__CONFIG__", json.dumps(config, ensure_ascii=False))
    )


def _pick_background() -> Path | None:
    d = settings.backgrounds_dir
    files = [p for p in d.iterdir() if p.suffix.lower() in _VIDEO_EXT] if d.is_dir() else []
    return random.choice(files) if files else None


def _bg_segments(config: dict, min_len: float = 1.5) -> list[float]:
    """Длительности кусков фона: смена картинки совпадает со сменой сцены.

    Фон, который режется на равные части, меняется невпопад. Смена в момент перехода к новой
    мысли работает как монтажная склейка и возвращает внимание. Слишком короткие куски склеиваем.
    """
    sc = config["scenes"]
    cuts = [0.0]
    for name in ("problem", "solution", "takeaway", "cta"):
        t = sc[name][0]
        if t - cuts[-1] >= min_len and config["duration"] - t >= min_len:
            cuts.append(t)
    cuts.append(config["duration"])
    return [b - a for a, b in zip(cuts, cuts[1:])]


def _ffmpeg_args(segments: list[float], clips: list[Path], frames_dir: Path) -> tuple[list[str], str, int]:
    """Вход 0..n-1 — куски фона, затем кадры вёрстки и звук. Возвращает (входы, фильтр, индекс кадров)."""
    duration = sum(segments)
    # Фон обрабатываем в половинном разрешении и только потом растягиваем: вчетверо меньше работы
    # на кадр, а лёгкая мягкость от апскейла тут кстати — фон должен уходить назад, а не спорить с текстом.
    hw, hh = W // 2, H // 2
    zw, zh = int(hw * 1.2) // 2 * 2, int(hh * 1.2) // 2 * 2  # запас на медленный «дрейф» камеры
    grade = f"eq=brightness=-0.16:saturation=0.55:contrast=0.94,scale={W}:{H}:flags=bicubic"

    if clips:
        inputs: list[str] = []
        parts: list[str] = []
        for i, seg in enumerate(segments):
            if i == len(segments) - 1 and len(segments) >= 3:
                # финал с призывом — чистый фон: любая движущаяся картинка (особенно с чужими надписями)
                # спорит с кнопкой «подписаться» и раздражает
                inputs += ["-f", "lavfi", "-t", f"{seg:.3f}", "-i", f"color=c=0x05070a:s={hw}x{hh}:r={FPS}"]
                parts.append(f"[{i}:v]setsar=1,format=yuv420p[b{i}]")
                continue
            clip = clips[i % len(clips)]
            inputs += ["-stream_loop", "-1", "-t", f"{seg:.3f}", "-i", str(clip.resolve())]
            # кадр чуть увеличен и плывёт по диагонали; направление чередуем, чтобы соседние куски не повторяли движение
            k = f"t/{seg:.3f}" if i % 2 == 0 else f"(1-t/{seg:.3f})"
            drift = f"crop={hw}:{hh}:x='(iw-ow)*{k}':y='(ih-oh)*{k}'"
            parts.append(f"[{i}:v]fps={FPS},setpts=PTS-STARTPTS,"
                         f"scale={zw}:{zh}:force_original_aspect_ratio=increase,{drift},setsar=1,format=yuv420p[b{i}]")
        joined = "".join(f"[b{i}]" for i in range(len(segments)))
        bg = ";".join(parts) + f";{joined}concat=n={len(segments)}:v=1:a=0,{grade}[bg]"
        frames_idx = len(segments)
    else:
        inputs = ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", f"color=c=0x05070a:s={W}x{H}:r={FPS}"]
        bg = "[0:v]null[bg]"
        frames_idx = 1

    inputs += ["-framerate", str(FPS), "-i", str(frames_dir.resolve() / "%06d.png")]
    vf = f"{bg};[bg][{frames_idx}:v]overlay=format=auto:shortest=1,format=yuv420p[v]"
    return inputs, vf, frames_idx


CHROME_ARGS = [
    "--no-sandbox", "--disable-dev-shm-usage",  # обязательно для запуска в контейнере
    "--force-color-profile=srgb", "--disable-lcd-text", "--font-render-hinting=none",
    "--hide-scrollbars", "--disable-gpu",
]


async def _render_frames(html: str, duration: float, outdir: Path, size: tuple[int, int] = (W, H),
                         fps: int = FPS, fmt: str = "png") -> int:
    """Снимает кадры в outdir/000000.<fmt>. Вкладки работают параллельно — рендер упирается в CPU.

    size/fps/fmt нужны длинному ролику (explainer.py): 16:9, 25 к/с, непрозрачный JPEG вместо PNG с прозрачностью.
    """
    from playwright.async_api import async_playwright

    total = max(1, int(duration * fps))
    workers = max(1, settings.render_workers)
    launch: dict = {"args": CHROME_ARGS}
    if settings.chromium_path and Path(settings.chromium_path).exists():
        launch["executable_path"] = settings.chromium_path

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch)

        async def worker(offset: int) -> None:
            page = await browser.new_page(viewport={"width": size[0], "height": size[1]}, device_scale_factor=1)
            await page.set_content(html, wait_until="load")
            await page.wait_for_function("window.layoutDone === true", timeout=60000)
            cdp = await page.context.new_cdp_session(page)
            # прозрачный фон: под кадры вёрстки ffmpeg подкладывает видео
            await cdp.send("Emulation.setDefaultBackgroundColorOverride",
                           {"color": {"r": 0, "g": 0, "b": 0, "a": 0}})
            for i in range(offset, total, workers):
                await page.evaluate("t => window.seek(t)", i / fps)
                # PNG с optimizeForSpeed: webp на этих кадрах Chromium кодирует вчетверо дольше
                params: dict = {"format": fmt, "optimizeForSpeed": True}
                if fmt == "jpeg":
                    params["quality"] = 92
                shot = await cdp.send("Page.captureScreenshot", params)
                ext = "jpg" if fmt == "jpeg" else fmt
                (outdir / f"{i:06d}.{ext}").write_bytes(base64.b64decode(shot["data"]))
            await page.close()

        try:
            await asyncio.gather(*(worker(k) for k in range(workers)))
        finally:
            await browser.close()
    return total


async def render(
    workdir: Path, audio: Path, words: list[Word], title: str, code_snippet: str = "",
    plan: ScenePlan | None = None, stock_clips: list[Path] | None = None, code_lang: str = "",
    cta_mode: str | None = None, segment_words: list[int] | None = None, takeaway: str = "",
) -> Path:
    if not words:
        raise RenderError("нет таймкодов слов для субтитров")
    if plan is None:
        plan = ScenePlan(hook_big=title, hook_small="", problem_before="", problem_after="",
                         steps=[], stock_queries=[])

    config = build_config(words, plan, title, code_snippet, code_lang, cta_mode, segment_words, takeaway)
    duration = config["duration"]
    clips = list(stock_clips or [])
    if not clips:
        local = _pick_background()
        if local:
            clips = [local]
    config["ambient"] = not clips   # ни стока, ни своего фона: рисуем тихий анимированный фон вместо чёрного

    out = workdir / "shorts_render.mp4"
    frames_dir = workdir / "frames"
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True)

    try:
        started = time.monotonic()
        frames = await _render_frames(_html(config), duration, frames_dir)
        log.info("frames: %s за %.0f c", frames, time.monotonic() - started)

        inputs, vf, frames_idx = _ffmpeg_args(_bg_segments(config), clips, frames_dir)
        audio_idx = frames_idx + 1  # звук — последний вход, сразу после кадров вёрстки
        extra_in: list[str] = []
        amap = f"{audio_idx}:a"
        next_in = audio_idx + 1
        sfx_idx = music_idx = None
        if settings.sfx_enabled:
            try:
                sfx_wav = sfx.render(config, workdir / "sfx.wav", audio)
                extra_in += ["-i", str(sfx_wav.resolve())]
                sfx_idx, next_in = next_in, next_in + 1
            except Exception:
                log.exception("звуковые акценты не собрались, ролик выйдет без них")
        if settings.music_enabled:
            try:
                mus_wav = music.render(config["duration"], workdir / "music.wav",
                                       sfx.voice_peak(audio), settings.music_level, settings.music_style)
                extra_in += ["-i", str(mus_wav.resolve())]
                music_idx, next_in = next_in, next_in + 1
            except Exception:
                log.exception("музыка не собралась, ролик выйдет без неё")
        labels = [f"[{audio_idx}:a]"]
        if music_idx is not None:
            # голос идёт и в микс, и управляющим сигналом: на словах музыка приглушается, на паузах поднимается
            vf += (f";[{audio_idx}:a]asplit=2[vo1][vo2];"
                   f"[{music_idx}:a][vo2]sidechaincompress=threshold=0.03:ratio=5:attack=50:release=450[md]")
            labels = ["[vo1]", "[md]"]
        if sfx_idx is not None:
            labels.append(f"[{sfx_idx}:a]")
        if len(labels) > 1:
            # amix делит каждый вход на число входов, volume возвращает голос на место; лимитер страхует от клиппинга
            vf += (f";{''.join(labels)}amix=inputs={len(labels)}:duration=first:dropout_transition=0,"
                   f"volume={len(labels)},alimiter=limit=0.95[a]")
            amap = "[a]"
        cmd = [
            settings.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
            *inputs, "-i", str(audio.resolve()), *extra_in,
            "-filter_complex", vf, "-map", "[v]", "-map", amap,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-maxrate", "4000k", "-bufsize", "8000k", "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart",
            str(out.resolve()),
        ]
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=workdir, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RenderError(f"ffmpeg exit {proc.returncode}: {err.decode(errors='replace')[-700:]}")
    except RenderError:
        raise
    except Exception as e:
        raise RenderError(f"вёрстка кадров: {type(e).__name__}: {e}") from e
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)  # кадры занимают сотни мегабайт

    log.info("rendered %s frames -> %s (%.1f s)", frames, out.name, duration)
    return out
