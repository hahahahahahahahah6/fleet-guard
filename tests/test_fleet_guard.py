"""End-to-end tests for fleet-guard. No network. Linux /proc assumed.

Each test isolates state via FLEET_GUARD_STATE_DIR pointing at a temp dir.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, os.path.abspath(SRC))

from fleet_guard import config, disk as diskmod, ledger, procutil  # noqa: E402
from fleet_guard import watchdog as wd  # noqa: E402

MARKER = config.ENV_MARKER


def _base_env(state_dir, **extra):
    env = dict(os.environ)
    env["FLEET_GUARD_STATE_DIR"] = state_dir
    env["PYTHONPATH"] = os.path.abspath(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env.update(extra)
    return env


def _run_cli(args, state_dir, stdin_text="", **extra_env):
    return subprocess.run(
        [sys.executable, "-m", "fleet_guard"] + args,
        input=stdin_text, capture_output=True, text=True, timeout=60,
        env=_base_env(state_dir, **extra_env),
    )


def _spawn_sleep(seconds=300, session=None):
    env = dict(os.environ)
    if session:
        env[MARKER] = session
    return subprocess.Popen(["sleep", str(seconds)], env=env)


class FleetGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fg-test-")
        os.environ["FLEET_GUARD_STATE_DIR"] = self.tmp
        self.procs: list[subprocess.Popen] = []

    def tearDown(self):
        for p in self.procs:
            try:
                p.kill()
            except OSError:
                pass
        # belt and suspenders: kill any leftover marker-tagged sleeps
        for p in procutil.procs_with_env(MARKER):
            if "sleep" in p["cmdline"]:
                try:
                    os.kill(p["pid"], signal.SIGKILL)
                except OSError:
                    pass
        os.environ.pop("FLEET_GUARD_STATE_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 1. quota breach blocks ----
    def test_quota_breach_blocks(self):
        for _ in range(3):
            self.procs.append(_spawn_sleep(session="quota-test"))
        time.sleep(0.5)
        r = _run_cli(["hook", "pretooluse"], self.tmp,
                     stdin_text=json.dumps({"session_id": "s1", "tool_name": "Bash"}),
                     FLEET_GUARD_BG_QUOTA="2")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("cap 2", out["reason"])
        events = [rec["event"] for rec in ledger.tail(5)]
        self.assertIn("hook.pretooluse-block", events)

    def test_quota_under_cap_allows(self):
        self.procs.append(_spawn_sleep(session="quota-test"))
        time.sleep(0.3)
        r = _run_cli(["hook", "pretooluse"], self.tmp,
                     stdin_text=json.dumps({"session_id": "s1"}),
                     FLEET_GUARD_BG_QUOTA="5")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("decision", r.stdout)

    # ---- 2. fail-open ----
    def test_fail_open_corrupt_state(self):
        with open(os.path.join(self.tmp, "config.json"), "w") as fh:
            fh.write("{not json")
        with open(os.path.join(self.tmp, "watch.json"), "w") as fh:
            fh.write("[broken")
        r = _run_cli(["hook", "pretooluse"], self.tmp,
                     stdin_text=json.dumps({"session_id": "s1"}))
        self.assertEqual(r.returncode, 0)  # allowed, not blocked
        self.assertNotIn("decision", r.stdout)
        r2 = _run_cli(["watchdog-once"], self.tmp)
        self.assertEqual(r2.returncode, 0, r2.stderr)

    # ---- 3. stall -> dump -> kill tree ----
    def test_stall_snapshot_and_kill(self):
        log = os.path.join(self.tmp, "agent.log")
        with open(log, "w") as fh:
            fh.write("working...\n")
        old = time.time() - 3600
        os.utime(log, (old, old))
        p = _spawn_sleep(300)
        self.procs.append(p)
        wd.add(p.pid, log, timeout_s=60, session_id="stall-sess", label="test")
        actions = wd.check_once(now=time.time())
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "stall-kill")
        self.assertEqual(actions[0]["pid"], p.pid)
        dump = actions[0]["dump"]
        self.assertTrue(dump and os.path.isfile(dump))
        with open(dump) as fh:
            snap = json.load(fh)
        self.assertEqual(snap["pid"], p.pid)
        time.sleep(0.5)
        self.assertIsNotNone(p.poll(), "stalled process should be dead")
        events = [rec["event"] for rec in ledger.tail(5)]
        self.assertIn("watchdog.stall-kill", events)
        self.assertEqual(wd.list_entries(), [])

    def test_busy_process_not_killed(self):
        log = os.path.join(self.tmp, "busy.log")
        with open(log, "w") as fh:
            fh.write("fresh output\n")
        p = _spawn_sleep(300)
        self.procs.append(p)
        wd.add(p.pid, log, timeout_s=600, session_id="busy-sess")
        actions = wd.check_once()
        self.assertEqual(actions, [])
        self.assertIsNone(p.poll(), "busy process must survive")

    # ---- 4. sessionend reaps leaked children ----
    def test_sessionend_reaps_tree(self):
        parent = subprocess.Popen(
            [sys.executable, "-c",
             "import subprocess, time, os; "
             "c = subprocess.Popen(['sleep','300']); "
             "print(c.pid, flush=True); time.sleep(300)"],
            env={**os.environ, MARKER: "sess-123"},
            stdout=subprocess.PIPE, text=True)
        self.procs.append(parent)
        child_pid = int(parent.stdout.readline().strip())
        time.sleep(0.5)
        r = _run_cli(["hook", "sessionend"], self.tmp,
                     stdin_text=json.dumps({"session_id": "sess-123",
                                            "reason": "test"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        time.sleep(1.0)
        self.assertIsNotNone(parent.poll(), "parent should be reaped")
        self.assertFalse(procutil.is_alive(child_pid) and
                         _cmdline(child_pid) == "sleep 300",
                         "leaked child should be reaped")
        events = [rec["event"] for rec in ledger.tail(5)]
        self.assertIn("sessionend.reap", events)

    # ---- 5. disk guard ----
    def test_disk_alert(self):
        scratch = os.path.join(self.tmp, "scratch")
        os.makedirs(scratch)
        with open(os.path.join(scratch, "big.bin"), "wb") as fh:
            fh.write(b"x" * 5000)
        alerts = diskmod.check(_cfg({"scratch_dirs": [scratch],
                                     "disk_max_bytes": 1000}))
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "over-limit")
        events = [rec["event"] for rec in ledger.tail(5)]
        self.assertIn("disk.alert", events)

    def test_disk_quarantine(self):
        scratch = os.path.join(self.tmp, "scratch2")
        os.makedirs(scratch)
        with open(os.path.join(scratch, "big.bin"), "wb") as fh:
            fh.write(b"x" * 5000)
        alerts = diskmod.check(_cfg({"scratch_dirs": [scratch],
                                     "disk_max_bytes": 1000,
                                     "disk_quarantine": True}))
        self.assertEqual(alerts[0]["kind"], "over-limit")
        self.assertIn("quarantined_to", alerts[0])
        self.assertFalse(os.path.isdir(scratch))
        self.assertTrue(os.path.isdir(alerts[0]["quarantined_to"]))

    # ---- 6. ledger append-only ----
    def test_ledger_append_only(self):
        ledger.append("test.one", "s", {"a": 1})
        ledger.append("test.two", "s", {"a": 2})
        recs = ledger.tail(2)
        self.assertEqual([r["event"] for r in recs], ["test.one", "test.two"])
        for r in recs:
            self.assertIn("ts", r)
        with open(ledger.path(), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)  # each line is valid JSON

    # ---- 7. stop hook busy warning ----
    def test_stop_hook_warns_on_busy(self):
        log = os.path.join(self.tmp, "w.log")
        with open(log, "w") as fh:
            fh.write("x\n")
        p = _spawn_sleep(300)
        self.procs.append(p)
        wd.add(p.pid, log, timeout_s=9999, session_id="s9")
        r = _run_cli(["hook", "stop"], self.tmp,
                     stdin_text=json.dumps({"session_id": "s9"}),
                     FLEET_GUARD_BUSY_ALERT="0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("still busy", r.stderr)
        events = [rec["event"] for rec in ledger.tail(5)]
        self.assertIn("hook.stop-busy-alert", events)

    # ---- 8. `run` attributes processes to a session ----
    def test_run_attributes_session(self):
        p = subprocess.Popen(
            [sys.executable, "-m", "fleet_guard", "run",
             "--session", "run-sess", "--", "sleep", "300"],
            env=_base_env(self.tmp))
        self.procs.append(p)
        time.sleep(0.8)
        hits = procutil.procs_with_env(MARKER, "run-sess")
        self.assertTrue(any("sleep 300" in h["cmdline"] for h in hits),
                        f"attributed sleep not found: {hits}")


def _cfg(overrides):
    cfg = config.load()
    cfg.update(overrides)
    return cfg


def _cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode().strip()
    except OSError:
        return ""


if __name__ == "__main__":
    unittest.main(verbosity=2)
