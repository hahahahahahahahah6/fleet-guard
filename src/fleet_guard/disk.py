"""Disk guard: alert (and optionally quarantine) runaway agent scratch dirs."""
from __future__ import annotations

import os
import shutil
import sys
import time

from . import config, ledger


def du_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda *a: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def _fmt(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def check(cfg: dict | None = None) -> list[dict]:
    """Check scratch dirs. Returns alert dicts; alerts also go to the ledger."""
    cfg = cfg or config.load()
    alerts: list[dict] = []
    max_bytes = int(cfg["disk_max_bytes"])
    min_free = int(cfg["disk_min_free_bytes"])
    for raw in cfg["scratch_dirs"]:
        path = os.path.expandvars(os.path.expanduser(str(raw)))
        if not os.path.isdir(path):
            continue
        usage = du_bytes(path)
        try:
            free = shutil.disk_usage(path).free
        except OSError:
            free = None
        alert: dict | None = None
        if usage > max_bytes:
            alert = {"dir": path, "kind": "over-limit",
                     "usage_bytes": usage, "limit_bytes": max_bytes}
            if cfg["disk_quarantine"]:
                dest = f"{path}.quarantined-{int(time.time())}"
                try:
                    shutil.move(path, dest)
                    alert["quarantined_to"] = dest
                except OSError as exc:
                    alert["quarantine_error"] = str(exc)
        elif free is not None and free < min_free:
            alert = {"dir": path, "kind": "low-free",
                     "free_bytes": free, "min_free_bytes": min_free}
        if alert:
            alerts.append(alert)
            ledger.append("disk.alert", None, alert, cfg)
            print(f"fleet-guard disk alert: {path} {_fmt(usage)} "
                  f"({alert['kind']})", file=sys.stderr)
    return alerts
