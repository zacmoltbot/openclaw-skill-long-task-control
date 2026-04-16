#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER_TOOL = ROOT / "scripts" / "task_ledger.py"
RUNNER = ROOT / "scripts" / "runner_engine.py"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, text=True, capture_output=True, check=True)


def run_json(*args: str):
    return json.loads(run(*args).stdout)


def load_json(path: Path):
    return json.loads(path.read_text())


def task_from(ledger_path: Path, task_id: str):
    return next(task for task in load_json(ledger_path)["tasks"] if task["task_id"] == task_id)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        ledger = root / "state" / "long-task-ledger.json"
        jobs_root = root / "state" / "jobs"
        spec = root / "demo-job.json"
        artifact = root / "artifacts" / "result.txt"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("mvp-ok\n")

        task_id = "execution-mvp-demo"
        job_id = "execution-mvp-demo-job"

        run(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "init", task_id,
            "--goal", "Prove execution-plane bridge + resume skeleton",
            "--owner", "main-agent",
            "--channel", "discord",
            "--workflow", "Execute item 1",
            "--workflow", "Execute item 2",
            "--next-action", "Run execution-plane loop",
        )

        spec.write_text(json.dumps({
            "job_id": job_id,
            "kind": "demo",
            "adapter": "generic_manual",
            "mode": "serial",
            "bridge": {
                "ledger": str(ledger),
                "task_id": task_id,
            },
            "items": [
                {"item_id": "item-01", "title": "Execute item 1", "checkpoint": "step-01", "generic_manual_mode": "synthetic_demo"},
                {"item_id": "item-02", "title": "Execute item 2", "checkpoint": "step-02", "artifact": str(artifact), "generic_manual_mode": "synthetic_demo"},
            ],
        }, ensure_ascii=False, indent=2) + "\n")

        run_json("python3", str(RUNNER), "--jobs-root", str(jobs_root), "init-job", str(spec))
        first = run_json("python3", str(RUNNER), "--jobs-root", str(jobs_root), "run-loop", job_id, "--max-steps", "1", "--execution-owner", "demo-first-pass")

        ledger_after_first = task_from(ledger, task_id)
        pending_after_first = ledger_after_first["reporting"]["pending_updates"]
        assert first["status"] == "RUNNING"
        assert ledger_after_first["status"] == "RUNNING"
        assert len(pending_after_first) >= 1
        assert any(update["event_type"] == "STEP_COMPLETED" for update in pending_after_first)

        job_file = jobs_root / job_id / "job.json"
        job_data = load_json(job_file)
        job_data["items"][1]["status"] = "RUNNING"
        job_data["items"][1]["execution_owner"] = "crashed-worker"
        job_data["items"][1]["execution_claimed_at"] = "2026-01-01T00:00:00+00:00"
        job_data["current_index"] = 1
        job_file.write_text(json.dumps(job_data, ensure_ascii=False, indent=2) + "\n")

        preview = run_json("python3", str(ROOT / "scripts" / "executor_engine.py"), "--jobs-root", str(jobs_root), "preview", job_id)
        assert preview["action"] == "execute_item"
        assert preview["item_id"] == "item-02"
        assert preview["resume_count"] >= 1

        second = run_json("python3", str(RUNNER), "--jobs-root", str(jobs_root), "run-loop", job_id, "--execution-owner", "demo-resume-pass")
        final_ledger = task_from(ledger, task_id)
        final_job = load_json(job_file)
        events = [u["event_type"] for u in final_ledger["reporting"]["pending_updates"]]

        assert second["status"] == "COMPLETED"
        assert final_ledger["status"] == "COMPLETED"
        assert "STEP_COMPLETED" in events
        assert "COMPLETED_HANDOFF" in events
        assert final_job["items"][1]["resume_count"] >= 1

        print(json.dumps({
            "ok": True,
            "ledger": str(ledger),
            "jobs_root": str(jobs_root),
            "task_id": task_id,
            "job_id": job_id,
            "first_run": first,
            "resume_preview": preview,
            "final_status": final_ledger["status"],
            "pending_update_types": events,
            "resume_count_item_02": final_job["items"][1]["resume_count"],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
