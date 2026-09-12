"""Shared terminal presentation for interactive setup; plain output for automation."""
from __future__ import annotations

import builtins
import os
from contextlib import contextmanager
from functools import wraps

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

_plain = False


def configure(*, plain=False):
    global _plain
    _plain = plain


def console():
    return Console(no_color=_plain or "NO_COLOR" in os.environ, markup=False, highlight=False)


def interactive(display):
    return display.is_terminal and not _plain and "NO_COLOR" not in os.environ and os.getenv("TERM") != "dumb"


def message(value=""):
    display = console()
    text = str(value)
    if not interactive(display):
        builtins.print(text)
        return
    lowered = text.lower()
    style = "default"
    prefix = "  "
    if lowered.startswith(("validated", "received", "created ", "installed ", "setup saved", "cloudflare setup saved", "refresh token and")):
        style, prefix = "green", "  ✓ "
    elif lowered.startswith(("no token", "cancelled", "cloudflare setup could not", "discord returned", "setup stopped", "cloudflare setup is unfinished")):
        style, prefix = "yellow", "  ! "
    elif text.startswith("https://"):
        style, prefix = "cyan", "  ↗ "
    display.print(Text(prefix+text, style=style))


def ask(prompt):
    display = console()
    if not interactive(display):
        return builtins.input(prompt)
    display.print(Text("  ? "+prompt, style="bold cyan"), end="")
    return builtins.input("")


def choices(labels):
    display = console()
    for index, label in enumerate(labels, 1):
        if interactive(display):
            line = Text(f"    {index}  ", style="bold cyan")
            line.append(str(label), style="default")
            display.print(line)
        else:
            message(f"  {index}. {label}")


def banner(title, subtitle):
    display = console()
    if not interactive(display):
        message(title+" — "+subtitle)
        return
    display.print()
    body = Text(title+"\n", style="bold cyan")
    body.append(subtitle, style="dim")
    display.print(Panel(body, border_style="cyan", padding=(1, 2), expand=False))


def step(title, number=None, total=None):
    label = f"{number}/{total}  {title}" if number is not None else title
    display = console()
    if not interactive(display):
        message("\n"+label)
        return
    display.print()
    display.rule(Text(label, style="bold cyan"), align="left", style="bright_black")


@contextmanager
def status(label):
    display = console()
    if interactive(display):
        with display.status(Text(label, style="cyan"), spinner="dots", spinner_style="cyan"):
            yield
    else:
        message(label+"...")
        yield


def run(label, function, *args, **kwargs):
    with status(label):
        return function(*args, **kwargs)


def busy(label):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            return run(label, function, *args, **kwargs)
        return wrapped
    return decorate


def summary(rows):
    display = console()
    body = Text()
    for name, value in rows:
        body.append(name+": ", style="bold")
        body.append(str(value)+"\n")
    if interactive(display):
        display.print(Panel(body, title="Setup saved", border_style="green", expand=False))
    else:
        message("Setup saved")
        message(body.plain.rstrip())
