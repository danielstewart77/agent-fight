"""Jinja2 templates singleton."""

from __future__ import annotations

from pathlib import Path

import bleach
from fastapi.templating import Jinja2Templates
from markdown import markdown
from markupsafe import Markup

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_MARKDOWN_TAGS = {
    "a", "blockquote", "br", "code", "del", "em", "h1", "h2", "h3",
    "h4", "h5", "h6", "hr", "li", "ol", "p", "pre", "strong", "ul",
}


def markdown_html(value: str | None) -> Markup:
    """Render persisted Markdown while preventing stored HTML/script injection."""
    rendered = markdown(value or "", extensions=["fenced_code", "sane_lists"])
    sanitized = bleach.clean(
        rendered,
        tags=_MARKDOWN_TAGS,
        attributes={"a": ["href", "title"]},
        protocols={"http", "https", "mailto"},
        strip=True,
    )
    return Markup(sanitized)


templates.env.filters["markdown"] = markdown_html
