#!/usr/bin/env python3
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS = ROOT / "scripts" / "openclaw_ops.py"
RUNNER = ROOT / "scripts" / "runner_engine.py"


def run(*args, check=True):
    return subprocess.run(args, text=True, capture_output=True, check=check)


def run_json(*args, check=True):
    return json.loads(run(*args, check=check).stdout)


def load(path: Path):
    return json.loads(path.read_text())


def task_from(path: Path, task_id: str):
    return next(task for task in load(path)["tasks"] if task["task_id"] == task_id)


def main():
    cases = [
        ("normalize-discord-target", "normalize requester discord target"),
        ("discord-target-repair", "repair discord target config"),
        ("discord-self-heal", "self-heal discord target config"),
    ]

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ledger = tmp / "ledger.json"
        jobs_root = tmp / "jobs"
        results = []

        for task_id, goal in cases:
            boot = run_json(
                "python3", str(OPS), "--ledger", str(ledger), "bootstrap-task", task_id,
                "--goal", goal,
                "--channel", "discord",
                "--requester-channel", "bad_username_target",
                "--session-key", "agent:main:discord:channel:1477237887136432162",
                "--workflow", "Do one step",
                "--next-action", "Run",
                "--jobs-root", str(jobs_root),
                "--disabled",
                "--dry-run",
            )
            init_job = boot["execution"]["job"]
            first_run = boot["execution"]["first_run"]
            job_status = run_json(
                "python3", str(RUNNER), "--jobs-root", str(jobs_root), "status", init_job["job_id"],
            )
            task = task_from(ledger, task_id)
            results.append({
                "task_id": task_id,
                "goal": goal,
                "first_run": first_run,
                "job_status": job_status,
                "task_status": task["status"],
                "requester_channel": task["message"]["requester_channel"],
                "requester_channel_source": task["message"].get("requester_channel_source"),
                "blocker": task.get("blocker"),
                "pending_updates": task.get("reporting", {}).get("pending_updates", []),
            })

            assert first_run["status"] == "COMPLETED", results[-1]
            assert job_status["status"] == "COMPLETED", results[-1]
            assert task["status"] == "COMPLETED", results[-1]
            assert task["message"]["requester_channel"] == "1477237887136432162", results[-1]
            assert task["message"].get("requester_channel_source") == "session_key_fallback", results[-1]
            assert task.get("blocker") in (None, {}), results[-1]
            assert not any(update.get("event_type") == "BLOCKED_ESCALATE" for update in task.get("reporting", {}).get("pending_updates", [])), results[-1]

        print(json.dumps({"ok": True, "cases": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
