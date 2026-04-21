#!/usr/bin/env python3
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS = ROOT / "scripts" / "openclaw_ops.py"


def run(*args, env=None):
    return subprocess.run(args, check=True, text=True, capture_output=True, env=env)


def run_json(*args, env=None):
    return json.loads(run(*args, env=env).stdout)


def main():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "scripts")
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / "ledger.json"
        task_id = "partial-success-closeout"

        run(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", task_id,
            "--goal", "Verify terminal cleanup on partial success",
            "--requester-channel", "1477237887136432162",
            "--workflow", "Step 1",
            "--workflow", "Step 2",
            "--next-action", "Run step 1",
            env=env,
        )

        run(
            "python3", str(OPS), "--ledger", str(ledger), "record-update", "STEP_COMPLETED", task_id,
            "--summary", "Completed step 1",
            "--current-checkpoint", "step-01",
            "--next-action", "Skip directly to closeout",
            env=env,
        )
        run(
            "python3", str(OPS), "--ledger", str(ledger), "record-update", "TASK_COMPLETED", task_id,
            "--summary", "Task completed with useful artifacts despite skipped closeout step",
            "--current-checkpoint", "step-01",
            "--output", "/tmp/fake-artifact.mp4",
            env=env,
        )

        preview = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", task_id, env=env)
        assert preview["truth_state"] == "INCONSISTENT", preview
        assert any(item.startswith("task:completed_but_steps_not_done:") for item in preview["inconsistencies"]), preview
        assert preview["state"] == "STOP_AND_DELETE", preview
        print(json.dumps({"ok": True, "state": preview["state"], "reason": preview["reason"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
