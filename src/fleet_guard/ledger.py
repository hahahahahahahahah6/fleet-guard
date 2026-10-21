"""Append-only JSONL ledger. Every guard action lands here.

One JSON object per line, fsync'ed on write. The ledger is append-only by
convention: fleet-guard itself never rewrites or truncates it.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import config


def path(cfg: dict | None = None) -> Path:
    return config.state_dir(cfg) / "ledger.jsonl"


def append(event: str, session_id: str | None = None, details: dict | None = None,
           cfg: dict | None = None) -> dict:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "session_id": session_id,
        "details": details or {},
    }
    p = path(cfg)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return record


def tail(n: int = 20, cfg: dict | None = None) -> list[dict]:
    p = path(cfg)
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records = []
    for line in lines[-n:]:
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records
