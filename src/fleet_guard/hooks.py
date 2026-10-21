"""Claude Code hook entry points. JSON in on stdin, JSON out on stdout.

  fleet-guard hook pretooluse   # PreToolUse: block when bg-process quota breached
  fleet-guard hook sessionend    # SessionEnd: reap leaked session children
  fleet-guard hook stop          # Stop: warn when too many watched procs are busy

Everything here is fail-open: any exception prints a note to stderr and exits
0 so the agent is never taken down by its own guard.
"""
from __future__ import annotations

import json
import sys

from . import config, ledger, procutil
from .watchdog import live_watched


def _read_input() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        return {}


def _block(reason: str) -> int:
    json.dump({"decision": "block", "reason": reason}, sys.stdout)
    sys.stdout.write("\n")
    return 0


def pretooluse(data: dict, cfg: dict) -> int:
    """Count agent background processes machine-wide; block over the cap."""
    cap = int(cfg["bg_quota"])
    running = procutil.procs_with_env(config.ENV_MARKER)
    if len(running) >= cap:
        sample = ", ".join(
            f"pid {p['pid']} ({p['cmdline'][:60]})" for p in running[:5]
        )
        reason = (
            f"fleet-guard: {len(running)} agent background processes already running "
            f"machine-wide (cap {cap}, default = half your CPU cores). "
            f"Wait for one to finish or stop it before starting another. "
            f"Running: {sample}"
            + (" ..." if len(running) > 5 else "")
        )
        ledger.append("hook.pretooluse-block", data.get("session_id"),
                      {"running": len(running), "cap": cap,
                       "pids": [p["pid"] for p in running]}, cfg)
        return _block(reason)
    return 0


def sessionend(data: dict, cfg: dict) -> int:
    """Reap process trees the session left behind."""
    session_id = str(data.get("session_id") or "")
    roots = []
    if session_id:
        roots = procutil.procs_with_env(config.ENV_MARKER, session_id)
    else:
        # no session id: still try to catch this process's own children later;
        # for now just scan for any marker (they are all agent-attributed)
        roots = procutil.procs_with_env(config.ENV_MARKER)
    reaped: list[int] = []
    for p in roots:
        result = procutil.kill_tree(p["pid"], cfg["kill_grace_s"])
        reaped.append(p["pid"])
        ledger.append("sessionend.reap", session_id or None,
                      {"root_pid": p["pid"], "cmdline": p["cmdline"][:120],
                       "sigkilled": result["sigkilled"]}, cfg)
    if reaped:
        print(f"fleet-guard: reaped {len(reaped)} leaked process tree(s): "
              f"{reaped}", file=sys.stderr)
    return 0


def stop(data: dict, cfg: dict) -> int:
    """Warn when more than `busy_alert_threshold` watched processes are busy."""
    busy = live_watched(cfg)
    threshold = int(cfg["busy_alert_threshold"])
    if len(busy) > threshold:
        ledger.append("hook.stop-busy-alert", data.get("session_id"),
                      {"busy": len(busy), "threshold": threshold,
                       "pids": [e["pid"] for e in busy]}, cfg)
        print(
            f"fleet-guard: {len(busy)} watched background processes still busy "
            f"(>{threshold}). They may be hung — check `fleet-guard watch list`.",
            file=sys.stderr,
        )
    return 0


def main(argv: list[str]) -> int:
    try:
        if len(argv) < 2:
            print("usage: fleet-guard hook {pretooluse|sessionend|stop}",
                  file=sys.stderr)
            return 2
        which = argv[1].lower()
        data = _read_input()
        cfg = config.load()
        if which == "pretooluse":
            return pretooluse(data, cfg)
        if which == "sessionend":
            return sessionend(data, cfg)
        if which == "stop":
            return stop(data, cfg)
        print(f"unknown hook: {which}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 — fail-open by design
        print(f"fleet-guard hook failed open ({exc}); allowing action.",
              file=sys.stderr)
        return 0
