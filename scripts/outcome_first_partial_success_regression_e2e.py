#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS = ROOT / "scripts" / "openclaw_ops.py"
RUNNER = ROOT / "scripts" / "runner_engine.py"
MONITOR = ROOT / "scripts" / "monitor_nudge.py"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, text=True, capture_output=True, check=True)


def run_json(*args: str):
    return json.loads(run(*args).stdout)


def load(path: Path):
    return json.loads(path.read_text())


def task_from(ledger_path: Path, task_id: str):
    return next(task for task in load(ledger_path)["tasks"] if task["task_id"] == task_id)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger = tmp / "ledger.json"
        jobs_root = tmp / "jobs"
        started = tmp / "shell-started.txt"
        artifact = tmp / "delivered.txt"
        script = tmp / "produce_then_hang.py"
        task_id = "outcome-first-partial-success"
        job_id = f"{task_id}-job"

        script.write_text(
            """
from pathlib import Path
import sys
import time

started = Path(sys.argv[1])
artifact = Path(sys.argv[2])
started.write_text("started\\n")
artifact.write_text("real output exists\\n")
while True:
    time.sleep(0.2)
""".strip() + "\n"
        )

        bootstrap = run_json(
            "python3", str(OPS), "--ledger", str(ledger), "bootstrap-task", task_id,
            "--goal", "Produce an output, then get interrupted before clean completion",
            "--owner", "main-agent",
            "--channel", "discord",
            "--requester-channel", "outcome-first-regression",
            "--workflow", f"Produce output then hang :: shell=python3 {script} {started} {artifact} :: expect={artifact}",
            "--next-action", "Run canonical execution loop",
            "--message-ref", "discord:msg:outcome-first",
            "--jobs-root", str(jobs_root),
            "--disabled",
            "--no-auto-start-execution",
        )
        assert bootstrap["execution"]["job"]["job_id"] == job_id, bootstrap

        proc = subprocess.Popen(
            ["python3", str(RUNNER), "--jobs-root", str(jobs_root), "run-loop", job_id, "--execution-owner", "outcome-first-test"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

        deadline = time.time() + 10
        while time.time() < deadline:
            if started.exists() and artifact.exists():
                break
            time.sleep(0.1)
        assert started.exists(), "interruptible step never started"
        assert artifact.exists(), "expected artifact was not produced before interruption"

        os.killpg(proc.pid, signal.SIGTERM)
        stdout, _stderr = proc.communicate(timeout=10)
        payload = json.loads((stdout or "").strip().splitlines()[-1])
        assert payload["interrupted"] is True, payload

        task = task_from(ledger, task_id)
        monitor = run_json("python3", str(MONITOR), "--ledger", str(ledger))
        report = next(item for item in monitor["reports"] if item["task_id"] == task_id)
        preview = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", task_id)
        user_facing = task.get("derived", {}).get("user_facing") or {}

        assert task["status"] == "BLOCKED", task
        assert task["blocker"]["failure_type"] == "EXECUTION_INTERRUPTED", task
        assert user_facing["outcome_status"] == "PARTIAL_SUCCESS", user_facing
        assert str(artifact) in user_facing["artifacts"], user_facing
        assert report["state"] == "OWNER_RECONCILE", report
        assert "partial-success" in report["reason"], report
        assert preview["user_facing"]["outcome_status"] == "PARTIAL_SUCCESS", preview
        assert str(artifact) in preview["user_facing"]["artifacts"], preview

        print(json.dumps({
            "ok": True,
            "task_id": task_id,
            "job_id": job_id,
            "runner_payload": payload,
            "task_status": task["status"],
            "monitor_state": report["state"],
            "user_facing": user_facing,
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
