# fleet-guard

Machine-resource guard layer for fleets of coding-agent sessions (Claude Code and friends).

When you run many agent sessions in parallel, the failure mode is rarely a bad
prompt — it's the *machine* falling over: background processes leak and pile
up, a hung run burns quota for hours while doing nothing, a session ends and
leaves orphaned children behind, a scratch dir quietly eats the disk overnight.
`fleet-guard` is a small stdlib-only Python package — three Claude Code hooks, a
stall watchdog daemon, a disk guard, and an append-only JSONL ledger — that
keeps the fleet from taking down the host.

## What it does

1. **Cross-session background-process quota** — a `PreToolUse` hook counts
   agent-attributed background processes *machine-wide*. Over the cap (default:
   half your CPU cores), the new background run is blocked with a message
   naming what's already running.
2. **Stall watchdog** — register long-running processes with `fleet-guard watch
   add`. If a watched process is alive but its log goes quiet for N seconds
   (default 600), the watchdog captures a diagnostic snapshot and kills the
   whole process tree — children first, `SIGTERM`, then `SIGKILL` for survivors.
3. **SessionEnd reaping** — a `SessionEnd` hook kills leaked child process
   trees the session left behind.
4. **Disk guard** — alerts (and optionally quarantines) when agent scratch dirs
   grow past a threshold, or their filesystem runs low on free space.
5. **Append-only ledger** — every guard action lands in `~/.fleet-guard/ledger.jsonl`.

**Fail-open by design.** Every hook and daemon pass is wrapped so that a guard
that crashes exits 0 and lets the agent continue. A guard must never take down
the agent it's protecting.

## How this differs

- **vs `loop-guard` (my semantic layer):** `loop-guard` detects *semantic*
  loops — an agent stuck in churn/test/burn review cycles, repeating itself
  without progress. `fleet-guard` is the *machine-resource* layer underneath it:
  processes, stalls, leaked children, disk. An agent can be semantically fine
  and still wedge the machine (a hung `sleep`-forever burns no tokens but holds
  a slot; a runaway log fills the disk). The two are complementary — run both.
- **vs manual shell workarounds** (`session-tree.sh` scripts, hand-rolled
  cgroups, `pkill` cron jobs): those work, but they're glue you maintain per
  machine. `fleet-guard` is one installable package — hooks, daemon, ledger —
  with a single config surface and tests.

## Motivating case class

The design is informed by Ramiz Raja's production checklist for running
multiple Claude Code agents
([codebyramiz.com/blog/running-multiple-claude-code-agents](https://codebyramiz.com/blog/running-multiple-claude-code-agents),
Oct 2026): cap background processes at half the cores, alert when too many are
busy, reap leaked processes on session end, and run a watchdog that snapshots
and kills a process tree after 600s of log silence. His watchdog once caught a
run hung for **7h22m while using only 258ms of CPU** — invisible to any
CPU-based monitor, obvious to a log-liveness check. His disk policy came from
an overnight **56G → 1G** incident. `fleet-guard` packages exactly that
operational pattern so you don't have to re-derive it the hard way.

## Install

```bash
pip install fleet-guard
```

Python 3.9+, stdlib only — no dependencies. Linux is the primary target
(`/proc`-based inspection); macOS works via a `ps` fallback (environment
attribution is Linux-only).

## Hook setup (Claude Code)

Add to your Claude Code hooks config:

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash",
        "hooks": [{ "type": "command", "command": "fleet-guard hook pretooluse" }] }
    ],
    "SessionEnd": [
      { "hooks": [{ "type": "command", "command": "fleet-guard hook sessionend" }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "fleet-guard hook stop" }] }
    ]
  }
}
```

Attribute background runs to a session so the quota and reaper can see them:

```bash
fleet-guard run --session "$CLAUDE_SESSION_ID" -- ./long-build.sh > build.log 2>&1 &
fleet-guard watch add --pid $! --log build.log --timeout 600 --label "build"
```

## Usage

```bash
# watch a process's log for stalls (log mtime = liveness signal)
fleet-guard watch add --pid 1234 --log ./build.log --timeout 600 --session abc
fleet-guard watch list
fleet-guard watch rm --pid 1234

# run the watchdog daemon (or tick `watchdog-once` from cron)
fleet-guard daemon --interval 30
fleet-guard watchdog-once

# check agent scratch dirs against disk limits
fleet-guard disk-check

# inspect what the guard did
fleet-guard ledger -n 20
```

## Configuration

Defaults live in code; override with `~/.fleet-guard/config.json` or
`FLEET_GUARD_*` environment variables:

| Key | Default | Env |
|---|---|---|
| `bg_quota` | half CPU cores | `FLEET_GUARD_BG_QUOTA` |
| `busy_alert_threshold` | 4 | `FLEET_GUARD_BUSY_ALERT` |
| `stall_timeout_s` | 600 | `FLEET_GUARD_STALL_TIMEOUT` |
| `watch_interval_s` | 30 | `FLEET_GUARD_WATCH_INTERVAL` |
| `kill_grace_s` | 5 | `FLEET_GUARD_KILL_GRACE` |
| `scratch_dirs` | [] | `FLEET_GUARD_SCRATCH_DIRS` (pathsep-joined) |
| `disk_max_bytes` | 10 GB | `FLEET_GUARD_DISK_MAX_BYTES` |
| `disk_min_free_bytes` | 5 GB | `FLEET_GUARD_DISK_MIN_FREE` |
| `disk_quarantine` | false | `FLEET_GUARD_DISK_QUARANTINE=1` |
| `state_dir` | `~/.fleet-guard` | `FLEET_GUARD_STATE_DIR` |

## Ledger format

`~/.fleet-guard/ledger.jsonl`, one JSON object per line:

```json
{"ts": "2026-10-21T19:00:00+00:00", "event": "watchdog.stall-kill",
 "session_id": "abc", "details": {"pid": 1234, "idle_s": 623.4,
 "dump": "/home/you/.fleet-guard/dumps/stall-1234-1729530000.json", "sigkilled": []}}
```

Events: `hook.pretooluse-block`, `hook.stop-busy-alert`, `sessionend.reap`,
`watch.add`, `watch.exit`, `watchdog.stall-kill`, `disk.alert`.

## A note on "thread dumps"

The stall snapshot captures everything reachable from *outside* the target
process with the stdlib on Linux: cmdline, `/proc/<pid>/status`, wait channel,
kernel stack (`/proc/<pid>/stack`), IO counters, open FD count, exe/cwd. That
is usually enough to tell "deadlocked on a futex" from "waiting on network
IO". A true in-*process* Python thread dump (every thread's Python-level stack)
needs cooperation from inside the target — tools like `py-spy` or `gdb` —
which is deliberately out of scope for a zero-dependency guard. If you need
that, point the `dump` path at your py-spy wrapper.

## Development

```bash
python -m unittest discover -s tests -v   # 11 tests, no network
```

## License

MIT — see [LICENSE](LICENSE).
