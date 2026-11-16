"""Звуковые акценты ролика: whoosh на смене сцены, удар на хуке, тик на узлах схемы и шагах.

Синтезируются здесь же, стандартной библиотекой: без файлов с чужой лицензией и без numpy.
Устроены как в монтаже: розовый шум (а не белый) через полосовой фильтр с плавным ходом частоты,
срез выше ~6 кГц (иначе шипит и «режет ухо»), мягкая реверберация, удар с подбасом, «деревянный» тик.
Акценты тихие и короткие, голос остаётся главным. Тайминги берутся из того же config, что и вёрстка.
НЕ оценено на слух: уровни и тембр подбираются автором; объективно проверена только доля высоких частот.
"""

import math
import random
import struct
import wave
from pathlib import Path

SR = 44100
# пиковые уровни как доля от пика ГОЛОСА: акцент не должен перекрывать речь, какой бы тихой ни была озвучка.
LEVEL = {"hit": 0.40, "whoosh": 0.20, "swipe": 0.11, "tick": 0.12}


def voice_peak(path: Path | None) -> float:
    """Пик озвучки (0..1); нечитаемый или не 16-битный файл — обычный уровень речи."""
    try:
        with wave.open(str(path), "rb") as w:
            if w.getsampwidth() != 2:
                return .7
            raw = w.readframes(w.getnframes())
        peak = max(abs(x) for x in struct.unpack("<%dh" % (len(raw) // 2), raw)) / 32768
        return min(1.0, max(.05, peak))
    except Exception:
        return .7


def _pink(n: int, seed: int) -> list[float]:
    """Розовый шум (фильтр Келлета): спад к высоким частотам, звучит естественнее белого."""
    rnd = random.Random(seed)
    b0 = b1 = b2 = 0.0
    out = []
    for _ in range(n):
        x = rnd.uniform(-1, 1)
        b0 = .99765 * b0 + x * .0990460
        b1 = .96300 * b1 + x * .2965164
        b2 = .57000 * b2 + x * 1.0526913
        out.append((b0 + b1 + b2 + x * .1848) * .25)
    return out


def _bandpass_sweep(src: list[float], fc: list[float], q: float) -> list[float]:
    """Полосовой фильтр с переменной центральной частотой (Chamberlin): «взмах» без колючего шума."""
    low = band = 0.0
    out = []
    damp = 1 / q
    for x, f in zip(src, fc):
        k = 2 * math.sin(math.pi * min(f, 6000) / SR)
        low += k * band
        high = x - low - damp * band
        band += k * high
        out.append(band)
    return out


def _lowpass(x: list[float], a: float) -> list[float]:
    y, out = 0.0, []
    for s in x:
        y += a * (s - y)
        out.append(y)
    return out


def _reverb(x: list[float], wet: float = .28, tail: float = .35) -> list[float]:
    """Небольшая комната (схема Шрёдера): 4 гребёнки и 2 фазовых фильтра. Хвост удлиняет звук на tail секунд."""
    n = len(x) + int(SR * tail)
    src = x + [0.0] * (n - len(x))
    comb_out = [0.0] * n
    for d, g in ((1116, .78), (1188, .77), (1277, .76), (1356, .75)):
        buf = [0.0] * d
        for i in range(n):
            j = i % d
            y = src[i] + buf[j] * g
            buf[j] = y
            comb_out[i] += y * .25
    out = comb_out
    for d, g in ((225, .5), (556, .5)):
        buf = [0.0] * d
        res = [0.0] * n
        for i in range(n):
            j = i % d
            b = buf[j]
            v = out[i] + b * g
            buf[j] = v
            res[i] = b - v * g
        out = res
    return [s + wet * r for s, r in zip(src, out)]


def _whoosh(dur: float = .6, seed: int = 1) -> list[float]:
    """Плавный взмах при смене главы: полоса шума уходит вверх и возвращается, с мягкой комнатой."""
    n = int(SR * dur)
    fc = [220 + 2300 * math.sin(math.pi * i / n) ** 1.4 for i in range(n)]
    y = _bandpass_sweep(_pink(n, seed), fc, q=1.1)
    y = _lowpass(y, .45)
    return _reverb([s * math.sin(math.pi * i / n) ** 1.5 for i, s in enumerate(y)], wet=.22, tail=.25)


def _swipe(dur: float = .22, seed: int = 2) -> list[float]:
    """Тихий «свист» воздуха на смене сцены: короткий, без высоких, чтобы не раздражал при частом повторе."""
    n = int(SR * dur)
    fc = [350 + 1500 * (i / n) for i in range(n)]
    y = _lowpass(_bandpass_sweep(_pink(n, seed), fc, q=1.4), .4)
    return [s * math.sin(math.pi * i / n) ** 2 for i, s in enumerate(y)]


def _hit(dur: float = .5) -> list[float]:
    """Удар: подбас, «тело», приглушённый щелчок и комната. «Цифра села на место»."""
    n = int(SR * dur)
    out, p1, p2 = [], 0.0, 0.0
    rnd = random.Random(3)
    click = _lowpass([rnd.uniform(-1, 1) * math.exp(-i / SR * 70) for i in range(n)], .25)
    for i in range(n):
        t = i / SR
        p1 += 2 * math.pi * (34 + 28 * math.exp(-t * 18)) / SR
        p2 += 2 * math.pi * (70 + 80 * math.exp(-t * 30)) / SR
        out.append(math.sin(p1) * math.exp(-t * 6.5) * 1.0 + math.sin(p2) * math.exp(-t * 14) * .6 + click[i] * .5)
    return _reverb(out, wet=.3, tail=.3)


def _tick(dur: float = .16) -> list[float]:
    """Тёплый «деревянный» тик: два мягких тона с быстрым спадом, плавная атака без щелчка."""
    n = int(SR * dur)
    out = []
    for i in range(n):
        t = i / SR
        env = min(1.0, t / .002) * math.exp(-t * 38)
        out.append((math.sin(2 * math.pi * 980 * t) + .35 * math.sin(2 * math.pi * 1960 * t)) * env)
    return _lowpass(out, .6)


_BANK = {"whoosh": _whoosh, "swipe": _swipe, "hit": _hit, "tick": _tick}


def cues(config: dict) -> list[tuple[float, str]]:
    """Список (секунда, вид звука) по тому же расписанию, что и анимация в composition.html."""
    sc, hook = config["scenes"], config["hook"]
    out: list[tuple[float, str]] = []
    h0 = sc["hook"][0]
    if hook.get("layout") == "claim":
        out += [(h0 + .95, "swipe"), (h0 + 1.45, "hit")]
    else:
        out.append((h0 + .1, "hit"))
    for name in ("problem", "solution", "takeaway", "cta"):
        s0, s1 = sc[name]
        if s1 - s0 > .05:
            out.append((s0 - .05, "whoosh"))
    flow = config.get("flow") or []
    if len(flow) >= 3:
        p0, p1 = sc["problem"]
        gap = min(1.25, max(.5, (p1 - p0 - .9) / len(flow)))
        out += [(p0 + .1 + i * gap + .12, "tick") for i in range(len(flow))]
    out += [(t - .08 + .12, "tick") for t in (config.get("stepTimes") or [])]
    return sorted((max(0.0, t), k) for t, k in out)


def _resample(x: list[float], rate: float) -> list[float]:
    """Проигрывание с другой скоростью (линейная интерполяция): меняет и длину, и высоту звука."""
    n = int(len(x) / rate)
    out = []
    for i in range(n):
        p = i * rate
        k = int(p)
        f = p - k
        a = x[k]
        b = x[k + 1] if k + 1 < len(x) else 0.0
        out.append(a + (b - a) * f)
    return out


def mix_cues(cues_: list[tuple], duration: float, ref: float, path: Path, seed: int = 11) -> Path:
    """Собирает дорожку из (секунда, вид[, множитель громкости]). Один и тот же звук каждый раз чуть другой по высоте и
    громкости: повтор одинакового семпла выдаёт синтетику, а студийный звук так не делают (источник: гайды по саунд-дизайну)."""
    rnd = random.Random(seed)
    total = int(SR * (duration + .5))
    track = [0.0] * total
    base = {k: fn() for k, fn in _BANK.items()}
    for c in sorted(cues_):
        t, kind = c[0], c[1]
        mult = c[2] if len(c) > 2 else 1.0
        snd = _resample(base[kind], rnd.uniform(.93, 1.07))
        peak = max(abs(x) for x in snd) or 1.0
        g = LEVEL[kind] * mult * rnd.uniform(.85, 1.1) * ref / peak
        start = int(max(0.0, t) * SR)
        for i, x in enumerate(snd):
            j = start + i
            if j >= total:
                break
            track[j] += x * g
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1.0, min(1.0, x)) * 32767)) for x in track))
    return path


def render(config: dict, path: Path, voice: Path | None = None) -> Path:
    """Пишет дорожку акцентов Shorts: моно, 16 бит. voice — озвучка, по её пику выставляется громкость."""
    return mix_cues(cues(config), config["duration"], voice_peak(voice), path)
