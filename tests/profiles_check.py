"""Проверка загрузчика профилей и FEATURES без сети и ключей. Запуск: .venv/bin/python tests/profiles_check.py
Каждый случай идёт в отдельном процессе, потому что настройки читаются при импорте."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE_ENV = {**os.environ, "BRAND_NAME": "T", "TG_CHANNEL_LINK": "https://t.me/t", "TELEGRAM_BOT_TOKEN": "x", "ADMIN_IDS": "1"}
fails = 0


def run(code: str, expect_ok: bool, expect_text: str = "", **env) -> None:
    global fails
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, env={**BASE_ENV, **env})
    out = r.stdout + r.stderr
    ok = (r.returncode == 0) == expect_ok and expect_text in out
    fails += not ok
    print(("OK   " if ok else "FAIL ") + code[:60].replace("\n", " ") + f" {env}")
    if not ok:
        print(out[-600:])


def variant(tmp: Path, edit) -> Path:
    d = tmp / "p"
    shutil.copytree(ROOT / "profiles", d)
    edit(d)
    return d


run("import app.prompts", True, PROFILE="general")
run("import app.prompts", False, "нет файла", PROFILE="nope")
run("from app import profiles; print(profiles.current().name)", True, "general", CONTENT_PROFILE="general")   # старое имя
run("import app.config", False, "FEATURES", FEATURES="bogus")
run("from app import features as f; assert f.enabled('habr') and not f.enabled('reddit')", True, FEATURES="habr,week")
run("from app import features as f; assert all(f.enabled(n) for n in f.KNOWN)", True, FEATURES="*")
with tempfile.TemporaryDirectory() as t:
    tmp = Path(t)
    d = variant(tmp, lambda d: (d / "general/profile.toml").write_text(
        (d / "general/profile.toml").read_text(encoding="utf-8").replace("pattern = '", "pattern = '(", 1), encoding="utf-8"))
    run("import app.topics", False, "битый шаблон", PROFILE="general", PROFILES_DIR=str(d))
    d = variant(tmp / "b", lambda d: (d / "general/prompts/system.md").unlink() or (d / "engineering/prompts/system.md").unlink())
    run("import app.prompts", False, "нет промпта", PROFILE="general", PROFILES_DIR=str(d))
    d = variant(tmp / "c", lambda d: (d / "general/profile.toml").write_text(
        (d / "general/profile.toml").read_text(encoding="utf-8").replace("[flags]\n", "[flags]\nfoo = true\n"), encoding="utf-8"))
    run("import app.prompts", False, "неизвестные флаги", PROFILE="general", PROFILES_DIR=str(d))
    d = variant(tmp / "d", lambda d: (d / "general/profile.toml").write_text(
        (d / "general/profile.toml").read_text(encoding="utf-8").replace('extends = "engineering"', 'extends = "general"'), encoding="utf-8"))
    run("import app.prompts", False, "по кругу", PROFILE="general", PROFILES_DIR=str(d))
print("всё в порядке" if not fails else f"упало: {fails}")
sys.exit(1 if fails else 0)
