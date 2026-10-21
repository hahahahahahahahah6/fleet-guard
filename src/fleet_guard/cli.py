"""fleet-guard command line interface."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import config, hooks, ledger
from . import watchdog as wd
from . import disk as diskmod


def _cfg_from_args(args) -> dict:
    cfg = config.load()
    if getattr(args, "state_dir", None):
        cfg["state_dir"] = args.state_dir
        os.environ["FLEET_GUARD_STATE_DIR"] = args.state_dir
    return cfg


def cmd_hook(args) -> int:
    return hooks.main(["hook", args.which])


def cmd_watch_add(args) -> int:
    cfg = _cfg_from_args(args)
    entry = wd.add(args.pid, args.log, timeout_s=args.timeout,
                   session_id=args.session, label=args.label or "", cfg=cfg)
    print(json.dumps(entry, indent=2))
    return 0


def cmd_watch_rm(args) -> int:
    cfg = _cfg_from_args(args)
    print("removed" if wd.remove(args.pid, cfg) else "not watched")
    return 0


def cmd_watch_list(args) -> int:
    cfg = _cfg_from_args(args)
    print(json.dumps(wd.list_entries(cfg), indent=2))
    return 0


def cmd_daemon(args) -> int:
    cfg = _cfg_from_args(args)
    return wd.daemon(args.interval, cfg)


def cmd_watchdog_once(args) -> int:
    cfg = _cfg_from_args(args)
    try:
        actions = wd.check_once(cfg)
    except Exception as exc:  # noqa: BLE001 — fail-open by design
        print(f"fleet-guard watchdog failed open ({exc}).", file=sys.stderr)
        return 0
    for action in actions:
        print(json.dumps(action))
    return 0


def cmd_disk_check(args) -> int:
    cfg = _cfg_from_args(args)
    try:
        alerts = diskmod.check(cfg)
    except Exception as exc:  # noqa: BLE001 — fail-open by design
        print(f"fleet-guard disk-check failed open ({exc}).", file=sys.stderr)
        return 0
    print(json.dumps(alerts, indent=2))
    return 0


def cmd_ledger(args) -> int:
    cfg = _cfg_from_args(args)
    for rec in ledger.tail(args.n, cfg):
        print(json.dumps(rec))
    return 0


def cmd_run(args) -> int:
    """Launch a command attributed to a session: sets FLEET_GUARD_SESSION_ID."""
    env = dict(os.environ)
    env[config.ENV_MARKER] = args.session
    os.execvpe(args.cmd[0], args.cmd, env)
    return 127  # unreachable


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="fleet-guard",
        description="Machine-resource guard layer for fleets of coding-agent sessions.",
    )
    ap.add_argument("--state-dir", default=None,
                    help="override state dir (or FLEET_GUARD_STATE_DIR)")
    sub = ap.add_subparsers(dest="command", required=True)

    h = sub.add_parser("hook", help="Claude Code hook entry point")
    h.add_argument("which", choices=["pretooluse", "sessionend", "stop"])
    h.set_defaults(func=cmd_hook)

    w = sub.add_parser("watch", help="register processes with the watchdog")
    wsub = w.add_subparsers(dest="watch_cmd", required=True)
    wa = wsub.add_parser("add", help="watch a pid's log for stalls")
    wa.add_argument("--pid", type=int, required=True)
    wa.add_argument("--log", required=True, help="log file whose mtime = liveness")
    wa.add_argument("--timeout", type=float, default=None,
                    help="stall seconds (default: config stall_timeout_s)")
    wa.add_argument("--session", default=None)
    wa.add_argument("--label", default="")
    wa.set_defaults(func=cmd_watch_add)
    wr = wsub.add_parser("rm", help="stop watching a pid")
    wr.add_argument("--pid", type=int, required=True)
    wr.set_defaults(func=cmd_watch_rm)
    wl = wsub.add_parser("list", help="list watched processes")
    wl.set_defaults(func=cmd_watch_list)

    d = sub.add_parser("daemon", help="run the stall watchdog daemon")
    d.add_argument("--interval", type=float, default=None)
    d.set_defaults(func=cmd_daemon)

    o = sub.add_parser("watchdog-once", help="single watchdog pass (cron-friendly)")
    o.set_defaults(func=cmd_watchdog_once)

    k = sub.add_parser("disk-check", help="check scratch dirs against disk limits")
    k.set_defaults(func=cmd_disk_check)

    lg = sub.add_parser("ledger", help="show recent ledger entries")
    lg.add_argument("-n", type=int, default=20)
    lg.set_defaults(func=cmd_ledger)

    r = sub.add_parser("run", help="run a command attributed to a session id")
    r.add_argument("--session", required=True)
    r.add_argument("cmd", nargs=argparse.REMAINDER)
    r.set_defaults(func=cmd_run)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run" and (not args.cmd or args.cmd[0] == "--"):
        args.cmd = [c for c in args.cmd if c != "--"]
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 — fail-open by design
        print(f"fleet-guard failed open ({exc}).", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())
