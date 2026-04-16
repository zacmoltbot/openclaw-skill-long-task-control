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
        summary = tmp / "batch-summary.json"
        script = tmp / "write_batch_summary.py"
        task_id = "issue8-batch-result-all-failed"
        job_id = f"{task_id}-job"

        script.write_text(
            """
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
out.write_text(json.dumps({
  \"success_count\": 0,
  \"failure_count\": 20,
  \"total_count\": 20,
  \"entries\": [
    {\"index\": i + 1, \"status\": \"failed\", \"error\": \"node/field mismatch\"}
    for i in range(20)
  ]
}, ensure_ascii=False, indent=2) + \"\\n\")
""".strip() + "\n"
        )

        bootstrap = run_json(
            "python3", str(OPS), "--ledger", str(ledger), "bootstrap-task", task_id,
            "--goal", "Run a generic shell batch step and reflect aggregated failure truth honestly",
            "--owner", "main-agent",
            "--channel", "discord",
            "--requester-channel", "issue8-regression",
            "--workflow", f"Run batch submissions :: shell=python3 {script} {summary} :: batch_result={summary}",
            "--next-action", "If all internal submissions fail, block honestly so monitor/retry can trigger",
            "--message-ref", "discord:msg:issue8",
            "--jobs-root", str(jobs_root),
            "--disabled",
        )

        first = bootstrap["execution"]["first_run"]
        assert first["status"] == "BLOCKED", first

        status = run_json("python3", str(RUNNER), "--jobs-root", str(jobs_root), "status", job_id)
        assert status["status"] == "BLOCKED", status
        assert status["blocked_reason"] == "BATCH_RESULT_THRESHOLD_NOT_MET", status

        task = task_from(ledger, task_id)
        pending = task.get("reporting", {}).get("pending_updates", [])
        event_types = [item["event_type"] for item in pending]
        latest_observation = task.get("observations", [])[-1]
        pending_block = next(item for item in pending if item["event_type"] == "BLOCKED_ESCALATE")

        assert task["status"] == "BLOCKED", task
        assert task.get("current_checkpoint") == "step-01", task
        assert "STEP_COMPLETED" not in event_types, event_types
        assert "TASK_COMPLETED" not in event_types, event_types
        assert latest_observation["event_type"] == "BLOCKED_CONFIRMED", latest_observation
        assert latest_observation["facts"]["batch_success_count"] == "0", latest_observation
        assert latest_observation["facts"]["batch_failure_count"] == "20", latest_observation
        assert latest_observation["facts"]["batch_total_count"] == "20", latest_observation
        assert latest_observation["facts"]["completion_evidence"] == "batch_result_summary", latest_observation
        assert pending_block["facts"]["batch_failure_count"] == "20", pending_block

        print(json.dumps({
            "ok": True,
            "task_id": task_id,
            "job_id": job_id,
            "first_run": first,
            "job_status": status["status"],
            "task_status": task["status"],
            "latest_observation": latest_observation,
            "pending_update_types": event_types,
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
