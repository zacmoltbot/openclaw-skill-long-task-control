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
    return json.loads(run(*args, check=check).stdout)


def load(path: Path):
    return json.loads(path.read_text())


def save(path: Path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def task_from(path: Path, task_id: str):
    return next(task for task in load(path)["tasks"] if task["task_id"] == task_id)


def make_stale(path: Path, task_id: str):
    ledger = load(path)
    task = next(task for task in ledger["tasks"] if task["task_id"] == task_id)
    old = "2020-01-01T00:00:00+00:00"
    task["last_checkpoint_at"] = old
    task.setdefault("heartbeat", {})["last_progress_at"] = old
    task["heartbeat"]["last_heartbeat_at"] = old
    save(path, ledger)


def init_task(path: Path, task_id: str, goal: str):
    run_json(
        "python3", str(OPS), "--ledger", str(path), "init-task", task_id,
        "--goal", goal,
        "--requester-channel", "1484432523781083197",
        "--workflow", "submit external",
        "--workflow", "collect result",
        "--next-action", "submit external",
    )


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger = tmp / "state" / "long-task-ledger.json"

        # A) legit external pending with evidence -> OK/noop
        task_a = "evidence-a"
        init_task(ledger, task_a, "Legit external pending should remain OK")
        run_json(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "external-job", task_a,
            "--provider", "runninghub", "--job-id", "rh-legit", "--state", "RUNNING",
            "--summary", "Provider accepted job and it is running",
            "--current-checkpoint", "step-01", "--next-action", "wait",
            "--fact", "provider_status_handle=runninghub:rh-legit",
        )
        make_stale(ledger, task_a)
        report_a = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", task_a)
        assert report_a["state"] == "OK", report_a

        # B) weak/fake pending claim -> OWNER_RECONCILE
        task_b = "evidence-b"
        init_task(ledger, task_b, "Weak external pending should trigger reconcile")
        data_b = load(ledger)
        task_b_obj = next(task for task in data_b["tasks"] if task["task_id"] == task_b)
        task_b_obj["external_jobs"].append({
            "provider": "runninghub",
            "job_id": "",
            "status": "RUNNING",
            "pending_external": True,
            "history": [{"at": "2026-04-12T00:00:00+00:00", "state": "RUNNING", "summary": "owner says still running", "facts": {}}],
        })
        save(ledger, data_b)
        make_stale(ledger, task_b)
        report_b = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", task_b)
        assert report_b["state"] == "OWNER_RECONCILE", report_b

        # C) owner can supply evidence -> stays RUNNING/OK
        data_b_fix = load(ledger)
        task_b_fix = next(task for task in data_b_fix["tasks"] if task["task_id"] == task_b)
        weak_job = task_b_fix["external_jobs"][0]
        weak_job["job_id"] = "rh-fixed"
        weak_job["provider_evidence"] = {
            "provider_job_id": "rh-fixed",
            "provider_status_handle": "runninghub:rh-fixed",
            "submission_receipt": "receipt-123",
        }
        save(ledger, data_b_fix)
        run_json(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "external-job", task_b,
            "--provider", "runninghub", "--job-id", "rh-fixed", "--state", "RUNNING",
            "--summary", "Owner reconciled with real provider handle",
            "--current-checkpoint", "step-01", "--next-action", "wait",
            "--fact", "provider_status_handle=runninghub:rh-fixed",
            "--fact", "submission_receipt=receipt-123",
        )
        report_c = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", task_b)
        assert report_c["state"] == "OK", report_c
        assert task_from(ledger, task_b)["status"] == "RUNNING"

        # D) owner still cannot supply evidence after repeated reconcile -> escalation/cleanup
        task_d = "evidence-d"
        init_task(ledger, task_d, "Repeated weak pending claims should escalate")
        data_d = load(ledger)
        task_d_obj = next(task for task in data_d["tasks"] if task["task_id"] == task_d)
        task_d_obj["external_jobs"].append({
            "provider": "runninghub",
            "job_id": None,
            "status": "PENDING",
            "pending_external": True,
            "history": [{"at": "2026-04-12T00:00:00+00:00", "state": "PENDING", "summary": "owner says queued", "facts": {}}],
        })
        save(ledger, data_d)
        make_stale(ledger, task_d)
        r1 = run_json("python3", str(MONITOR), "--ledger", str(ledger), "--apply-supervision")
        d1 = next(item for item in r1["reports"] if item["task_id"] == task_d)
        assert d1["state"] == "OWNER_RECONCILE", d1
        r2 = run_json("python3", str(MONITOR), "--ledger", str(ledger), "--apply-supervision")
        d2 = next(item for item in r2["reports"] if item["task_id"] == task_d)
        assert d2["state"] == "OWNER_RECONCILE", d2
        r3 = run_json("python3", str(MONITOR), "--ledger", str(ledger), "--apply-supervision")
        d3 = next(item for item in r3["reports"] if item["task_id"] == task_d)
        assert d3["state"] == "BLOCKED_ESCALATE", d3
        final_d = task_from(ledger, task_d)
        assert final_d["monitoring"]["cron_state"] == "DELETE_REQUESTED"

        print(json.dumps({
            "ok": True,
            "ledger": str(ledger),
            "scenarios": {
                "A": report_a["state"],
                "B": report_b["state"],
                "C": report_c["state"],
                "D": d3["state"],
            },
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
