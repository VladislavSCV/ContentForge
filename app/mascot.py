"""Маскот автора в ролике: позы-стикеры и выбор позы под смысл фразы.

Лист стикеров режется скриптом scripts/mascot_slice.py на assets/mascot/poses/NN.png.
Маска закрывает рот, поэтому «говорит» маскот телом (подпрыгивает на словах — это в вёрстке)
и сменой эмоции: каждая новая фраза получает позу по смыслу. Деньги — пачка купюр, сбой —
фейспалм или вздох, код — ноутбук, результат — палец вверх. Фраза без явного смысла берёт
нейтральную позу сцены. Одна и та же поза не идёт подряд и не возвращается раньше, чем
пройдёт несколько других: иначе кажется, что картинки крутятся по кругу.

Нет папки poses — берём одиночный assets/mascot/mascot.png. Нет и его — ролик без маскота.
"""

import base64
import re
from pathlib import Path

from .config import settings
from .tts import Word

# Эмоции по приоритету: (название, регулярка по тексту фразы, позы, в каких сценах уместна).
# Номера поз — как на контактном листе data/mascot_contact.png.
# 01 «мир», 02 палец на зрителя, 04 чешет голову, 05 разводит руками, 06 руки скрещены,
# 07 думает, 08 «идея», 09 кофе, 10 фейспалм, 11 ноутбук, 12 вздох, 13 деньги,
# 14 сердечко, 15 спиной «мир», 16 в кресле, 17 селфи, 18 палец вверх.
ALL = ("hook", "problem", "solution", "takeaway", "cta")
NOT_CTA = ("hook", "problem", "solution", "takeaway")
EMOTIONS: list[tuple[str, str, list[str], tuple[str, ...]]] = [
    ("love", r"подпиш|польз|совет|спасибо|вместе", ["14", "17", "01"], ("cta", "takeaway")),
    ("money", r"деньг|долл|тысяч|\$|стоим|счёт|счет|платим|платит|дорог|эконом|бюджет|убытк|выручк", ["13"], NOT_CTA),
    ("fail", r"упал|рухн|сбой|авари|простоя?|простой|ошибк|заблокир|перегруж|тормоз|медленн|катастроф|не работа|сломал|завис", ["10", "12", "04"], ("hook", "problem", "solution")),
    ("night", r"ночь|ночью|дежур|пятниц|вызов|алерт|в три", ["09", "12"], ("hook", "problem")),
    ("success", r"ускор|быстр|меньше|снизил|сократ|полегчал|выросл|в \d+ раз|процент|результат|получил|заработал|стало лучше", ["18", "17"], ("hook", "solution", "takeaway")),
    ("code", r"запрос|индекс|команд|конфиг|код\b|скрипт|строчк|таблиц|создал|созда|добав|запуст|выкат|деплой|образ|контейнер|sql|docker|слой", ["11", "16", "07"], ("hook", "problem", "solution")),
    ("think", r"анализ|смотр|провер|измер|метрик|найд|разбер|сначала|причин|explain|разобра|понять|понима", ["07", "06"], ("problem", "solution", "takeaway")),
    ("idea", r"одну|одна|одной|одно\b|всего|секрет|трюк|решени|идея|просто|хитрост|лайфхак|нужен", ["08"], ("hook", "solution", "takeaway")),
    ("confused", r"почему|зачем|как так|непонятно|что делать|не знаешь|\?", ["05", "04"], ("hook", "problem")),
    ("relax", r"автомат|само\b|спокойн|без боли|незаметно|никто не заметил", ["16"], ("solution", "takeaway")),
]
# Нейтральные позы сцены: берём, когда фраза не попала ни в одну эмоцию.
DEFAULT_POSES: dict[str, list[str]] = {
    "hook": ["02", "05", "17"],
    "problem": ["04", "05", "06", "07"],
    "solution": ["06", "07", "08", "16"],
    "takeaway": ["18", "08", "06"],
    "cta": ["14", "01"],
}
MIN_HOLD = 1.5       # не меняем позу чаще: иначе картинка мельтешит
MAX_HOLD = 3.4       # а в длинной фразе меняем сами, чтобы маскот не застывал
TAIL_KEEP = 0.9      # перед концом сцены не меняем — поза должна успеть «сыграть»


