"""Снимок промптов и словарей тем по профилям контента. Нужен для рефакторинга профилей:
после правок `python tests/golden_snapshot.py --check` должен сказать «без отличий».

Запуск без аргументов перезаписывает tests/golden/<профиль>.json; --check только сравнивает.
Профиль и бренд задаются через окружение, поэтому значения из .env в снимок не попадают.
"""
import difflib
import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden"
MODULES = ["app.prompts", "app.ai", "app.topics", "app.learning", "app.cta", "app.tells",
           "app.pronounce", "app.quality", "app.pipeline", "app.longform", "app.explainer_script"]
PROFILES = {
    "engineering": {"CONTENT_PROFILE": "engineering", "SOURCES_FILE": "assets/sources.txt"},
    "general": {"CONTENT_PROFILE": "general", "SOURCES_FILE": "assets/sources_general.txt"},
}
FIXED = {"BRAND_NAME": "TestBrand", "BRAND_HANDLE": "@testbrand", "TG_CHANNEL_LINK": "https://t.me/testbrand",
         "TELEGRAM_BOT_TOKEN": "x", "ADMIN_IDS": "1"}   # заглушки: .env в папке проекта больше не лежит


def _plain(v):
    if isinstance(v, re.Pattern):
        return {"__re__": v.pattern, "flags": v.flags}
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple, set, frozenset)):
        items = [_plain(x) for x in v]
        return sorted(items, key=json.dumps) if isinstance(v, (set, frozenset)) else items
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    raise TypeError


def dump() -> dict:
    out = {}
    for name in MODULES:
        try:
            mod = importlib.import_module(name)
        except Exception as e:  # модуль не импортируется без внешних пакетов: помечаем, а не молчим
            out[name] = {"__import_error__": type(e).__name__}
            continue
        consts = {}
        for k, v in vars(mod).items():
            if k.startswith("__") or not re.fullmatch(r"_?[A-Z][A-Z0-9_]*", k):
                continue
            try:
                consts[k] = _plain(v)
            except TypeError:
                continue
        out[name] = consts
    return out


def _compare(prof: str, old: dict, new: dict) -> int:
    """Сводка по ключам: что исчезло, что появилось, что изменилось. 0 — отличий нет."""
    lines = []
    for mod in sorted(set(old) | set(new)):
        o, n = old.get(mod, {}), new.get(mod, {})
        for k in sorted(set(o) | set(n)):
            if k not in n:
                lines.append(f"  - {mod}.{k}: исчезло")
            elif k not in o:
                lines.append(f"  + {mod}.{k}: появилось")
            elif o[k] != n[k]:
                lines.append(f"  ~ {mod}.{k}: изменилось")
    print(f"[{prof}] " + ("без отличий" if not lines else f"отличий: {len(lines)}"))
    print(chr(10).join(lines))
    return 1 if lines else 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        sys.path.insert(0, str(ROOT))
        print(json.dumps(dump(), ensure_ascii=False, indent=1, sort_keys=True))
        return 0
    check = "--check" in sys.argv
    bad = 0
    for prof, env in PROFILES.items():
        r = subprocess.run([sys.executable, __file__, "--child"], cwd=ROOT, capture_output=True, text=True,
                           env={**os.environ, **env, **FIXED})
        if r.returncode:
            print(r.stderr[-2000:]); return 2
        path = GOLDEN / f"{prof}.json"
        if check:
            bad += _compare(prof, json.loads(path.read_text(encoding="utf-8")), json.loads(r.stdout))
        else:
            path.write_text(r.stdout, encoding="utf-8")
            print(f"[{prof}] записано {len(r.stdout)} байт")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
