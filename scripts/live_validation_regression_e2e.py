#!/usr/bin/env python3
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS = ROOT / "scripts" / "openclaw_ops.py"
LEDGER_TOOL = ROOT / "scripts" / "task_ledger.py"
MONITOR = ROOT / "scripts" / "monitor_nudge.py"
CRON = ROOT / "scripts" / "monitor_cron.py"


def run(*args, check=True):
    return subprocess.run(args, check=check, text=True, capture_output=True)


def run_json(*args, check=True):
    return json.loads(run(*args, check=check).stdout)


def load(path: Path):
    return json.loads(path.read_text())


def save(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def task_from(path: Path, task_id: str):
    return next(task for task in load(path)["tasks"] if task["task_id"] == task_id)


def make_stale(path: Path, task_id: str, *, nudge_count: int = 0, age_task: bool = False):
    ledger = load(path)
    task = next(task for task in ledger["tasks"] if task["task_id"] == task_id)
    old = "2020-01-01T00:00:00+00:00"
    if age_task:
        task["created_at"] = old
    task["last_checkpoint_at"] = old
    task.setdefault("heartbeat", {})["last_progress_at"] = old
    task["heartbeat"]["last_heartbeat_at"] = old
    task.setdefault("monitoring", {})["nudge_count"] = nudge_count
    task["monitoring"]["last_nudge_at"] = old
    save(path, ledger)


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger = tmp / "state" / "long-task-ledger.json"
        cron_dir = tmp / "crons"

        # 1) GAP-1 one-shot: first NUDGE, second BLOCKED.
        task_gap = "gap1-oneshot"
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", task_gap,
            "--goal", "gap1 oneshot",
            "--requester-channel", "1484432523781083197",
            "--workflow", "step one",
            "--workflow", "step two",
            "--next-action", "do step one",
        )
        gap_data = load(ledger)
        gap_task = next(t for t in gap_data["tasks"] if t["task_id"] == task_gap)
        gap_task["workflow"][0]["state"] = "DONE"
        save(ledger, gap_data)
        make_stale(ledger, task_gap)
        gap1 = run_json("python3", str(MONITOR), "--ledger", str(ledger), "--apply-supervision")
        gap1_report = next(r for r in gap1["reports"] if r["task_id"] == task_gap)
        assert gap1_report["state"] == "NUDGE_MAIN_AGENT", gap1_report
        gap2 = run_json("python3", str(MONITOR), "--ledger", str(ledger), "--apply-supervision")
        gap2_report = next(r for r in gap2["reports"] if r["task_id"] == task_gap)
        assert gap2_report["state"] == "BLOCKED_ESCALATE", gap2_report

        # 2) owner resume contract + workflow convergence.
        task_resume = "resume-contract"
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", task_resume,
            "--goal", "resume contract",
            "--requester-channel", "1484432523781083197",
            "--workflow", "prep",
            "--workflow", "download",
            "--next-action", "prep",
        )
        make_stale(ledger, task_resume)
        first_resume = run_json("python3", str(MONITOR), "--ledger", str(ledger), "--apply-supervision")
        resume_report = next(r for r in first_resume["reports"] if r["task_id"] == task_resume)
        token = resume_report["action_payload"]["resume_token"]
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "record-update", "STARTED", task_resume,
            "--summary", "resume owner picked up step 2",
            "--current-checkpoint", "step-02",
            "--next-action", "download file",
            "--fact", "worker=owner",
            "--resume-token", token,
        )
        resume_task = task_from(ledger, task_resume)
        req = resume_task["monitoring"]["resume_requests"][-1]
        assert req["resume_token"] == token
        assert req["acknowledged_at"]
        assert req["resume_outcome"] == "STARTED"
        assert resume_task["current_checkpoint"] == "step-02"
        assert resume_task["workflow"][0]["state"] == "DONE"
        assert resume_task["workflow"][1]["state"] == "RUNNING"

        # 3) delivery push fires and acks.
        task_delivery = "delivery-push"
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", task_delivery,
            "--goal", "delivery push",
            "--requester-channel", "1484432523781083197",
            "--workflow", "do",
            "--next-action", "do",
        )
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "record-update", "CHECKPOINT", task_delivery,
            "--summary", "step done",
            "--current-checkpoint", "step-01",
            "--next-action", "continue",
            "--fact", "step=done",
        )
        run("python3", str(CRON), "--ledger", str(ledger), "--cron-dir", str(cron_dir), "install", task_delivery)
        tick = run_json("python3", str(CRON), "--ledger", str(ledger), "--cron-dir", str(cron_dir), "run-once", "--task-id", task_delivery)
        delivery_task = task_from(ledger, task_delivery)
        assert tick["delivery_push_count"] == 1, tick
        assert delivery_task["reporting"]["pending_updates"] == []
        assert delivery_task["reporting"]["delivered_updates"][0]["delivered_via"] == "monitor.delivery_push"

        # 4) transient failures retry 1, retry 2, then escalate.
        task_retry = "retry-contract"
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", task_retry,
            "--goal", "retry contract",
            "--requester-channel", "1484432523781083197",
            "--workflow", "download",
            "--workflow", "verify",
            "--next-action", "download",
        )
        for attempt in [1, 2]:
            run(
                "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "block", task_retry,
                "--reason", f"transient DOWNLOAD_TIMEOUT attempt {attempt}",
                "--safe-next-step", "retry download",
                "--current-checkpoint", "step-01",
                "--fact", "failure_type=DOWNLOAD_TIMEOUT",
            )
            report = run_json("python3", str(MONITOR), "--ledger", str(ledger), "--apply-supervision")
            retry_report = next(r for r in report["reports"] if r["task_id"] == task_retry)
            assert retry_report["state"] == "NUDGE_MAIN_AGENT", retry_report
            data = load(ledger)
            task_retry_fix = next(t for t in data["tasks"] if t["task_id"] == task_retry)
            task_retry_fix["status"] = "RUNNING"
            task_retry_fix["blocker"] = None
            save(ledger, data)
        run(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "block", task_retry,
            "--reason", "transient DOWNLOAD_TIMEOUT attempt 3",
            "--safe-next-step", "retry download",
            "--current-checkpoint", "step-01",
            "--fact", "failure_type=DOWNLOAD_TIMEOUT",
        )
        report3 = run_json("python3", str(MONITOR), "--ledger", str(ledger), "--apply-supervision")
        retry3 = next(r for r in report3["reports"] if r["task_id"] == task_retry)
        assert retry3["state"] == "BLOCKED_ESCALATE", retry3

        # 5) legit external pending with evidence stays OK.
        task_external = "external-pending-ok"
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", task_external,
            "--goal", "external pending ok",
            "--requester-channel", "1484432523781083197",
            "--workflow", "submit",
            "--workflow", "collect",
            "--next-action", "submit",
        )
        run_json(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "external-job", task_external,
            "--provider", "runninghub",
            "--job-id", "rh-123",
            "--state", "RUNNING",
            "--summary", "provider still running",
            "--current-checkpoint", "step-01",
            "--next-action", "wait",
            "--fact", "provider_status_handle=runninghub:rh-123",
        )
        make_stale(ledger, task_external)
        ext_report = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", task_external)
        assert ext_report["state"] == "OK", ext_report

        print(json.dumps({
            "ok": True,
            "gap1": gap2_report["state"],
            "resume_token": token,
            "delivery_push_count": tick["delivery_push_count"],
            "retry_final": retry3["state"],
            "external_pending": ext_report["state"],
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
