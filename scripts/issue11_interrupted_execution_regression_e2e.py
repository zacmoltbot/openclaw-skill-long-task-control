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
        marker = tmp / "shell-started.txt"
        gate = tmp / "resume-artifact.txt"
        script = tmp / "interruptible_step.py"
        task_id = "issue11-interrupted-execution"
        job_id = f"{task_id}-job"

        script.write_text(
            """
from pathlib import Path
import sys
import time

marker = Path(sys.argv[1])
marker.write_text("started\\n")
while True:
    time.sleep(0.2)
""".strip() + "\n"
        )

        bootstrap = run_json(
            "python3", str(OPS), "--ledger", str(ledger), "bootstrap-task", task_id,
            "--goal", "Prove interrupted canonical execution becomes real ledger truth",
            "--owner", "main-agent",
            "--channel", "discord",
            "--requester-channel", "issue11-regression",
            "--workflow", f"Run interruptible shell step :: shell=python3 {script} {marker} :: artifact={gate}",
            "--next-action", "Run canonical execution loop",
            "--message-ref", "discord:msg:issue11",
            "--jobs-root", str(jobs_root),
            "--disabled",
            "--no-auto-start-execution",
        )
        assert bootstrap["execution"]["job"]["job_id"] == job_id, bootstrap

        proc = subprocess.Popen(
            ["python3", str(RUNNER), "--jobs-root", str(jobs_root), "run-loop", job_id, "--execution-owner", "issue11-test"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

        deadline = time.time() + 10
        while time.time() < deadline:
            if marker.exists():
                break
            time.sleep(0.1)
        assert marker.exists(), "interruptible step never started"

        os.killpg(proc.pid, signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=10)
        payload = json.loads((stdout or "").strip().splitlines()[-1])

        task = task_from(ledger, task_id)
        job = load(jobs_root / job_id / "job.json")
        progress = [json.loads(line) for line in (jobs_root / job_id / "progress.jsonl").read_text().splitlines() if line.strip()]
        monitor = run_json("python3", str(MONITOR), "--ledger", str(ledger))
        report = next(item for item in monitor["reports"] if item["task_id"] == task_id)
        pending = task.get("reporting", {}).get("pending_updates", [])
        blocked = next(item for item in pending if item["event_type"] == "BLOCKED_ESCALATE")

        assert payload["interrupted"] is True, payload
        assert payload["signal"] == "SIGTERM", payload
        assert task["status"] == "BLOCKED", task
        assert task["blocker"]["failure_type"] == "EXECUTION_INTERRUPTED", task
        assert task["observed"]["block"]["facts"]["interrupted_signal"] == "SIGTERM", task["observed"]["block"]
        assert blocked["facts"]["failure_type"] == "EXECUTION_INTERRUPTED", blocked
        assert blocked["facts"]["interrupted_signal"] == "SIGTERM", blocked
        assert report["state"] == "BLOCKED_ESCALATE", report
        assert job["status"] == "BLOCKED", job
        assert job["blocked_reason"] == "EXECUTION_INTERRUPTED", job
        assert job["execution"]["last_interrupted_signal"] == "SIGTERM", job["execution"]
        assert any(event["kind"] == "JOB_INTERRUPTED" for event in progress), progress

        print(json.dumps({
            "ok": True,
            "task_id": task_id,
            "job_id": job_id,
            "runner_payload": payload,
            "task_status": task["status"],
            "failure_type": task["blocker"]["failure_type"],
            "monitor_state": report["state"],
            "job_status": job["status"],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
