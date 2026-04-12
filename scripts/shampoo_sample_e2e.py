#!/usr/bin/env python3
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASK_LEDGER = ROOT / "scripts" / "task_ledger.py"
OPENCLAW_OPS = ROOT / "scripts" / "openclaw_ops.py"
MONITOR = ROOT / "scripts" / "monitor_nudge.py"


def run(*args, check=True):
    return subprocess.run(args, check=check, text=True, capture_output=True)


def run_json(*args, check=True):
    proc = run(*args, check=check)
    return json.loads(proc.stdout)


def load(path):
    return json.loads(Path(path).read_text())


def task_from(ledger_path, task_id):
    ledger = load(ledger_path)
    return next(task for task in ledger["tasks"] if task["task_id"] == task_id)


def set_old(task, *, nudge_count=0, age_task=False):
    old = "2020-01-01T00:00:00+00:00"
    if age_task:
        task["created_at"] = old
    task["last_checkpoint_at"] = old
    task.setdefault("heartbeat", {})["last_progress_at"] = old
    task["heartbeat"]["last_heartbeat_at"] = old
    task.setdefault("monitoring", {})["nudge_count"] = nudge_count
    task["monitoring"]["last_nudge_at"] = old


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger = tmp / "state" / "long-task-ledger.json"
        task_id = "shampoo-30s-20260412-a"
        output_dir = tmp / "artifacts"
        output_dir.mkdir(parents=True, exist_ok=True)
        final_video = output_dir / "shampoo-ad-30s.mp4"
        final_video.write_bytes(b"FAKE_MP4_DATA")

        activation_payload = run_json(
            "python3", str(OPENCLAW_OPS), "--ledger", str(ledger), "bootstrap-task", task_id,
            "--goal", "Deliver a 30-second shampoo ad sample with verified lifecycle control",
            "--owner", "main-agent",
            "--channel", "discord",
            "--requester-channel", "1484432523781083197",
            "--workflow", "Draft treatment",
            "--workflow", "Render sample via RunningHub",
            "--workflow", "Validate and handoff",
            "--message-ref", "discord:msg:shampoo-activation",
            "--summary", "Activation announced and shampoo sample task initialized",
            "--fact", "activation_message=emitted",
            "--fact", "sample_name=30s_shampoo_ad",
            "--next-action", "Prepare treatment outline and create first checkpoint",
            "--expected-interval-sec", "300",
            "--timeout-sec", "300",
            "--nudge-after-sec", "300",
            "--renotify-interval-sec", "300",
            "--escalate-after-nudges", "1",
            "--max-nudges", "3",
            "--blocked-escalate-after-sec", "300",
            "--every", "5m",
            "--disabled"
        )
        assert activation_payload["job"]["schedule"]["kind"] == "every"
        assert activation_payload["job"]["schedule"]["everyMs"] == 300000

        run_json(
            "python3", str(OPENCLAW_OPS), "--ledger", str(ledger), "record-update", "STEP_PROGRESS", task_id,
            "--summary", "Treatment drafting started",
            "--current-checkpoint", "step-01",
            "--next-action", "Submit RunningHub render job",
            "--fact", "activation_message=emitted",
        )

        external_submitted = run_json(
            "python3", str(TASK_LEDGER), "--ledger", str(ledger), "external-job", task_id,
            "--provider", "runninghub",
            "--job-id", "rh-job-001",
            "--state", "SUBMITTED",
            "--workflow", "wf-a",
            "--app", "runninghub-app-a",
            "--summary", "Submitted RunningHub shampoo render job",
            "--current-checkpoint", "step-02",
            "--next-action", "Wait for RunningHub status callback",
            "--fact", "latest_status=submitted",
        )
        assert external_submitted["pending_external"] is True

        running = run_json(
            "python3", str(TASK_LEDGER), "--ledger", str(ledger), "external-job", task_id,
            "--provider", "runninghub",
            "--job-id", "rh-job-001",
            "--state", "RUNNING",
            "--workflow", "wf-a",
            "--app", "runninghub-app-a",
            "--summary", "RunningHub render is running",
            "--current-checkpoint", "step-02",
            "--next-action", "Wait for RunningHub completion",
            "--fact", "latest_status=running",
        )
        assert running["job"]["status"] == "RUNNING"

        task = load(ledger)
        inner = next(item for item in task["tasks"] if item["task_id"] == task_id)
        set_old(inner, nudge_count=0)
        inner["external_jobs"][0]["pending_external"] = True
        inner["external_jobs"][0]["status"] = "RUNNING"
        ledger.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n")

        pending_ok = run_json("python3", str(OPENCLAW_OPS), "--ledger", str(ledger), "preview-tick", task_id)
        assert pending_ok["state"] == "OK"
        assert pending_ok["notify"] is False

        failed_once = run_json(
            "python3", str(TASK_LEDGER), "--ledger", str(ledger), "external-job", task_id,
            "--provider", "runninghub",
            "--job-id", "rh-job-001",
            "--state", "FAILED",
            "--workflow", "wf-a",
            "--app", "runninghub-app-a",
            "--summary", "RunningHub workflow A failed",
            "--failure-type", "EXTERNAL_WAIT",
            "--current-checkpoint", "step-02",
            "--next-action", "Retry with alternative workflow",
            "--fact", "latest_status=failed",
        )
        assert failed_once["job"]["failure_count"] == 1

        switched = run_json(
            "python3", str(TASK_LEDGER), "--ledger", str(ledger), "external-job", task_id,
            "--provider", "runninghub",
            "--job-id", "rh-job-002",
            "--state", "SWITCHED_WORKFLOW",
            "--workflow", "wf-b",
            "--app", "runninghub-app-b",
            "--summary", "Switched to alternative workflow after two app/workflow failures",
            "--current-checkpoint", "step-02",
            "--next-action", "Wait for alternative workflow result",
            "--fact", "latest_status=switched_workflow",
        )
        assert switched["job"]["switch_count"] == 1

        completed_external = run_json(
            "python3", str(TASK_LEDGER), "--ledger", str(ledger), "external-job", task_id,
            "--provider", "runninghub",
            "--job-id", "rh-job-002",
            "--state", "COMPLETED",
            "--workflow", "wf-b",
            "--app", "runninghub-app-b",
            "--summary", "Alternative RunningHub workflow completed",
            "--current-checkpoint", "step-03",
            "--next-action", "Validate output file and handoff",
            "--fact", f"output_file={final_video}",
            "--fact", "latest_status=completed",
        )
        assert completed_external["pending_external"] is False

        completed_payload = run_json(
            "python3", str(OPENCLAW_OPS), "--ledger", str(ledger), "record-update", "TASK_COMPLETED", task_id,
            "--summary", "Rendered shampoo sample and validated output file",
            "--current-checkpoint", "step-03",
            "--next-action", "None",
            "--output", str(final_video),
            "--completed-checkpoint", "step-01",
            "--completed-checkpoint", "step-02",
            "--completed-checkpoint", "step-03",
            "--fact", f"output_file={final_video}",
            "--fact", f"size_bytes={final_video.stat().st_size}",
            "--fact", "duration_sec=30"
        )
        assert completed_payload["task_status"] == "COMPLETED"
        assert any(item.startswith("artifact_exists[") for item in completed_payload["validation"])

        cleanup_result = run_json("python3", str(OPENCLAW_OPS), "--ledger", str(ledger), "preview-tick", task_id)
        assert cleanup_result["state"] == "STOP_AND_DELETE"
        removed = run_json("python3", str(OPENCLAW_OPS), "--ledger", str(ledger), "remove-monitor", task_id, "--dry-run")
        assert removed["removed"] is True

        final_task = task_from(ledger, task_id)
        assert final_task["monitoring"]["cron_state"] == "DELETED"

        print(json.dumps({
            "ok": True,
            "task_id": task_id,
            "states": {
                "pending_external": pending_ok["state"],
                "terminal": cleanup_result["state"],
            },
            "external_jobs": [
                {"job_id": job["job_id"], "status": job["status"], "workflow": job.get("workflow"), "app": job.get("app")}
                for job in final_task["external_jobs"]
            ],
            "ledger": str(ledger),
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

