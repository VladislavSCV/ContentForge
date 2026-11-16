"""Фоновая музыка ролика: тёплый пэд, мягкий бас, лёгкий пульс. Без файлов и без чужих лицензий.

Синтез стандартной библиотекой, как в sfx.py. Один цикл из четырёх тактов повторяется на всю длину ролика.
Три стиля (MUSIC_STYLE): calm — мягкий, tense — тёмный и напряжённый для историй про взломы и сбои,
drive — бодрый, с заметным пульсом. Громкость задаётся долей от пика голоса, под речь музыку
дополнительно приглушает ffmpeg (sidechaincompress в video.py и explainer.py), на паузах она поднимается.
НЕ оценено на слух: тембр, темп и уровень подбираются автором; образцы — scripts/music_samples.py.
"""

import math
import random
import struct
import wave
from pathlib import Path

SR = 44100
_MIN, _MAJ, _FIFTH = 1.1892, 1.2599, 1.4983
# корень аккорда (Гц), терция, квинта; темп; уровни баса, «бочонка» и «хэта»
STYLES = {
    "calm": dict(bpm=92, bass=.32, kick=.40, hat=.05,
                 chords=[(110.00, _MIN, _FIFTH), (87.31, _MAJ, _FIFTH), (130.81, _MAJ, _FIFTH), (98.00, _MAJ, _FIFTH)]),
    "tense": dict(bpm=100, bass=.38, kick=.22, hat=.03,
                  chords=[(110.00, _MIN, _FIFTH), (87.31, _MAJ, _FIFTH), (73.42, _MIN, _FIFTH), (82.41, _MAJ, _FIFTH)]),
    "drive": dict(bpm=118, bass=.34, kick=.55, hat=.09,
                  chords=[(82.41, _MIN, _FIFTH), (130.81, _MAJ, _FIFTH), (98.00, _MAJ, _FIFTH), (73.42, _MAJ, _FIFTH)]),
}


def _cycle(style: str = "calm", shift: float = 1.0, kick_scale: float = 1.0) -> list[float]:
    """Четыре такта музыки, пик нормирован к 1.0. shift — сдвиг тональности (множитель частот), kick_scale — пульс."""
    st = STYLES.get(style) or STYLES["calm"]
    beat = 60 / st["bpm"]
    bar = 4 * beat
    n_bar = int(SR * bar)
    rnd = random.Random(7)
    out: list[float] = []
    for root, third, fifth in st["chords"]:
        root *= shift
        # пэд на октаву выше корня: три ноты, у каждой две слегка расстроенные струны («хор»)
        freqs = [root * 2, root * 2 * third, root * 2 * fifth]
        w = [2 * math.pi * f * d / SR for f in freqs for d in (0.997, 1.003)]
        ph = [0.0] * len(w)
        bw = 2 * math.pi * root / SR
        bph = 0.0
        for i in range(n_bar):
            t = i / SR
            # плавный вход и выход аккорда: стыки без щелчков
            env = min(1.0, t / 0.9) * min(1.0, (bar - t) / 0.35)
            pad = 0.0
            for k in range(len(w)):
                ph[k] += w[k]
                pad += math.sin(ph[k]) + 0.25 * math.sin(2 * ph[k])
            pad *= env * 0.05
            # бас: щипок на каждую восьмую
            e8 = (t % (beat / 2))
            bph += bw
            bass = math.sin(bph) * math.exp(-e8 * 7) * st["bass"]
            # мягкий «бочонок» на первой и третьей долях
            tk = t % (2 * beat)
            kick = math.sin(2 * math.pi * (46 + 70 * math.exp(-tk * 28)) * tk) * math.exp(-tk * 9) * st["kick"] * kick_scale
            # тихий шумовой «хэт» на слабой восьмой
            th = (t + beat / 2) % beat
            hat = rnd.uniform(-1, 1) * math.exp(-th * 55) * st["hat"] if th < 0.08 else 0.0
            out.append(pad + bass + kick + hat)
    peak = max(abs(x) for x in out) or 1.0
    return [x / peak for x in out]


def render(duration: float, path: Path, voice_peak: float = 0.7, level: float = 0.30, style: str = "calm") -> Path:
    """Дорожка длиной в ролик: моно, 16 бит. Пик музыки = level от пика голоса.

    Один и тот же четырёхтактовый круг дольше минуты утомляет, поэтому ролик делится на секции по ~40 секунд:
    сначала без «бочонка» (вступление), дальше с пульсом, с каждой секцией тональность уходит на тон выше/ниже.
    Для Shorts (до ~40 с) это одна секция-вступление с пульсом, как раньше.
    """
    short = duration <= 45
    plan = [(1.0, 1.0)] if short else [(1.0, 0.0), (1.0, 1.0), (1.1225, 1.0), (0.8909, 1.0)]
    cycles = [_cycle(style, sh, ks) for sh, ks in plan]
    sec_len = len(cycles[0]) * 4          # секция = четыре круга, около 40 секунд
    total = int(SR * (duration + 0.5))
    gain = level * voice_peak
    fade_in, fade_out = int(SR * 0.6), int(SR * 1.8)
    samples = bytearray()
    for i in range(total):
        cyc = cycles[min(i // sec_len, len(cycles) - 1) if short else (i // sec_len) % len(cycles)]
        x = cyc[i % len(cyc)] * gain
        if i < fade_in:
            x *= i / fade_in
        elif i > total - fade_out:
            x *= max(0.0, (total - i) / fade_out)
        samples += struct.pack("<h", int(max(-1.0, min(1.0, x)) * 32767))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(bytes(samples))
    return path
