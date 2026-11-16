"""Сборка и санитайзинг HTML под Telegram Bot API (parse_mode=HTML).

LLM иногда выдаёт <br>, <p>, <h2>, <ul> или незакрытые теги — Telegram на таком падает
с "can't parse entities". Поэтому текст прогоняется через парсер, который оставляет
только поддерживаемые теги и гарантирует корректную вложенность.
"""

import html
import re
from html.parser import HTMLParser

from .schemas import TelegramStream

TG_TEXT_LIMIT = 4096

_ALLOWED = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "blockquote", "tg-spoiler", "span"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []

    def _open(self, tag: str, attrs: str = "") -> None:
        self.out.append(f"<{tag}{attrs}>")
        self.stack.append(tag)

    def _close(self, tag: str) -> None:
        if tag not in self.stack:
            return
        while self.stack:
            top = self.stack.pop()
            self.out.append(f"</{top}>")
            if top == tag:
                break

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "br":
            self.out.append("\n")
        elif tag in _HEADINGS:
            self._open("b")
        elif tag == "li":
            self.out.append("\n• ")
        elif tag == "a":
            href = a.get("href") or ""
            if href.startswith(("http://", "https://", "tg://")):
                self._open("a", f' href="{html.escape(href, quote=True)}"')
        elif tag == "code" and "pre" in self.stack:
            cls = a.get("class") or ""
            attr = f' class="{html.escape(cls, quote=True)}"' if cls.startswith("language-") else ""
            self._open("code", attr)
        elif tag == "blockquote":
            self._open("blockquote", " expandable" if "expandable" in a else "")
        elif tag == "span":
            if a.get("class") == "tg-spoiler":
                self._open("span", ' class="tg-spoiler"')
        elif tag in _ALLOWED:
            self._open(tag)

    def handle_endtag(self, tag):
        if tag in _HEADINGS:
            self._close("b")
            self.out.append("\n")
        elif tag == "p":
            self.out.append("\n\n")
        elif tag in _ALLOWED:
            self._close(tag)

    def handle_data(self, data):
        self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        text = "".join(self.out)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def sanitize(text: str) -> str:
    p = _Sanitizer()
    p.feed(text)
    p.close()
    return p.result()


def visible_length(html_text: str) -> int:
    """Длина текста так, как её считает Telegram (без тегов)."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", html_text)))


def build_post(tg: TelegramStream) -> str:
    parts = [sanitize(tg.formatted_text)]
    code = tg.code_snippet.strip()
    if code:
        lang = re.sub(r"[^a-z0-9+#-]", "", tg.code_language.lower())
        cls = f' class="language-{lang}"' if lang else ""
        parts.append(f"<pre><code{cls}>{html.escape(code, quote=False)}</code></pre>")
    if tg.cta_text.strip():
        parts.append(sanitize(tg.cta_text))
    tags = " ".join(t if t.startswith("#") else f"#{t}" for t in tg.hashtags if t.strip())
    if tags:
        parts.append(html.escape(tags, quote=False))
    return "\n\n".join(p for p in parts if p)


def escape(text: str) -> str:
    return html.escape(text, quote=False)


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
