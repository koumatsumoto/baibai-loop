from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


def load_project_env(start: Path | None = None) -> Path | None:
    base = (start or Path.cwd()).resolve()
    for candidate in (base, *base.parents):
        dotenv = candidate / ".env"
        if dotenv.is_file():
            load_dotenv(dotenv, override=False)
            return dotenv
    return None
