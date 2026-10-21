"""Diagnostic snapshot for a stalled process.

Captures everything the stdlib can reach from outside the target process on
Linux: cmdline, status, kernel stack (/proc/<pid>/stack), wait channel, IO
counters, open FD count. This is a *kernel-level* snapshot — a true in-process
Python thread dump (e.g. via py-spy or gdb) is out of scope for a stdlib-only
guard, and the README says so plainly.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import procutil


def _read(path: str) -> str | None:
    try:
        with open(path, errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def snapshot(pid: int, outdir: str | Path) -> Path | None:
    """Write a diagnostic snapshot for pid; return the file path (None on failure)."""
    outdir = Path(outdir)
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    data: dict = {
        "pid": pid,
        "captured_at": time.time(),
        "platform": "linux" if procutil._IS_LINUX else "other",
    }
    if procutil._IS_LINUX:
        data.update({
            "cmdline": _read(f"/proc/{pid}/cmdline"),
            "status": _read(f"/proc/{pid}/status"),
            "wchan": _read(f"/proc/{pid}/wchan"),
            "kernel_stack": _read(f"/proc/{pid}/stack"),
            "io": _read(f"/proc/{pid}/io"),
        })
        try:
            data["open_fds"] = len(os.listdir(f"/proc/{pid}/fd"))
        except OSError:
            data["open_fds"] = None
        for link in ("exe", "cwd"):
            try:
                data[link] = os.readlink(f"/proc/{pid}/{link}")
            except OSError:
                data[link] = None
    else:
        data["ps"] = next(
            (p for p in procutil.iter_processes() if p["pid"] == pid), None
        )
    dest = outdir / f"stall-{pid}-{int(time.time())}.json"
    try:
        dest.write_text(json.dumps(data, indent=2, default=str))
    except OSError:
        return None
    return dest
