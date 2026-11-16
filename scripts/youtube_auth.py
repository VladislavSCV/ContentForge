"""Одноразовое получение YOUTUBE_REFRESH_TOKEN.

1. Google Cloud Console → проект → включи «YouTube Data API v3».
2. Google Auth Platform → Audience: External, добавь свой аккаунт в Test users.
3. Clients → Create client → Desktop app → Download JSON в корень проекта.
4. Запусти:  python scripts/youtube_auth.py [client_secret_….json]
   Открой ссылку, войди в аккаунт с каналом, разреши доступ. Браузер уйдёт на
   http://localhost/?code=… и покажет «не удаётся подключиться» — это нормально:
   скопируй адрес из строки браузера целиком и вставь в терминал.

Без интерактива (два шага):
   python scripts/youtube_auth.py --url               # печатает ссылку
   python scripts/youtube_auth.py --code '<адрес>'    # меняет код на токен
Токены сразу записываются в .env.

Второй токен, только на чтение статистики (для итогов недели): добавь флаг --stats
   python scripts/youtube_auth.py --stats --url
   python scripts/youtube_auth.py --stats --code '<адрес>'
Он пишется в YOUTUBE_STATS_REFRESH_TOKEN и не трогает токен загрузки.
Чтобы токены жили месяцами, переведи приложение в Google Auth Platform → Audience → «In production».
"""

import json
import re
import sys
from pathlib import Path

from google_auth_oauthlib.flow import Flow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]  # только загрузка — удалять/менять видео токен не может
# второй токен (флаг --stats): только чтение статистики; пишется в YOUTUBE_STATS_REFRESH_TOKEN
STATS_SCOPES = ["https://www.googleapis.com/auth/youtube.readonly",
                "https://www.googleapis.com/auth/yt-analytics.readonly"]
REDIRECT = "http://localhost:8085/"
ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / ".youtube_auth_state.json"


def _secret_file(args: list[str]) -> Path:
    for a in args:
        if a.endswith(".json"):
            return Path(a)
    found = sorted(ROOT.glob("client_secret*.json"))
    if not found:
        sys.exit("Не найден client_secret*.json в корне проекта")
    return found[-1]


def _flow(secret: Path, verifier: str | None = None, stats: bool = False) -> Flow:
    flow = Flow.from_client_secrets_file(str(secret), STATS_SCOPES if stats else SCOPES, redirect_uri=REDIRECT, code_verifier=verifier,
                                         autogenerate_code_verifier=verifier is None)
    return flow


def _save_env(values: dict[str, str]) -> None:
    env = ROOT / ".env"
    text = env.read_text(encoding="utf-8") if env.exists() else ""
    for k, v in values.items():
        if re.search(rf"^{k}=.*$", text, flags=re.M):
            text = re.sub(rf"^{k}=.*$", f"{k}={v}", text, flags=re.M)
        else:
            text += f"\n{k}={v}\n"
    env.write_text(text, encoding="utf-8")


def print_url(secret: Path, stats: bool = False) -> None:
    flow = _flow(secret, stats=stats)
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"verifier": flow.code_verifier, "state": state, "secret": str(secret), "stats": stats}))
    print(f"\nОткрой ссылку и войди в аккаунт с YouTube-каналом:\n\n{url}\n")


def exchange(response_url: str) -> None:
    saved = json.loads(STATE.read_text())
    stats = bool(saved.get("stats"))
    flow = _flow(Path(saved["secret"]), saved["verifier"], stats)
    response_url = response_url.strip().replace("http://", "https://", 1)  # oauthlib требует https в URL ответа
    flow.fetch_token(authorization_response=response_url)
    creds = flow.credentials
    if not creds.refresh_token:
        sys.exit("Google не вернул refresh_token — запусти заново (нужен prompt=consent)")
    client = json.loads(Path(saved["secret"]).read_text())["installed"]
    values = {
        "YOUTUBE_CLIENT_ID": client["client_id"],
        "YOUTUBE_CLIENT_SECRET": client["client_secret"],
        # токен статистики кладём отдельно, чтобы не затереть токен загрузки
        "YOUTUBE_STATS_REFRESH_TOKEN" if stats else "YOUTUBE_REFRESH_TOKEN": creds.refresh_token,
    }
    _save_env(values)
    STATE.unlink(missing_ok=True)
    print("\nГотово, записал в .env:")
    for k, v in values.items():
        print(f"{k}={v[:12]}…")


if __name__ == "__main__":
    import os
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    args = sys.argv[1:]
    stats = "--stats" in args
    if "--url" in args:
        print_url(_secret_file(args), stats)
    elif "--code" in args:
        exchange(args[args.index("--code") + 1])
    else:
        print_url(_secret_file(args), stats)
        exchange(input("Вставь адрес из строки браузера (http://localhost:8085/?...): "))
