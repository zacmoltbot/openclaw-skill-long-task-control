#!/usr/bin/env python3
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS = ROOT / "scripts" / "openclaw_ops.py"
LEDGER = ROOT / "scripts" / "task_ledger.py"
CRON = ROOT / "scripts" / "monitor_cron.py"


def run(*args, env=None, check=True):
    return subprocess.run(args, text=True, capture_output=True, env=env, check=check)


def run_json(*args, env=None, check=True):
    return json.loads(run(*args, env=env, check=check).stdout)


def load(path: Path):
    return json.loads(path.read_text())


def task_from(path: Path, task_id: str):
    return next(t for t in load(path)["tasks"] if t["task_id"] == task_id)


FAKE_OPENCLAW = """#!/bin/sh
if [ \"$1\" = "message" ] && [ \"$2\" = "send" ]; then
  shift 2
  target=""
  while [ $# -gt 0 ]; do
    case \"$1\" in
      --target)
        target="$2"
        shift 2
        ;;
      *)
        shift
        ;;
    esac
  done
  if printf '%s' "$target" | grep -Eq '^[0-9]+$'; then
    printf '{"ok":true,"messageId":"msg-123"}\n'
    exit 0
  fi
  echo "invalid target: $target" >&2
  exit 12
fi
if [ \"$1\" = "agent" ]; then
  printf '{"ok":true}\n'
  exit 0
fi
echo "unsupported fake openclaw invocation: $*" >&2
exit 99
"""


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ledger = tmp / "ledger.json"
        cron_dir = tmp / "cron"
        cron_dir.mkdir()
        fake_bin = tmp / "bin"
        fake_bin.mkdir()
        fake_openclaw = fake_bin / "openclaw"
        fake_openclaw.write_text(FAKE_OPENCLAW)
        fake_openclaw.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"

        # 1) username-style discord requester target gets normalized from session key.
        normalized_task = "normalize-discord-target"
        boot = run_json(
            "python3", str(OPS), "--ledger", str(ledger), "bootstrap-task", normalized_task,
            "--goal", "normalize requester discord target",
            "--channel", "discord",
            "--requester-channel", "edward_ch_wang",
            "--session-key", "agent:main:discord:channel:1477237887136432162",
            "--workflow", "Do one step",
            "--next-action", "Run",
            "--disabled",
            "--dry-run",
            "--no-auto-execution",
            env=env,
        )
        normalized_state = task_from(ledger, normalized_task)
        assert normalized_state["message"]["requester_channel"] == "1477237887136432162", normalized_state["message"]
        assert normalized_state["message"]["requester_channel_source"] == "session_key_fallback", normalized_state["message"]
        assert boot["requester_channel"] == "1477237887136432162", boot

        # 2) repeated identical invalid-target delivery failure on blocked task => fail-closed stop.
        blocked_task = "blocked-invalid-target-fail-closed"
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", blocked_task,
            "--goal", "blocked notification target invalid should not churn forever",
            "--channel", "discord",
            "--requester-channel", "edward_ch_wang",
            "--workflow", "Inspect",
            "--next-action", "Inspect",
            env=env,
        )
        run_json("python3", str(CRON), "--ledger", str(ledger), "--cron-dir", str(cron_dir), "install", blocked_task, env=env)
        run(
            "python3", str(LEDGER), "--ledger", str(ledger), "block", blocked_task,
            "--reason", "Missing credentials",
            "--safe-next-step", "Wait for credentials",
            "--fact", "failure_type=AUTH_REQUIRED",
            env=env,
        )
        stop = run_json("python3", str(CRON), "--ledger", str(ledger), "--cron-dir", str(cron_dir), "run-once", "--task-id", blocked_task, env=env)
        blocked_state = task_from(ledger, blocked_task)
        assert stop["stopped_fail_closed"] is True, stop
        assert blocked_state["monitoring"]["cron_state"] == "DELETED", blocked_state["monitoring"]
        assert blocked_state["monitoring"]["stop_policy"] == "FAIL_CLOSED", blocked_state["monitoring"]
        assert blocked_state["reporting"]["pending_updates"], blocked_state["reporting"]

        # 3) repeated identical delivery/config failure on non-terminal task => thresholded fail-closed stop.
        repeated_task = "repeated-delivery-fail-closed"
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", repeated_task,
            "--goal", "same delivery failure should stop after threshold",
            "--channel", "discord",
            "--requester-channel", "still_bad_username",
            "--workflow", "Inspect",
            "--next-action", "Inspect",
            env=env,
        )
        run_json("python3", str(CRON), "--ledger", str(ledger), "--cron-dir", str(cron_dir), "install", repeated_task, env=env)
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "record-update", "STEP_COMPLETED", repeated_task,
            "--summary", "checkpoint that keeps failing to deliver",
            "--current-checkpoint", "step-01",
            "--next-action", "Continue",
            env=env,
        )
        final = None
        for idx in range(3):
            proc = run("python3", str(CRON), "--ledger", str(ledger), "--cron-dir", str(cron_dir), "run-once", "--task-id", repeated_task, env=env, check=False)
            assert proc.returncode == 0, proc.stdout + proc.stderr
            payload = json.loads(proc.stdout)
            if idx < 2:
                assert payload.get("stopped_fail_closed") is not True, payload
            else:
                final = payload
        repeated_state = task_from(ledger, repeated_task)
        assert final["stopped_fail_closed"] is True, final
        assert repeated_state["monitoring"]["cron_state"] == "DELETED", repeated_state["monitoring"]
        assert repeated_state["monitoring"]["stop_reason"] == "repeated identical delivery/config failure with no recovery path", repeated_state["monitoring"]

        print(json.dumps({
            "ok": True,
            "normalized_target": normalized_state["message"],
            "blocked_fail_closed": stop,
            "repeated_delivery_fail_closed": final,
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
