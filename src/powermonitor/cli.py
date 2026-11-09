"""Dispatch desktop and terminal modes without loading Qt for live sampling."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments[:1] == ["live"]:
        from powermonitor.live import main as live_main

        return live_main(arguments[1:])
    commands = {"scan", "status", "output", "capture", "info", "stats", "export"}
    if arguments and arguments[0] in commands:
        from powermonitor.commands import main as command_main

        return command_main(arguments)
    if arguments in (["--help"], ["-h"]):
        print("Commands: scan, status, output on/off, live, capture, info, stats, export")
        print("Use powermonitor COMMAND --help. No command opens the desktop UI.")
        print("Desktop options: --demo, --open PATH, --screenshot PATH")
        return 0
    if arguments and not arguments[0].startswith("--"):
        print(f"Unknown command: {arguments[0]}", file=sys.stderr)
        return 2
    from powermonitor.app import main as desktop_main

    return desktop_main(arguments)
