"""Private stdin/stdout protocol for the owner's supervised native helper.

This module is not an HTTP interface. The persistent worker handles one operation
at a time; standalone probes keep the one-shot protocol.
"""

import json
import sys
import time
from contextlib import nullcontext

from conty.adapters.base import AdapterError
from conty.adapters.macos.audio import AudioAdapter
from conty.adapters.macos.keyboard import KeyboardAdapter
from conty.adapters.macos.media import MediaAdapter
from conty.adapters.macos.music import MusicAdapter


def handle(payload, adapters):
    try:
        if "read_volume" in payload:
            result = {"ok": True, "volume": adapters["volume"].read()}
        elif "check" in payload:
            readiness = {}
            for kind in payload["check"]:
                check = adapters[kind].check()
                readiness[kind] = {
                    "available": bool(check["available"]),
                    "code": check.get("code"),
                }
            result = {"ok": True, "readiness": readiness}
        else:
            action = payload["action"]
            kind = action["type"]
            remaining = action["deadline_monotonic"] - time.monotonic()
            if remaining <= 0:
                raise AdapterError("timed_out", "Deadline expired before native execution")
            adapter = adapters[kind]
            if kind == "keyboard":
                target = action.get("target", {})
                adapter.execute(
                    action["key"],
                    app_id=target.get("app_id"),
                    focus=target.get("focus"),
                    modifiers=tuple(action["modifiers"]),
                    timeout=remaining,
                )
            elif kind == "media":
                target = action.get("target", "system")
                if target == {"app_id": "com.apple.Music"}:
                    adapter = adapters["music"]
                elif target != "system":
                    raise AdapterError("invalid_action", "Unsupported media target")
                adapter.execute(action["command"], timeout=remaining)
            else:
                adapter.execute(
                    action["operation"],
                    delta=action.get("delta"),
                    timeout=remaining,
                    value=action.get("value"),
                )
            if time.monotonic() >= action["deadline_monotonic"]:
                raise AdapterError("timed_out", "Native deadline expired", effects_possible=True)
            result = {"ok": True}
    except AdapterError as exc:
        result = {"ok": False, "code": exc.code, "effects_possible": exc.effects_possible}
    except Exception:
        result = {"ok": False, "code": "executor_unavailable", "effects_possible": True}
    return result


def main():
    adapters = {
        "keyboard": KeyboardAdapter(),
        "media": MediaAdapter(),
        "music": MusicAdapter(supervised=True),
        "volume": AudioAdapter(),
    }
    if "--persistent" not in sys.argv:
        payload = json.loads(sys.stdin.buffer.read(1024 * 1024 + 1))
        print(json.dumps(handle(payload, adapters)))
        return
    warm = sys.argv[sys.argv.index("--warm") + 1].split(",") if "--warm" in sys.argv else []
    for kind in warm:
        if kind in ("keyboard", "media", "volume"):
            try:
                adapters[kind].prepare()
            except AdapterError:
                # Missing optional dependencies remain ordinary capability errors.
                pass
    print(json.dumps({"ready": True}), flush=True)
    while line := sys.stdin.buffer.readline(1024 * 1024 + 1):
        if len(line) > 1024 * 1024 or not line.endswith(b"\n"):
            raise ValueError("Native request exceeds its frame bound")
        frame = json.loads(line)
        if (
            not isinstance(frame, dict)
            or set(frame) != {"id", "payload"}
            or type(frame["id"]) is not int
        ):
            raise ValueError("Invalid native request frame")
        objc = sys.modules.get("objc")
        with objc.autorelease_pool() if objc else nullcontext():
            result = handle(frame["payload"], adapters)
        print(json.dumps({"id": frame["id"], "result": result}), flush=True)


if __name__ == "__main__":
    main()