def _uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


def _needed() -> list[str]:
    ids = {p for _, _, poses, _ in EMOTIONS for p in poses}
    ids |= {p for poses in DEFAULT_POSES.values() for p in poses}
    return sorted(ids)


def load() -> dict[str, str]:
    """Позы, которые реально лежат на диске: {id: data-uri}. Пусто — маскота нет."""
    poses_dir = settings.mascot_dir / "poses"
    found = {p: _uri(poses_dir / f"{p}.png") for p in _needed() if (poses_dir / f"{p}.png").is_file()}
    if found:
        return found
    single = settings.mascot_dir / "mascot.png"
    return {"main": _uri(single)} if single.is_file() else {}


def candidates(text: str, scene: str, available: set[str]) -> tuple[str, list[str]]:
    """(эмоция, позы-кандидаты) для фразы в данной сцене."""
    low = text.lower()
    for name, rx, poses, scenes in EMOTIONS:
        if scene in scenes and re.search(rx, low):
            ok = [p for p in poses if p in available]
            if ok:
                return name, ok
    return "default", [p for p in DEFAULT_POSES[scene] if p in available]


def _choose(options: list[str], recent: list[str]) -> str:
    """Из кандидатов берём ту, что дольше всех не показывали (не было среди последних)."""
    def age(p: str) -> int:
        return recent[::-1].index(p) if p in recent else 99
    return max(options, key=lambda p: (age(p), -options.index(p)))


def _sentences(words: list[Word]) -> list[tuple[float, str]]:
    out, cur = [], []
    for w in words:
        cur.append(w)
        if re.search(r"[.!?…]$", w.text):
            out.append((cur[0].start, " ".join(x.text for x in cur)))
            cur = []
    if cur:
        out.append((cur[0].start, " ".join(x.text for x in cur)))
    return out


def timeline(words: list[Word], scenes: dict[str, tuple[float, float]], available: set[str]) -> list[list]:
    """[[время, поза], …]: на начале каждой фразы поза выбирается по её смыслу."""
    if "main" in available:
        return [[0.0, "main"]]
    sentences = _sentences(words)
    events: list[list] = []
    recent: list[str] = []

    def put(t: float, pose: str) -> None:
        events.append([round(t, 3), pose])
        recent.append(pose)

    for scene in ALL:
        s0, s1 = scenes[scene]
        if s1 - s0 < 0.05:
            continue
        inside = [(t, txt) for t, txt in sentences if s0 - 0.01 <= t < s1]
        # первая поза сцены — по первой фразе (фразы сцены могут начаться чуть раньше её границы)
        text0 = inside[0][1] if inside else ""
        _, opts = candidates(text0, scene, available)
        if not opts:
            continue
        last = s0
        put(0.0 if scene == "hook" else s0, _choose(opts, recent))
        for t, txt in inside[1:]:
            if t >= s1 - TAIL_KEEP:
                break
            # долгую паузу между сменами разбавляем сами — той же эмоцией, другой позой
            while t - last > MAX_HOLD * 1.6 and last + MAX_HOLD < s1 - TAIL_KEEP:
                last += MAX_HOLD
                _, o = candidates(inside[0][1], scene, available)
                put(last, _choose(o, recent))
            if t - last >= MIN_HOLD:
                _, o = candidates(txt, scene, available)
                if o:
                    pose = _choose(o, recent)
                    # одна и та же поза подряд — это не смена, пропускаем
                    if pose != events[-1][1]:
                        put(t, pose)
                        last = t
    return events
