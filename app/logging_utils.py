from __future__ import annotations

from pathlib import Path


def append_log_line(path: Path, line: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip("\n"))
            handle.write("\n")
        return True
    except OSError:
        return False


def append_log_block(path: Path, text: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text.rstrip("\n"))
            handle.write("\n")
        return True
    except OSError:
        return False
