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
COMPLIANCE = ROOT / "scripts" / "compliance_check.py"


def run(*args, check=True):
    return subprocess.run(args, check=check, text=True, capture_output=True)


def run_json(*args, check=True):
    return json.loads(run(*args, check=check).stdout)


def load(path: Path):
    return json.loads(path.read_text())


def task_from(path: Path, task_id: str):
    return next(task for task in load(path)["tasks"] if task["task_id"] == task_id)


def make_stale(path: Path, task_id: str, *, nudge_count: int = 0, age_task: bool = True):
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
    path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")


def latest_pending(task):
    return task["reporting"]["pending_updates"][-1]


def ack(path: Path, task_id: str, update_id: str, note: str):
    return run_json(
        "python3", str(LEDGER_TOOL), "--ledger", str(path), "ack-delivery", task_id, update_id,
        "--message-ref", f"discord:msg:{update_id}", "--note", note,
    )


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        ledger = tmp / "state" / "long-task-ledger.json"
        cron_dir = tmp / "state" / "monitor-crons"
        artifact = tmp / "artifacts" / "result.txt"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("ok\n")

        # A) bootstrap success -> monitor ACTIVE -> step complete -> user-visible checkpoint required.
        task_a = "case-a-bootstrap"
        boot_a = run_json(
            "python3", str(OPS), "--ledger", str(ledger), "bootstrap-task", task_a,
            "--goal", "Verify bootstrap and required step checkpoint delivery",
            "--requester-channel", "1484432523781083197",
            "--workflow", "Inspect",
            "--workflow", "Implement",
            "--next-action", "Start step 1",
            "--disabled",
        )
        assert boot_a["job"]["id"]
        assert task_from(ledger, task_a)["monitoring"]["cron_state"] == "DISABLED"
        step_a = run_json(
            "python3", str(OPS), "--ledger", str(ledger), "record-update", "STEP_COMPLETED", task_a,
            "--summary", "Step 1 completed",
            "--current-checkpoint", "step-01",
            "--next-action", "Start step 2",
            "--fact", "step=1",
        )
        assert step_a["pending_user_update"]["event_type"] == "STEP_COMPLETED"
        ack(ledger, task_a, step_a["pending_user_update"]["update_id"], "Delivered step completion update")

        # B) external job pending -> not stale -> complete => checkpoint artifact update.
        task_b = "case-b-external-pending"
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", task_b,
            "--goal", "Verify pending external jobs do not get misclassified",
            "--requester-channel", "1484432523781083197",
            "--workflow", "Submit job",
            "--workflow", "Collect",
            "--next-action", "Submit job",
        )
        run_json(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "external-job", task_b,
            "--provider", "runninghub", "--job-id", "rh-pending", "--state", "RUNNING",
            "--summary", "External job is running", "--current-checkpoint", "step-01", "--next-action", "Wait for completion",
        )
        make_stale(ledger, task_b, age_task=False)
        preview_b1 = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", task_b)
        assert preview_b1["state"] == "OK"
        run_json(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "external-job", task_b,
            "--provider", "runninghub", "--job-id", "rh-pending", "--state", "COMPLETED",
            "--summary", "External job completed successfully", "--current-checkpoint", "step-02", "--next-action", "Download artifact",
            "--fact", f"artifact_path={artifact}",
        )
        task_b_ledger = task_from(ledger, task_b)
        pending_b = latest_pending(task_b_ledger)
        assert pending_b["event_type"] == "EXTERNAL_JOB_COMPLETED"
        assert any("artifact_path" in line for line in pending_b["status_block"].splitlines())
        ack(ledger, task_b, pending_b["update_id"], "Delivered external job completion update")

        # C) external job failed -> retry/switch -> user-visible updates required.
        task_c = "case-c-external-fail"
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", task_c,
            "--goal", "Verify external job failure and workflow switch reporting",
            "--requester-channel", "1484432523781083197",
            "--workflow", "Submit workflow A",
            "--workflow", "Switch if needed",
            "--next-action", "Submit workflow A",
        )
        run_json(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "external-job", task_c,
            "--provider", "runninghub", "--job-id", "rh-fail", "--state", "FAILED",
            "--summary", "Workflow A failed", "--current-checkpoint", "step-01", "--next-action", "Retry or switch",
            "--failure-type", "EXTERNAL_WAIT",
        )
        pending_c1 = latest_pending(task_from(ledger, task_c))
        assert pending_c1["event_type"] == "EXTERNAL_JOB_FAILED"
        ack(ledger, task_c, pending_c1["update_id"], "Delivered failure update")
        run_json(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "external-job", task_c,
            "--provider", "runninghub", "--job-id", "rh-switch", "--state", "SWITCHED_WORKFLOW",
            "--summary", "Switched to workflow B", "--workflow", "wf-b", "--app", "app-b",
            "--current-checkpoint", "step-02", "--next-action", "Wait for workflow B",
        )
        pending_c2 = latest_pending(task_from(ledger, task_c))
        assert pending_c2["event_type"] == "WORKFLOW_SWITCH"
        ack(ledger, task_c, pending_c2["update_id"], "Delivered switch update")

        # D) install failure -> durable infra signal, but not a task-level blocker.
        task_d = "case-d-install-failure"
        failed = run(
            "python3", str(OPS), "--ledger", str(ledger), "bootstrap-task", task_d,
            "--goal", "Force install failure",
            "--requester-channel", "1484432523781083197",
            "--workflow", "Bootstrap",
            "--next-action", "Install monitor",
            "--every", "not-a-duration",
            check=False,
        )
        assert failed.returncode != 0
        install_signal = json.loads(failed.stderr.strip().splitlines()[-1])
        assert install_signal["signal"] == "INSTALL_FAILED"
        preview_d = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", task_d)
        assert preview_d["state"] == "OK"

        # E) blocked escalate -> stop/delete cron + one-shot full report.
        task_e = "case-e-blocked"
        run(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", task_e,
            "--goal", "Escalate blocker once then cleanup",
            "--requester-channel", "1484432523781083197",
            "--workflow", "Inspect",
            "--workflow", "Escalate blocker",
            "--next-action", "Inspect",
        )
        run("python3", str(CRON), "--ledger", str(ledger), "--cron-dir", str(cron_dir), "install", task_e)
        run(
            "python3", str(LEDGER_TOOL), "--ledger", str(ledger), "owner-reply", task_e,
            "--reply", "B", "--summary", "Need credentials", "--reason", "Missing credentials",
            "--need", "Provide credentials", "--safe-next-step", "Retry after credentials", "--next-action", "Wait",
        )
        pending_e = latest_pending(task_from(ledger, task_e))
        assert pending_e["event_type"] == "BLOCKED_ESCALATE"
        tick_e = run_json("python3", str(CRON), "--ledger", str(ledger), "--cron-dir", str(cron_dir), "run-once", "--task-id", task_e)
        assert tick_e["cron_removed"] is True
        assert task_from(ledger, task_e)["monitoring"]["cron_state"] == "DELETED"

        # F) completed -> stop/delete cron + completed handoff.
        task_f = "case-f-completed"
        run(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", task_f,
            "--goal", "Finish and hand off once",
            "--requester-channel", "1484432523781083197",
            "--workflow", "Do work",
            "--workflow", "Complete",
            "--next-action", "Do work",
        )
        run("python3", str(CRON), "--ledger", str(ledger), "--cron-dir", str(cron_dir), "install", task_f)
        completed = run_json(
            "python3", str(OPS), "--ledger", str(ledger), "record-update", "TASK_COMPLETED", task_f,
            "--summary", "All done with validated artifact",
            "--current-checkpoint", "step-02", "--next-action", "None",
            "--output", str(artifact), "--fact", "validation=passed",
        )
        assert completed["pending_user_update"]["event_type"] == "COMPLETED_HANDOFF"
        ack(ledger, task_f, completed["pending_user_update"]["update_id"], "Delivered completed handoff")
        tick_f = run_json("python3", str(CRON), "--ledger", str(ledger), "--cron-dir", str(cron_dir), "run-once", "--task-id", task_f)
        assert tick_f["cron_removed"] is True
        assert task_from(ledger, task_f)["monitoring"]["cron_state"] == "DELETED"

        # G) ledger updated but user-visible checkpoint not sent => compliance must catch it.
        task_g = "case-g-missed-delivery"
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", task_g,
            "--goal", "Catch missing user-visible delivery",
            "--requester-channel", "1484432523781083197",
            "--workflow", "Do step",
            "--next-action", "Do step",
        )
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "record-update", "STEP_COMPLETED", task_g,
            "--summary", "Did the step but forgot to report it",
            "--current-checkpoint", "step-01", "--next-action", "Continue",
            "--fact", "step=done",
        )
        compliance = run_json("python3", str(COMPLIANCE), "--ledger", str(ledger))
        miss = [f for f in compliance["findings"] if f["code"] == "missing_user_visible_update" and task_g in f["message"]]
        assert miss, compliance

        # H) partial success / skipped closeout should still converge to terminal cleanup.
        task_h = "case-h-partial-success-closeout"
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "init-task", task_h,
            "--goal", "Verify partial-success closeout still removes monitor",
            "--requester-channel", "1484432523781083197",
            "--workflow", "Step 1",
            "--workflow", "Step 2",
            "--next-action", "Run step 1",
        )
        run("python3", str(CRON), "--ledger", str(ledger), "--cron-dir", str(cron_dir), "install", task_h)
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "record-update", "STEP_COMPLETED", task_h,
            "--summary", "Completed step 1",
            "--current-checkpoint", "step-01", "--next-action", "Skip directly to closeout",
        )
        run_json(
            "python3", str(OPS), "--ledger", str(ledger), "record-update", "TASK_COMPLETED", task_h,
            "--summary", "Task completed with useful artifacts despite skipped closeout step",
            "--current-checkpoint", "step-01", "--output", str(artifact),
        )
        preview_h = run_json("python3", str(OPS), "--ledger", str(ledger), "preview-tick", task_h)
        assert preview_h["truth_state"] == "INCONSISTENT", preview_h
        assert any(item.startswith("task:completed_but_steps_not_done:") for item in preview_h["inconsistencies"]), preview_h
        assert preview_h["state"] == "STOP_AND_DELETE", preview_h
        tick_h = run_json("python3", str(CRON), "--ledger", str(ledger), "--cron-dir", str(cron_dir), "run-once", "--task-id", task_h)
        assert tick_h["cron_removed"] is True
        assert task_from(ledger, task_h)["monitoring"]["cron_state"] == "DELETED"

        print(json.dumps({
            "ok": True,
            "ledger": str(ledger),
            "scenarios": {
                "A": step_a["pending_user_update"]["event_type"],
                "B": preview_b1["state"],
                "C": [pending_c1["event_type"], pending_c2["event_type"]],
                "D": {"signal": install_signal["signal"], "preview_state": preview_d["state"]},
                "E": tick_e["report"]["state"],
                "F": tick_f["report"]["state"],
                "G": miss[0],
                "H": tick_h["report"]["state"],
            },
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

