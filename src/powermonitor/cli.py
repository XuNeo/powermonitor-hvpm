"""Dispatch desktop and terminal modes without loading Qt for live sampling."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments[:1] == ["live"]:
        from powermonitor.live import main as live_main

        return live_main(arguments[1:])
    from powermonitor.app import main as desktop_main

    return desktop_main(arguments)
