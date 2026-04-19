#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER_TOOL = ROOT / "scripts" / "task_ledger.py"
RUNNER = ROOT / "scripts" / "runner_engine.py"
EXECUTOR = ROOT / "scripts" / "executor_engine.py"
MONITOR = ROOT / "scripts" / "monitor_nudge.py"


def run(*args, **kwargs):
    kwargs.setdefault("timeout", 15)
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)


def run_json(*args, **kwargs):
    r = run(*args, **kwargs)
    return json.loads(r.stdout.strip())


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def save(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def create_job(ledger_path: Path, jobs_root: Path, task_id: str) -> str:
    job_id = f"{task_id}-job"
    run(
        "python3", str(LEDGER_TOOL), "--ledger", str(ledger_path),
        "init", task_id,
        "--goal", "terminal blocked precedence must notify user",
        "--owner", "main-agent",
        "--channel", "discord",
        "--next-action", "run execution",
        "--workflow", "Step 1 :: shell=true",
        "--workflow", "Step 2 :: shell=false :: retry_budget=1",
    )

    spec = {
        "job_id": job_id,
        "kind": "generic-long-task",
        "adapter": "generic_manual",
        "mode": "serial",
        "bridge": {"ledger": str(ledger_path), "task_id": task_id},
        "items": [
            {"item_id": "step-01", "title": "Step 1", "checkpoint": "step-01", "shell": "true"},
            {"item_id": "step-02", "title": "Step 2", "checkpoint": "step-02", "shell": "false", "retry_budget": 1},
        ],
    }
    spec_path = jobs_root / job_id / "job-spec.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n")
    run_json(
        "python3", str(RUNNER), "--jobs-root", str(jobs_root),
        "init-job", str(spec_path),
        "--ledger", str(ledger_path),
        "--task-id", task_id,
    )
    return job_id


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        ledger = Path(td) / "ledger.json"
        jobs_root = Path(td) / "jobs"
        task_id = "terminal-blocked-precedence"
        job_id = create_job(ledger, jobs_root, task_id)

        run_json("python3", str(EXECUTOR), "--jobs-root", str(jobs_root), "run-next", job_id, "--execution-owner", "regression-test")
        retry = run_json("python3", str(EXECUTOR), "--jobs-root", str(jobs_root), "run-next", job_id, "--execution-owner", "regression-test")
        blocked = run_json("python3", str(EXECUTOR), "--jobs-root", str(jobs_root), "run-next", job_id, "--execution-owner", "regression-test")
        assert retry["status"] == "RETRY", retry
        assert blocked["status"] == "BLOCKED", blocked

        # Simulate the cross-agent / interrupted-owner bug class:
        # executor truth already says BLOCKED, but ledger top-level/projection drifts back to RUNNING
        payload = load(ledger)
        task = next(t for t in payload["tasks"] if t["task_id"] == task_id)
        task["status"] = "RUNNING"
        task["current_checkpoint"] = "step-01"
        task["last_checkpoint_at"] = task.get("created_at")
        task.setdefault("observed", {}).pop("block", None)
        task.setdefault("monitoring", {})["last_escalated_at"] = None
        save(ledger, payload)

        report = run_json("python3", str(MONITOR), "--ledger", str(ledger))["reports"][0]
        assert report["state"] == "BLOCKED_ESCALATE", report
        assert "terminal blocked executor truth observed" in report["reason"], report

        print(json.dumps({
            "ok": True,
            "task_id": task_id,
            "job_id": job_id,
            "retry_status": retry["status"],
            "blocked_status": blocked["status"],
            "monitor_state": report["state"],
            "monitor_reason": report["reason"],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
