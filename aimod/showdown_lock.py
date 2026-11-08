"""A tiny shared lock between tools\aggression_probe.py --showdown and AIMOD's racing intensity.

Two programs writing the same aggression value fight (live 2026-11-05 Richmond: AIMOD wrote the rival cap 3.0 to the
pair, the probe wrote its step, each took the other's write for a game re-roll and the test ended after 1 s). While
the probe holds its cars it writes this file and refreshes it every second; AIMOD gives those cars their own values
back and leaves them alone until the file is gone or stale."""
from __future__ import annotations

import json
import os
import time

FRESH_S = 4.0            # a lock not refreshed for this long is dead (the probe crashed or was closed)


def lock_path() -> str:
    env = os.environ.get("AIMOD_SHOWDOWN_LOCK")
    if env:
        return env
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "AIMOD", "showdown.lock")


def write(numbers) -> bool:
    """create / refresh the lock for these car numbers"""
    path = lock_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"cars": [int(n) for n in numbers], "pid": os.getpid(), "t": time.time()}, f)
        return True
    except OSError:
        return False


def read(now: float | None = None) -> set[int]:
    """the car numbers a live probe holds (empty: no lock, a stale one, or an unreadable one)"""
    path = lock_path()
    try:
        if (now if now is not None else time.time()) - os.path.getmtime(path) > FRESH_S:
            return set()
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {int(n) for n in data.get("cars", ())}
    except (OSError, ValueError, TypeError, AttributeError):
        return set()


def remove():
    try:
        os.remove(lock_path())
    except OSError:
        pass
