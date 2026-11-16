# Как завести новую команду

Один код, у каждой команды свой инстанс: свой бот, канал, база, бренд и профиль контента.
Всё выполняется локально; на сервер ничего не уходит, выкладка делается отдельно и вручную.

## 1. Профиль (тексты и темы команды)

Профиль — папка `profiles/<имя>/`. Можно взять готовый (`engineering`, `general`) или завести свой:

```
cp -r profiles/general profiles/mycmd
```

| Что править | Где |
|---|---|
| Голос, аудитория, правила, формат поста и Shorts | `profiles/mycmd/prompts/*.md` (`{brand_name}` и `{tg_channel_link}` подставляются сами) |
| Что считается интересной темой, вес темы | `profile.toml`, `[[themes]]` (weight + regex) |
| Откуда брать темы | `sources_file` в `profile.toml` (по одной RSS-ссылке в строке) |
| Столпы и рубрики над постом | `[pillars]`, `[rubrics]`, тема с `id = "market"` |
| Что показывать в ролике (код, схемы, «миф → правда») | `[flags]` |

Правила:
- Файл промпта, которого нет в папке, берётся у родителя (`extends = "engineering"`). Для психологии, науки и т.п. родитель по умолчанию подходит, но `linkedin_en.md`, `habr_*.md` и `tg_edit.md` написаны под инженерный бренд: если команда их использует, скопируй и перепиши.
- Порядок тем в `[[themes]]` менять нельзя после накопления статистики: ключи `t0, t1…` связывают тему с поправками из `learning.py`. Новые темы добавляй в конец.
- Ошибка в профиле (битый regex, неизвестный флаг, нет промпта) останавливает запуск с названием файла.

## 2. Инстанс (токены, канал, расписание)

```
scripts/instance.sh new mycmd mycmd        # instances/mycmd/ из шаблона, PROFILE=mycmd
$EDITOR instances/mycmd/.env               # токен бота, ADMIN_IDS, канал, бренд, ключи Яндекса
scripts/instance.sh config mycmd           # проверить, во что соберётся compose
scripts/instance.sh up mycmd               # поднять (docker compose -p cf-mycmd)
scripts/instance.sh logs mycmd
```

- У команды обязательно свой Telegram-бот: два инстанса на одном токене мешают друг другу.
- В `instances/mycmd/` лежат ещё `backgrounds/` (фоны Shorts), `mascot/` (пусто — ролики без маскота; маскот автора в них не попадает) и `pronunciation.txt` (как читать трудные слова, формат как в `assets/pronunciation.txt`).
- `instances/` в git не попадает (там токены и данные); в репозитории только `instances/_template/`.
- Площадки: `FEATURES=*` оставляет всё; `FEATURES=week,reddit` включает только перечисленное (`linkedin, habr, devto, reddit, week, talk, longvideo`). Telegram и YouTube управляются токенами.
- Напоминания про ручные шаги написаны под автора проекта; для команды в шаблоне `REMINDERS=false`.

## 3. YouTube
Нужен отдельный OAuth под аккаунт команды (`scripts/youtube_auth.py`). Пока приложение Google в режиме Testing, токен живёт 7 дней, а пользователей ≤100: для постоянной работы нужна верификация приложения.

## 4. Проверки после правок профиля

```
.venv/bin/python tests/golden_snapshot.py --check   # промпты и словари тем не изменились (кроме того, что правил ты)
bash tests/behavior_check.sh                        # поведение совпадает со старым коммитом
```
После намеренной правки текстов обнови эталон: `python tests/golden_snapshot.py` и посмотри `git diff tests/golden`.
