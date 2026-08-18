"""Entrypoint: run the JARVIS API server."""

from __future__ import annotations

import uvicorn

from .config import store


def main() -> None:
    cfg = store.get()
    uvicorn.run("jarvis.main:app", host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
