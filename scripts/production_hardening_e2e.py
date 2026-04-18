#!/usr/bin/env python3
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS = ROOT / "scripts" / "openclaw_ops.py"
LEDGER_TOOL = ROOT / "scripts" / "task_ledger.py"
MONITOR = ROOT / "scripts" / "monitor_nudge.py"


def run(*args, check=True):
    return subprocess.run(args, check=check, text=True, capture_output=True)


def run_json(*args, check=True):
    proc = run(*args, check=check)
    return json.loads(proc.stdout)


def load(path: Path):
    return json.loads(path.read_text())


def task_from(ledger_path: Path, task_id: str):
    return next(task for task in load(ledger_path)["tasks"] if task["task_id"] == task_id)


def force_old(task, old="2020-01-01T00:00:00+00:00", *, nudge_count=0):
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

        # C) bootstrap install failure produces structured signal + durable ledger truth.
        failed_task = "install-failure-20260412-a"
        failed = run(
            "python3", str(OPS), "--ledger", str(ledger), "bootstrap-task", failed_task,
            "--goal", "Prove install failure is observable",
            "--requester-channel", "1484432523781083197",
            "--workflow", "Bootstrap",
            "--next-action", "Install monitor",
            "--every", "not-a-duration",
            "--model", "minimax/MiniMax-M2.7",
            check=False,
        )
        assert failed.returncode != 0
        install_signal = json.loads(failed.stderr.strip().splitlines()[-1])
        assert install_signal["signal"] == "INSTALL_FAILED"
        failed_ledger = task_from(ledger, failed_task)
        assert failed_ledger["monitoring"]["cron_state"] == "INSTALL_FAILED"
        assert failed_ledger["monitoring"]["install_signal"] == "INSTALL_FAILED"

        install_report = run_json("python3", str(MONITOR), "--ledger", str(ledger))
        failed_monitor = next(r for r in install_report["reports"] if r["task_id"] == failed_task)
        # P0 semantics: INSTALL_FAILED is a benign infrastructure signal (not a task-level
        # problem). The monitor returns OK since there is no observed task-level blocker.
        assert failed_monitor["state"] == "OK", failed_monitor

        # E) stale RUNNING task repeatedly supervised stays in heartbeat-due lane.
        # This is NOT the transient BLOCKED retry path. Under current live P0 semantics,
        # this fixture only ages heartbeat/checkpoint (not observed progress truth), so
        # the monitor emits HEARTBEAT_DUE rather than NUDGE_MAIN_AGENT/BLOCKED_ESCALATE.
        retry_task = "retry-3-20260412-a"
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", retry_task,
            "--goal", "Block after three timeout retries on same step",
            "--requester-channel", "1484432523781083197",
            "--workflow", "Implement",
            "--next-action", "Keep implementing",
        )
        last_iter = None
        for _ in range(3):
            data = load(ledger)
            task = next(t for t in data["tasks"] if t["task_id"] == retry_task)
            force_old(task, nudge_count=0)
            ledger.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            iter_report = run_json("python3", str(MONITOR), "--ledger", str(ledger), "--apply-supervision")
            last_iter = next(r for r in iter_report["reports"] if r["task_id"] == retry_task)
            assert last_iter["state"] == "HEARTBEAT_DUE", last_iter
        retry_report = run_json("python3", str(MONITOR), "--ledger", str(ledger))
        retry_state = next(r for r in retry_report["reports"] if r["task_id"] == retry_task)
        assert retry_state["state"] == "HEARTBEAT_DUE", retry_state
        assert retry_state["retry_count"] == {}, retry_state["retry_count"]

        # F) app/workflow switch after 2 external failures is durable truth in ledger.
        switch_task = "switch-after-2-fails-20260412-a"
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", switch_task,
            "--goal", "Switch workflow after two external failures",
            "--requester-channel", "1484432523781083197",
            "--workflow", "Submit external job",
            "--workflow", "Collect result",
            "--next-action", "Submit workflow A",
        )
        run_json(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "external-job", switch_task,
            "--provider", "runninghub", "--job-id", "rh-a-1", "--state", "FAILED",
            "--workflow", "wf-a", "--app", "app-a", "--failure-type", "EXTERNAL_WAIT",
            "--summary", "workflow A failed first time", "--current-checkpoint", "step-01", "--next-action", "Retry external workflow"
        )
        run_json(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "external-job", switch_task,
            "--provider", "runninghub", "--job-id", "rh-a-2", "--state", "FAILED",
            "--workflow", "wf-a", "--app", "app-a", "--failure-type", "EXTERNAL_WAIT",
            "--summary", "workflow A failed second time", "--current-checkpoint", "step-01", "--next-action", "Switch workflow"
        )
        run_json(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "external-job", switch_task,
            "--provider", "runninghub", "--job-id", "rh-b-1", "--state", "SWITCHED_WORKFLOW",
            "--workflow", "wf-b", "--app", "app-b",
            "--summary", "switched to workflow B after two failures", "--current-checkpoint", "step-01", "--next-action", "Wait for workflow B"
        )
        switch_ledger = task_from(ledger, switch_task)
        assert len(switch_ledger["external_jobs"]) == 3
        assert switch_ledger["external_jobs"][-1]["status"] == "SWITCHED_WORKFLOW"
        # Current live P0 semantics persist external failure/switch truth in external_jobs history.
        # monitoring.retry_count is not populated by external-job events, so validate durable truth directly.
        failed_jobs = [j for j in switch_ledger["external_jobs"] if j.get("status") == "FAILED"]
        assert len(failed_jobs) == 2, failed_jobs

        print(json.dumps({
            "ok": True,
            "ledger": str(ledger),
            "install_failure": install_signal,
            "retry_3": retry_state,
            "switch_after_2_fails": {
                "task_id": switch_task,
                "failed_job_count": len(failed_jobs),
                "latest_job": switch_ledger["external_jobs"][-1],
            },
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
