"""Entrypoint: run the JARVIS API server."""

from __future__ import annotations

import atexit
import os

import uvicorn

from .config import DATA_DIR, store

_PID_PATH = DATA_DIR / "jarvis.pid"


def _write_pid() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _PID_PATH.write_text(str(os.getpid()), encoding="utf-8")


def _clear_pid() -> None:
    try:
        if _PID_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
            _PID_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def main() -> None:
    _write_pid()
    atexit.register(_clear_pid)
    cfg = store.get()
    uvicorn.run("jarvis.main:app", host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
