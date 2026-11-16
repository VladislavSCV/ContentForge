"""Профили контента: всё, что отличает одну команду от другой, лежит в profiles/<имя>/.

    profiles/<имя>/profile.toml     флаги поведения, темы и их веса, столпы, рубрики, ленты, запасные запросы к стоку
    profiles/<имя>/prompts/*.md     промпты; в тексте работают {brand_name} и {tg_channel_link}

Профиль может наследоваться (`extends = "engineering"`): промпты, которых в папке нет, берутся у родителя,
`[flags]` сливаются, остальные таблицы и списки профиля заменяют родительские целиком (в том числе пустые).
engineering — эталон поведения. Новая команда = копия папки general, правка текстов и `PROFILE=<имя>` в .env.
Проверки на старте: неизвестный флаг, битый шаблон темы, недостающий промпт — ошибка с названием файла.
"""

import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import settings

PROMPT_NAMES = ("system", "tg_review", "tg_edit", "script", "story_script", "script_review",
                "linkedin", "linkedin_en", "habr_plan", "habr_section", "scene")
FLAG_NAMES = ("story_shorts", "claim_layout", "flow_layout", "rubrics", "code_warning", "code_scene")
_TOP_KEYS = {"extends", "description", "sources_file", "stock_fallback", "flags", "pillars", "rubrics",
             "pillar_patterns", "themes"}
_THEME_KEYS = {"id", "label", "weight", "pattern"}


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    story_shorts: bool        # разрешён формат Shorts «история с героем»
    claim_layout: bool        # сцена «миф → правда» в ролике
    flow_layout: bool         # сцена-схема из узлов в ролике
    rubrics: bool             # рубрика (столп бренда) первой строкой Telegram-поста
    code_warning: bool        # предупреждать, что в посте нет кода или команды
    code_scene: bool          # в ролике показывается терминал с кодом из поста
    stock_fallback: tuple     # запросы к стоковому видео, если модель не дала своих
    sources_file: Path        # RSS/Atom-ленты для подбора тем
    themes: tuple             # ((вес, шаблон), …): что интересно аудитории; индекс i = ключ темы t{i}
    theme_labels: tuple       # человекочитаемые названия тем, в том же порядке
    market_pattern: str | None  # шаблон темы с id = "market" (рынок и найм), если есть
    ai_pattern: str | None    # шаблон столпа ai
    pillars: dict             # столп → подпись источника темы (порядок = порядок выбора)
    rubrics_map: dict         # столп → рубрика над постом
    prompt_dirs: tuple        # папки prompts/ от самой специфичной к родительской

    def prompt(self, name: str) -> str:
        """Текст промпта с подставленным брендом. Подставляем replace-ом, а не format: в промптах есть JSON в скобках."""
        if name not in PROMPT_NAMES:
            raise ProfileError(f"неизвестный промпт {name!r}, есть {PROMPT_NAMES}")
        for d in self.prompt_dirs:
            f = d / f"{name}.md"
            if f.exists():
                text = f.read_text(encoding="utf-8")
                text = text[:-1] if text.endswith("\n") else text
                return text.replace("{brand_name}", settings.brand_name).replace("{tg_channel_link}", settings.tg_channel_link)
        raise ProfileError(f"профиль {self.name!r}: нет промпта {name}.md ни в {[str(d) for d in self.prompt_dirs]}")


def profiles_dir() -> Path:
    return settings.profiles_dir or Path(__file__).resolve().parent.parent / "profiles"


def available() -> list[str]:
    d = profiles_dir()
    return sorted(p.name for p in d.iterdir() if (p / "profile.toml").exists()) if d.is_dir() else []


def _read(name: str) -> dict:
    path = profiles_dir() / name / "profile.toml"
    if not path.exists():
        raise ProfileError(f"CONTENT_PROFILE={name!r}: нет файла {path}; доступные профили: {available()}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(f"{path}: {e}") from e
    extra = set(data) - _TOP_KEYS
    if extra:
        raise ProfileError(f"{path}: неизвестные ключи {sorted(extra)}")
    return data


def _chain(name: str) -> list[tuple[str, dict]]:
    """Профиль и его предки, от самого специфичного."""
    out, seen = [], set()
    while name:
        if name in seen:
            raise ProfileError(f"профили наследуются по кругу: {sorted(seen)} → {name}")
        seen.add(name)
        data = _read(name)
        out.append((name, data))
        name = data.get("extends", "")
    return out


@lru_cache(maxsize=None)
def load(name: str) -> Profile:
    chain = _chain(name)
    merged: dict = {}
    flags: dict = {}
    for _, data in reversed(chain):          # от предка к потомку: потомок заменяет
        for k, v in data.items():
            if k == "flags":
                flags.update(v)
            elif k != "extends":
                merged[k] = v
    bad = set(flags) - set(FLAG_NAMES)
    if bad:
        raise ProfileError(f"профиль {name!r}: неизвестные флаги {sorted(bad)}, есть {FLAG_NAMES}")
    missing = [f for f in FLAG_NAMES if f not in flags]
    if missing:
        raise ProfileError(f"профиль {name!r}: не заданы флаги {missing}")
    themes, labels, market = [], [], None
    for i, t in enumerate(merged.get("themes", [])):
        extra = set(t) - _THEME_KEYS
        if extra or not {"weight", "pattern"} <= set(t):
            raise ProfileError(f"профиль {name!r}: тема #{i}: нужны weight и pattern, лишние ключи {sorted(extra)}")
        try:
            re.compile(t["pattern"])
        except re.error as e:
            raise ProfileError(f"профиль {name!r}: тема #{i}: битый шаблон: {e}") from e
        themes.append((int(t["weight"]), t["pattern"]))
        labels.append(t.get("label", f"тема {i}"))
        if t.get("id") == "market":
            market = t["pattern"]
    ai = merged.get("pillar_patterns", {}).get("ai")
    if ai:
        try:
            re.compile(ai)
        except re.error as e:
            raise ProfileError(f"профиль {name!r}: pillar_patterns.ai: {e}") from e
    pillars = dict(merged.get("pillars", {}))
    if pillars and not market:
        raise ProfileError(f"профиль {name!r}: столпы заданы, но нет темы с id = \"market\"")
    return Profile(
        name=name, description=merged.get("description", ""), **{f: bool(flags[f]) for f in FLAG_NAMES},
        stock_fallback=tuple(merged.get("stock_fallback", ())),
        sources_file=Path(merged.get("sources_file", "assets/sources.txt")),
        themes=tuple(themes), theme_labels=tuple(labels), market_pattern=market, ai_pattern=ai,
        pillars=pillars, rubrics_map=dict(merged.get("rubrics", {})),
        prompt_dirs=tuple(profiles_dir() / n / "prompts" for n, _ in chain),
    )


def current() -> Profile:
    return load(settings.content_profile)
