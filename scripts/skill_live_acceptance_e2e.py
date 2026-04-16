#!/usr/bin/env python3
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS = ROOT / "scripts" / "openclaw_ops.py"
RUNNER = ROOT / "scripts" / "runner_engine.py"


def run(*args, env=None):
    return subprocess.run(args, check=True, text=True, capture_output=True, env=env)


def run_json(*args, env=None):
    return json.loads(run(*args, env=env).stdout)


def load(path: Path):
    return json.loads(path.read_text())


def task_from(ledger_path: Path, task_id: str):
    return next(task for task in load(ledger_path)["tasks"] if task["task_id"] == task_id)


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger = tmp / "ledger.json"
        jobs_root = tmp / "jobs"
        out_dir = tmp / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        inspected = out_dir / "inspected.txt"
        draft = out_dir / "draft.txt"
        validated = out_dir / "validated.txt"
        task_id = "acceptance-demo"
        job_id = f"{task_id}-job"

        env = os.environ.copy()

        bootstrap = run_json(
            "python3", str(OPS), "--ledger", str(ledger), "bootstrap-task", task_id,
            "--goal", "Inspect inputs, build draft artifact, validate, and hand off",
            "--owner", "main-agent",
            "--channel", "discord",
            "--requester-channel", "discord:channel:1477237887136432162",
            "--workflow", f"Inspect inputs :: shell=printf 'inspected\n' > {inspected} :: expect={inspected}",
            "--workflow", f"Build draft artifact :: shell=cat {inspected} > {draft} && printf 'draft\n' >> {draft} :: expect={draft}",
            "--workflow", f"Validate and hand off :: shell=grep -q draft {draft} && cp {draft} {validated} :: expect={validated}",
            "--next-action", "Start step-01 and publish the first observed update",
            "--message-ref", "discord:msg:acceptance-demo",
            "--jobs-root", str(jobs_root),
            "--disabled",
            env=env,
        )
        assert bootstrap["task_id"] == task_id
        assert "ACTIVATED" in bootstrap["activation"]
        assert "TASK START" in bootstrap["task_start"]
        assert bootstrap["execution"]["mode"] == "canonical-execution-first"

        init_job = bootstrap["execution"]["job"]
        first = bootstrap["execution"]["first_run"]
        assert init_job["job_id"] == job_id
        assert Path(init_job["spec_path"]).exists()
        assert first["status"] == "COMPLETED"
        assert first["steps_executed"] == 3

        job_status = run_json("python3", str(RUNNER), "--jobs-root", str(jobs_root), "status", job_id)
        assert job_status["status"] == "COMPLETED"
        assert job_status["completed"] == ["step-01", "step-02", "step-03"]
        assert inspected.exists()
        assert draft.exists()
        assert validated.exists()

        task_state = task_from(ledger, task_id)
        pending = task_state.get("reporting", {}).get("pending_updates", [])
        delivered = task_state.get("reporting", {}).get("delivered_updates", [])
        delivered_types = [item["event_type"] for item in delivered]
        assert task_state["status"] == "COMPLETED"
        assert task_state["heartbeat"]["watchdog_state"] == "STOP_AND_DELETE"
        assert pending == []
        assert delivered_types.count("STEP_COMPLETED") == 3
        assert "COMPLETED_HANDOFF" in delivered_types
        assert bootstrap["execution"]["first_run"]["delivery_flush"]["pending_remaining"] == 0
        assert bootstrap["execution"]["first_run"]["delivery_flush"]["delivered_count"] == 4
        assert task_state["message"]["requester_channel_valid"] is True
        assert "1477237887136432162" in str(task_state["message"]["requester_channel"])

        preview = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", task_id, env=env)
        assert preview["state"] == "STOP_AND_DELETE"
        assert preview["pending_user_updates_deliverable_count"] == 0

        print(json.dumps({
            "ok": True,
            "task_id": task_id,
            "job_id": job_id,
            "ledger": str(ledger),
            "jobs_root": str(jobs_root),
            "job_status": job_status["status"],
            "task_status": task_state["status"],
            "delivered_update_types": delivered_types,
            "requester_channel": task_state["message"]["requester_channel"],
            "monitor_state": preview["state"],
            "artifacts": [str(inspected), str(draft), str(validated)],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
