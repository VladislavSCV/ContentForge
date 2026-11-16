"""Промпты берутся из профиля контента: profiles/<имя>/prompts/*.md (см. app/profiles.py).

Имена констант прежние, чтобы остальной код не менялся. {brand_name} и {tg_channel_link} в текстах подставляются здесь.
"""

from . import profiles

_p = profiles.current()

SYSTEM_PROMPT = _p.prompt("system")
TG_REVIEW_PROMPT = _p.prompt("tg_review")
TG_EDIT_PROMPT = _p.prompt("tg_edit")
SCRIPT_PROMPT = _p.prompt("script")
STORY_SCRIPT_PROMPT = _p.prompt("story_script")
SCRIPT_REVIEW_PROMPT = _p.prompt("script_review")
LINKEDIN_PROMPT = _p.prompt("linkedin")
HABR_PLAN_PROMPT = _p.prompt("habr_plan")
HABR_SECTION_PROMPT = _p.prompt("habr_section")
LINKEDIN_PROMPT_EN = _p.prompt("linkedin_en")
