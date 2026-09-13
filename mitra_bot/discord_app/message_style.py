"""Shared presentation for Discord replies; delivery and permissions stay with callers."""
from __future__ import annotations

import discord


COLORS = {"info": 0x5865F2, "success": 0x57F287, "warning": 0xFEE75C, "error": 0xED4245}
ICONS = {"info": "", "success": "✅ ", "warning": "⚠️ ", "error": "❌ "}


def notice(title: str, body: str | None = None, *, tone: str = "info") -> str:
    """A compact heading and separate body, suitable for public or private replies."""
    heading = f"### {ICONS[tone]}{title}"
    return heading + (f"\n\n{body}" if body else "")


def pages(title: str, sections, *, limit: int = 2000):
    """Pack complete paragraphs into messages without splitting normal fields."""
    heading = notice(title) + "\n\n"
    budget = limit - len(heading)
    if budget < 1:
        raise ValueError("Message heading exceeds the message limit")
    body = ""
    for section in sections:
        section = str(section)
        while len(section) > budget:
            if body:
                yield heading + body
                body = ""
            split = section.rfind("\n", 0, budget + 1)
            split = split if split > 0 else budget
            yield heading + section[:split]
            section = section[split:].lstrip("\n")
        if body and len(body) + len(section) + 2 > budget:
            yield heading + body
            body = ""
        body += ("\n\n" if body else "") + section
    if body:
        yield heading + body


def embed(*, title=None, description=None, color=None, **kwargs) -> discord.Embed:
    """Use one accent palette while retaining caller-supplied semantic colors."""
    palette = {
        discord.Color.blue().value: COLORS["info"],
        discord.Color.blurple().value: COLORS["info"],
        discord.Color.teal().value: COLORS["info"],
        discord.Color.green().value: COLORS["success"],
        discord.Color.red().value: COLORS["error"],
        discord.Color.orange().value: COLORS["warning"],
        discord.Color.gold().value: COLORS["warning"],
    }
    value = color.value if isinstance(color, discord.Color) else color
    result = discord.Embed(title=title, description=description,
                           color=palette.get(value, value if value is not None else COLORS["info"]), **kwargs)
    result.set_footer(text="Mitra")
    return result
