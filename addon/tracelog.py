"""Append-only trace of what the voice loop actually did, written next to the add-on.

Exists because 'nothing happens' is not a diagnosis. The dialog is ephemeral and a raise on a
background thread can vanish, so the same symptom has covered a stale config, a swallowed
exception, and a phase that ignored a command — three different bugs that looked identical from
the outside. This turns each into a line you can read.

Off unless a `debug` file sits beside it, so it costs nothing in normal use.
"""

from __future__ import annotations

import os
import time

_LOG_PATH = os.path.join(os.path.dirname(__file__), "trace.log")
_FLAG_PATH = os.path.join(os.path.dirname(__file__), "debug")
_enabled: bool | None = None


def enabled() -> bool:
    global _enabled
    if _enabled is None:
        _enabled = os.path.exists(_FLAG_PATH)
    return _enabled


def reset() -> None:
    """Start a fresh log for a new session, so one run is one readable file."""
    if not enabled():
        return
    try:
        with open(_LOG_PATH, "w") as fh:
            fh.write(f"=== session start {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    except OSError:
        pass


def write(event: str, detail: str = "") -> None:
    if not enabled():
        return
    try:
        with open(_LOG_PATH, "a") as fh:
            stamp = time.strftime("%H:%M:%S")
            fh.write(f"{stamp}  {event}{('  ' + detail) if detail else ''}\n")
    except OSError:
        pass
