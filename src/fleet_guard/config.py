"""Configuration: defaults < ~/.fleet-guard/config.json < FLEET_GUARD_* env vars."""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {
    # where ledger.jsonl / watch.json / config.json live
    "state_dir": "~/.fleet-guard",
    # max agent background processes machine-wide (default: half the CPU cores)
    "bg_quota": max(1, (os.cpu_count() or 2) // 2),
    # Stop hook warns when more than this many watched processes are still busy
    "busy_alert_threshold": 4,
    # watchdog: kill a watched process whose log went quiet for this long
    "stall_timeout_s": 600,
    "watch_interval_s": 30,
    # SIGTERM -> SIGKILL grace period when killing a process tree
    "kill_grace_s": 5,
    # agent scratch dirs to watch, e.g. ["~/.claude/scratch", "/tmp/agent-work"]
    "scratch_dirs": [],
    # alert when a scratch dir exceeds this size
    "disk_max_bytes": 10 * 1024**3,
    # alert when a scratch dir's filesystem has less free space than this
    "disk_min_free_bytes": 5 * 1024**3,
    # move an over-limit scratch dir aside instead of only alerting
    "disk_quarantine": False,
}

# env var -> (config key, parser)
_ENV_MAP = {
    "FLEET_GUARD_STATE_DIR": ("state_dir", str),
    "FLEET_GUARD_BG_QUOTA": ("bg_quota", int),
    "FLEET_GUARD_BUSY_ALERT": ("busy_alert_threshold", int),
    "FLEET_GUARD_STALL_TIMEOUT": ("stall_timeout_s", float),
    "FLEET_GUARD_WATCH_INTERVAL": ("watch_interval_s", float),
    "FLEET_GUARD_KILL_GRACE": ("kill_grace_s", float),
    "FLEET_GUARD_SCRATCH_DIRS": ("scratch_dirs", lambda v: [p for p in v.split(os.pathsep) if p]),
    "FLEET_GUARD_DISK_MAX_BYTES": ("disk_max_bytes", int),
    "FLEET_GUARD_DISK_MIN_FREE": ("disk_min_free_bytes", int),
    "FLEET_GUARD_DISK_QUARANTINE": ("disk_quarantine", lambda v: v.strip().lower() in ("1", "true", "yes")),
}

ENV_MARKER = "FLEET_GUARD_SESSION_ID"


def _load_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        # Fail-open: a corrupt/unreadable config never breaks the agent.
        return {}


def load() -> dict:
    cfg = dict(DEFAULTS)
    state_dir = Path(os.path.expandvars(os.path.expanduser(cfg["state_dir"])))
    file_cfg = _load_file(state_dir / "config.json")
    for key, value in file_cfg.items():
        if key in cfg:
            cfg[key] = value
    for env_var, (key, parse) in _ENV_MAP.items():
        raw = os.environ.get(env_var)
        if raw is None or raw == "":
            continue
        try:
            cfg[key] = parse(raw)
        except (ValueError, TypeError):
            continue  # fail-open: ignore malformed env values
    return cfg


def state_dir(cfg: dict | None = None) -> Path:
    cfg = cfg or load()
    d = Path(os.path.expandvars(os.path.expanduser(str(cfg["state_dir"]))))
    d.mkdir(parents=True, exist_ok=True)
    return d
