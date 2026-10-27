"""Owner-local configuration, dashboard service and host-control diagnostics."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence

from conty.adapters.base import AdapterError
from conty.adapters.macos.audio import AudioAdapter
from conty.adapters.macos.keyboard import SUPPORTED_KEYS, KeyboardAdapter
from conty.adapters.macos.music import MusicAdapter
from conty.adapters.process import ProcessAdapter


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Conty: YAML-driven host-control dashboard and local diagnostics"
    )
    commands = root.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Validate a dashboard without native effects")
    validate.add_argument("config")
    serve = commands.add_parser("serve", help="Serve a paired browser dashboard")
    serve.add_argument("config")
    doctor = commands.add_parser(
        "doctor", help="Check capabilities and request missing Accessibility access"
    )
    doctor.add_argument(
        "--no-prompt",
        action="store_true",
        help="Check only; do not request Accessibility permission",
    )
    keyboard = commands.add_parser("keyboard", help="Dispatch one key to a running application")
    keyboard.add_argument("key", choices=SUPPORTED_KEYS, help="Physical ANSI key, not Unicode text")
    keyboard.add_argument(
        "--app-id", help="Running application bundle ID; omit to use the frontmost application"
    )
    keyboard.add_argument(
        "--focus",
        choices=["activate", "require_frontmost"],
        help="Requires --app-id; defaults to activate for an explicit target",
    )
    keyboard.add_argument(
        "--modifier", action="append", choices=["shift", "control", "option", "command"], default=[]
    )
    keyboard.add_argument("--timeout", type=float, default=5)
    music = commands.add_parser("music", help="Send one command to already-running Apple Music")
    music.add_argument("operation", choices=["previous_track", "play_pause", "next_track"])
    music.add_argument("--timeout", type=float, default=5)
    volume = commands.add_parser("volume", help="Change the selected system output device")
    volume.add_argument("operation", choices=["adjust", "toggle_mute"])
    volume.add_argument("--delta", type=int)
    process = commands.add_parser(
        "process", help="Run a local executable, without an implicit shell"
    )
    process.add_argument("--timeout", type=float, default=10)
    process.add_argument("--cwd")
    process.add_argument("--env-from", action="append", default=[], metavar="CHILD=HOST")
    process.add_argument("argv", nargs=argparse.REMAINDER, help="-- /absolute/executable arg ...")
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command in ("validate", "serve"):
            from conty.config import ConfigurationError, load_config

            try:
                if args.command == "validate":
                    load_config(args.config)
                    print("Configuration is valid")
                else:
                    from conty.service import serve

                    serve(args.config)
            except ConfigurationError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            return 0
        if args.command == "doctor":
            report = {
                "python": sys.executable,
                "pid": os.getpid(),
                "platform": sys.platform,
                "permission_identity": "Launch-method dependent: macOS may attribute access to the launching terminal/editor (such as Terminal, iTerm, or Zed), Python, or the osascript helper; the Python path alone does not identify which app to authorize",
                "keyboard": KeyboardAdapter().check(request_permission=not args.no_prompt),
                "music": MusicAdapter().check(),
                "audio": AudioAdapter().check(),
            }
            print(json.dumps(report, indent=2))
            return (
                0
                if all(report[name]["available"] for name in ("keyboard", "music", "audio"))
                else 1
            )
        if args.command == "keyboard":
            result = KeyboardAdapter().execute(
                args.key,
                app_id=args.app_id,
                focus=args.focus,
                modifiers=tuple(args.modifier),
                timeout=args.timeout,
            )
        elif args.command == "music":
            result = MusicAdapter().execute(args.operation, timeout=args.timeout)
        elif args.command == "volume":
            result = AudioAdapter().execute(args.operation, delta=args.delta)
        else:
            env_from = {}
            for mapping in args.env_from:
                destination, separator, source = mapping.partition("=")
                if not separator or destination in env_from:
                    raise AdapterError(
                        "invalid_action", "Use unique --env-from CHILD=HOST mappings"
                    )
                env_from[destination] = source
            arguments = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
            result = asyncio.run(
                ProcessAdapter().execute(
                    arguments, timeout=args.timeout, cwd=args.cwd, env_from=env_from
                )
            )
        print(
            json.dumps(
                {"status": "succeeded", "message": result.message, "details": result.details},
                indent=2,
            )
        )
        return 0
    except AdapterError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "code": exc.code,
                    "message": str(exc),
                    "effects_possible": exc.effects_possible,
                    "details": exc.details,
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("Interrupted; any dispatched native effects cannot be undone.", file=sys.stderr)
        return 130
