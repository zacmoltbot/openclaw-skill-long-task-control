#!/usr/bin/env python3
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS = ROOT / "scripts" / "openclaw_ops.py"
RUNNER = ROOT / "scripts" / "runner_engine.py"


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True)


def run_json(*args):
    return json.loads(run(*args).stdout)


def load(path: Path):
    return json.loads(path.read_text())


def task_from(ledger_path: Path, task_id: str):
    return next(task for task in load(ledger_path)["tasks"] if task["task_id"] == task_id)


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger = tmp / "ledger.json"
        jobs_root = tmp / "jobs"
        task_id = "issue7-research-scouting"
        job_id = f"{task_id}-job"

        bootstrap = run_json(
            "python3", str(OPS), "--ledger", str(ledger), "bootstrap-task", task_id,
            "--goal", "Scout RunningHub apps/workflows and design prompts",
            "--owner", "main-agent",
            "--channel", "discord",
            "--requester-channel", "issue7-regression",
            "--workflow", "Scout candidate RunningHub apps/workflows",
            "--workflow", "Compare fit and pick prompt strategy",
            "--next-action", "Start with real scouting work or block honestly if execution semantics do not exist",
            "--message-ref", "discord:msg:issue7",
            "--jobs-root", str(jobs_root),
            "--disabled",
        )

        assert bootstrap["execution"]["mode"] == "canonical-execution-first"
        first = bootstrap["execution"]["first_run"]
        assert first["status"] == "BLOCKED", first

        status = run_json("python3", str(RUNNER), "--jobs-root", str(jobs_root), "status", job_id)
        assert status["status"] == "BLOCKED", status

        task = task_from(ledger, task_id)
        pending = task.get("reporting", {}).get("pending_updates", [])
        event_types = [item["event_type"] for item in pending]

        assert task["status"] == "BLOCKED", task
        assert pending[0]["summary"] == "OWNER_ACTION_REQUIRED", pending
        assert "STEP_COMPLETED" not in event_types, event_types
        assert "TASK_COMPLETED" not in event_types, event_types
        assert "COMPLETED_HANDOFF" not in event_types, event_types
        assert any(item["event_type"] == "BLOCKED_ESCALATE" and item.get("source_kind") == "BLOCKED_CONFIRMED" for item in pending), pending
        assert task.get("current_checkpoint") == "step-01", task.get("current_checkpoint")

        print(json.dumps({
            "ok": True,
            "task_id": task_id,
            "job_id": job_id,
            "first_run": first,
            "job_status": status["status"],
            "task_status": task["status"],
            "pending_update_types": event_types,
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
