"""Install loguru sinks: structured JSONL error log to .logs/parser-errors.jsonl
and keep the default stdout sink for local dev.

Imported once at process startup (from api_server.lifespan).
"""
import json
import os
import sys
from pathlib import Path
from typing import Optional

from loguru import logger

_INSTALLED = False


def install(log_dir: Optional[str] = None) -> None:
    """Add a JSONL sink that only captures WARNING+. Idempotent."""
    global _INSTALLED
    if _INSTALLED:
        return

    base = Path(log_dir) if log_dir else Path(__file__).resolve().parents[1] / ".logs"
    base.mkdir(parents=True, exist_ok=True)
    log_file = base / "parser-errors.jsonl"

    def sink(message):
        # message.record carries {time, level, file, function, line, message, extra, exception}
        rec = message.record
        entry = {
            "ts": rec["time"].isoformat(),
            "source": "parser",
            "level": rec["level"].name.lower(),
            "logger": rec["name"],
            "function": rec["function"],
            "file": f"{rec['file'].name}:{rec['line']}",
            "msg": rec["message"],
        }
        extra = rec.get("extra") or {}
        if extra:
            entry["extra"] = {k: str(v)[:500] for k, v in extra.items()}
        if rec.get("exception"):
            # Exception is a RecordException — turn into readable trace.
            exc = rec["exception"]
            entry["exception"] = {
                "type": exc.type.__name__ if exc.type else None,
                "value": str(exc.value)[:500] if exc.value else None,
            }
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with log_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            # Never let the logger crash the request.
            pass

    logger.add(sink, level="WARNING", enqueue=False)
    _INSTALLED = True
    logger.info(f"[logging] JSONL sink installed: {log_file}")
