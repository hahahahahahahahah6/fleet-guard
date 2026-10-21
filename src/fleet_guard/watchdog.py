"""Stall watchdog: log-quiet watched processes get snapshotted, then killed.

Register long-running agent processes with `fleet-guard watch add --pid … --log …`.
The daemon (or a cron tick of `watchdog-once`) checks each entry: if the
process is alive but its log file has not grown/changed for longer than the
stall timeout, capture a diagnostic snapshot and kill the whole process tree.
"""
from __future__ import annotations

import json
import os
import sys
import time

from . import config, diagnostics, ledger, procutil

WATCH_FILE = "watch.json"
DUMP_DIR = "dumps"


def _watch_path(cfg: dict) -> "Path":
    from pathlib import Path
    return config.state_dir(cfg) / WATCH_FILE


def _load_watch(cfg: dict) -> dict:
    try:
        from pathlib import Path  # noqa: F401  (kept local for clarity)
        data = json.loads(_watch_path(cfg).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}  # fail-open: corrupt state never blocks the watchdog


def _save_watch(entries: dict, cfg: dict) -> None:
    p = _watch_path(cfg)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2))
    os.replace(tmp, p)


def add(pid: int, log: str, timeout_s: float | None = None,
        session_id: str | None = None, label: str = "", cfg: dict | None = None) -> dict:
    cfg = cfg or config.load()
    entries = _load_watch(cfg)
    entry = {
        "pid": pid,
        "log": log,
        "added": time.time(),
        "timeout": timeout_s if timeout_s is not None else float(cfg["stall_timeout_s"]),
        "session_id": session_id,
        "label": label,
    }
    entries[str(pid)] = entry
    _save_watch(entries, cfg)
    ledger.append("watch.add", session_id, {"pid": pid, "log": log,
                                            "timeout_s": entry["timeout"]}, cfg)
    return entry


def remove(pid: int, cfg: dict | None = None) -> bool:
    cfg = cfg or config.load()
    entries = _load_watch(cfg)
    if str(pid) in entries:
        del entries[str(pid)]
        _save_watch(entries, cfg)
        return True
    return False


def list_entries(cfg: dict | None = None) -> list[dict]:
    cfg = cfg or config.load()
    return sorted(_load_watch(cfg).values(), key=lambda e: e["pid"])


def live_watched(cfg: dict | None = None) -> list[dict]:
    """Watched entries whose process is still alive (for the Stop hook)."""
    cfg = cfg or config.load()
    return [e for e in list_entries(cfg) if procutil.is_alive(e["pid"])]


def _log_mtime(entry: dict) -> float:
    try:
        return os.path.getmtime(entry["log"])
    except OSError:
        return float(entry.get("added", 0))  # no log yet: grace from add time


def check_once(cfg: dict | None = None, now: float | None = None) -> list[dict]:
    """One watchdog pass. Returns the list of actions taken (for tests/CLI)."""
    cfg = cfg or config.load()
    now = time.time() if now is None else now
    entries = _load_watch(cfg)
    actions: list[dict] = []
    changed = False
    for key, entry in list(entries.items()):
        pid = int(entry["pid"])
        if not procutil.is_alive(pid):
            del entries[key]
            changed = True
            ledger.append("watch.exit", entry.get("session_id"),
                          {"pid": pid, "label": entry.get("label", "")}, cfg)
            actions.append({"action": "deregister-exited", "pid": pid})
            continue
        idle_s = now - _log_mtime(entry)
        timeout = float(entry.get("timeout", cfg["stall_timeout_s"]))
        if idle_s > timeout:
            dump_dir = config.state_dir(cfg) / DUMP_DIR
            dump = diagnostics.snapshot(pid, dump_dir)
            result = procutil.kill_tree(pid, cfg["kill_grace_s"])
            del entries[key]
            changed = True
            ledger.append("watchdog.stall-kill", entry.get("session_id"), {
                "pid": pid, "label": entry.get("label", ""),
                "idle_s": round(idle_s, 1), "timeout_s": timeout,
                "dump": str(dump) if dump else None,
                "sigkilled": result["sigkilled"],
            }, cfg)
            actions.append({"action": "stall-kill", "pid": pid,
                            "idle_s": round(idle_s, 1),
                            "dump": str(dump) if dump else None})
    if changed:
        _save_watch(entries, cfg)
    return actions


def daemon(interval_s: float | None = None, cfg: dict | None = None) -> int:
    cfg = cfg or config.load()
    interval = interval_s if interval_s is not None else float(cfg["watch_interval_s"])
    print(f"fleet-guard watchdog running (interval {interval}s). Ctrl-C to stop.",
          file=sys.stderr)
    try:
        while True:
            for action in check_once(cfg):
                print(f"fleet-guard watchdog: {action}", file=sys.stderr)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("fleet-guard watchdog stopped.", file=sys.stderr)
        return 0
