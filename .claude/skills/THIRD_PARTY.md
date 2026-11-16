# Сторонние навыки (скачаны 2026-11-06, прочитаны до установки)

Положены вручную, без `npx skills add` и без глобальной установки. Работают только в этом проекте.
Обновлять вручную: скачать новый тег, заново проверить (скрытые символы, хуки, `allowed-tools`, сеть, `eval`, `subprocess`).

| Навыки | Источник | Версия | Лицензия | SHA-256 архива |
|---|---|---|---|---|
| social, content-strategy, copywriting, customer-research, product-marketing | github.com/coreyhaines31/marketingskills | тег v2.14.0, коммит 5ffe077d2e59486d91c5ccaecbd74b6973c32308 | MIT (`_third_party_licenses/marketingskills-LICENSE`) | 8ed20eb1773c5202b0b9705f79bbc2b3d78494a5c7e68840ae2265336a9d3a8e |
| ui-ux-pro-max | github.com/nextlevelbuilder/ui-ux-pro-max-skill | тег v2.15.0, коммит a38d04c3d5c298c851dbe5e6ee1965ee3de42cb5 | MIT в LICENSE; на npm замечена CC-BY-NC-4.0, выяснить до коммерческого использования | 004a5e7814ae5921b2ea9983c6e7635b8513b8f39cc2ed39a2d32113da9ae1fd |

## Что не взято
- marketingskills: папки `evals` и остальные 48 навыков.
- ui-ux-pro-max: `validate_data.py`, `scripts/tests`, а также навыки ui-styling, design, design-system, brand, slides, banner-design (в них скрипты с сетью, `npx` и ключами API).

## Что изменено локально
- ui-ux-pro-max/SKILL.md: `"${CLAUDE_PLUGIN_ROOT}/.claude/skills/ui-ux-pro-max/scripts/search.py"` заменено на `".claude/skills/ui-ux-pro-max/scripts/search.py"` (переменной плагина в папке проекта нет).

## Что известно о поведении
- Python-скрипты ui-ux-pro-max: только стандартная библиотека, без сети. Читают COLORTERM; с `--persist` пишут `design-system/`, перезаписывают только с `--force` (по правилу навыка без явного разрешения автора не использовать).
- social/references/listening.md содержит рецепты `curl` к публичным API (Reddit, Hacker News, Bluesky): без нужды не запускать.
- Правило навыка social: ничего не публиковать автоматически без явного разрешения.
