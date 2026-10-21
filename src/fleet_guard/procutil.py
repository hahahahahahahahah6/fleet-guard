"""Process inspection and tree-kill utilities. Stdlib only.

Linux uses /proc directly (fast, no subprocess). Other platforms fall back to
`ps`. Environment-marker reads only work on Linux; elsewhere they return None
and attribution-based features degrade gracefully (fail-open).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

_IS_LINUX = sys.platform.startswith("linux")


def _iter_proc_linux() -> list[dict]:
    procs = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                stat = fh.read()
            # comm is parenthesised and may contain spaces/parens: split at last ')'
            rparen = stat.rfind(b")")
            parts = stat[rparen + 2:].split()
            ppid = int(parts[1])
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmdline = fh.read().replace(b"\0", b" ").decode(errors="replace").strip()
            procs.append({"pid": pid, "ppid": ppid, "cmdline": cmdline})
        except (OSError, ValueError, IndexError):
            continue  # process vanished mid-scan
    return procs


def _iter_proc_ps() -> list[dict]:
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    procs = []
    for line in out.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        try:
            procs.append({
                "pid": int(parts[0]),
                "ppid": int(parts[1]),
                "cmdline": parts[2] if len(parts) > 2 else "",
            })
        except ValueError:
            continue
    return procs


def iter_processes() -> list[dict]:
    """Return [{pid, ppid, cmdline}] for visible processes."""
    if _IS_LINUX:
        try:
            return _iter_proc_linux()
        except OSError:
            pass
    return _iter_proc_ps()


def children_map(procs: list[dict] | None = None) -> dict[int, list[int]]:
    cmap: dict[int, list[int]] = {}
    for p in procs or iter_processes():
        cmap.setdefault(p["ppid"], []).append(p["pid"])
    return cmap


def subtree_pids(root: int, cmap: dict[int, list[int]] | None = None) -> list[int]:
    """root + all descendants, children before parents (safe kill order)."""
    cmap = cmap if cmap is not None else children_map()
    ordered: list[int] = []
    stack = [root]
    while stack:
        pid = stack.pop()
        ordered.append(pid)
        stack.extend(cmap.get(pid, []))
    ordered.reverse()
    return ordered


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours
    return True


def read_environ(pid: int) -> dict[str, str] | None:
    """Process environment as a dict. Linux-only; None elsewhere/unreadable."""
    if not _IS_LINUX:
        return None
    try:
        with open(f"/proc/{pid}/environ", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    env: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if b"=" in item:
            k, v = item.split(b"=", 1)
            env[k.decode(errors="replace")] = v.decode(errors="replace")
    return env


def procs_with_env(key: str, value: str | None = None) -> list[dict]:
    """Processes carrying an environment marker (excludes this process)."""
    me = os.getpid()
    hits = []
    for p in iter_processes():
        if p["pid"] == me:
            continue
        env = read_environ(p["pid"])
        if not env or key not in env:
            continue
        if value is not None and env[key] != value:
            continue
        hits.append(p)
    return hits


def kill_tree(root: int, grace_s: float = 5.0) -> dict:
    """SIGTERM a whole process tree (children first), then SIGKILL survivors."""
    cmap = children_map()
    targets = [p for p in subtree_pids(root, cmap) if is_alive(p)]
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + max(0.0, grace_s)
    while time.monotonic() < deadline:
        targets = [p for p in targets if is_alive(p)]
        if not targets:
            break
        time.sleep(0.2)
    killed = []
    for pid in targets:
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except (ProcessLookupError, PermissionError):
            pass
    # reap zombies we own
    for pid in killed:
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass
    return {"signalled": targets, "sigkilled": killed}
